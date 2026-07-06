#!/usr/bin/env python3
"""Held-out precision harness for the anchor-propagation method.

Closes the open question from ``notes/wikidata.md``: the P5137 pivot's precision
was never measured. We measure it by *leave-one-out* over lexemes that already
carry a P5137 (the ground truth):

  For each cluster and each member lexeme L that is a clean anchor (its own
  P5137 senses agree on a single Q = ``q_true``), pretend L has no P5137 and
  recompute the cluster's anchor from the OTHER clean anchors. If those others
  agree on a single Q (``predicted``), the method would propagate it to L; the
  prediction is a HIT iff ``predicted == q_true``. If the others disagree, the
  method abstains (no write — not counted as an error). If L is the only anchor,
  there is nothing to predict from (no coverage).

This mirrors exactly what the pipeline does when it grounds a *senseless* target:
it copies the single dominant anchor Q of the other members. So the hit-rate is
the propagation precision on writable clusters.

Two granularities are reported:
  * per-cluster propagation precision — every (cluster, held-out anchor) decision;
  * per-write-unit precision — deduped to (lid, predicted_Q), the actual write unit.

CAVEAT (stated, per the plan): this measures *agreement with existing P5137*, not
absolute truth. A fraction of held-out labels may share the method's own
low-gate (label-matching) origin, which inflates the number. True provenance
stratification (makesense-origin vs. other) would need the P887 reference on each
P5137 statement; ``load_lexemes.py`` does extract references (the ``sense_ref``
table), but they are far too sparse to de-bias — see "Provenance" in the output.
All inputs here are CC0 / structural; no gloss is read.

Run after match.py:
    uv run precision.py
"""

import collections
import os
import sqlite3

import config


def load_clusters(conn):
    """clean_anchor joined to lexeme language + sense count, grouped per cluster.

    Returns {(lexentry, sense_num): [(lid, qid, lang, n_senses), ...]}.
    """
    rows = conn.execute(
        """
        SELECT ca.lexentry, ca.sense_num, ca.lid, ca.qid, lx.lang,
               (SELECT count(*) FROM wd.sense s WHERE s.lid = ca.lid) AS n_senses
        FROM cn.clean_anchor ca
        JOIN wd.lexeme lx ON lx.lid = ca.lid
        """
    ).fetchall()
    clusters = collections.defaultdict(list)
    for lexentry, sense_num, lid, qid, lang, n_senses in rows:
        clusters[(lexentry, sense_num)].append((lid, qid, lang, n_senses))
    return clusters


def evaluate(clusters):
    """Leave-one-out over every clean anchor; yield one record per held-out lexeme.

    record = (lid, q_true, predicted_or_None, outcome, lang, n_senses, n_other_anchors)
    outcome in {'hit','miss','abstain','no_anchor'}.
    """
    for members in clusters.values():
        if len(members) < 2:
            continue  # only one anchor: nothing to predict it from
        for i, (lid, q_true, lang, n_senses) in enumerate(members):
            others = [m for j, m in enumerate(members) if m[0] != lid]
            other_qids = {m[1] for m in others}
            n_other = len(others)
            if not other_qids:
                yield (lid, q_true, None, "no_anchor", lang, n_senses, 0)
            elif len(other_qids) > 1:
                yield (lid, q_true, None, "abstain", lang, n_senses, n_other)
            else:
                predicted = next(iter(other_qids))
                outcome = "hit" if predicted == q_true else "miss"
                yield (lid, q_true, predicted, outcome, lang, n_senses, n_other)


def pct(num, den):
    return f"{100.0 * num / den:.1f}%" if den else "n/a"


def typed_qids(conn):
    """qids with at least one P31 — the measurement harnesses' 'concrete anchor' feature
    (gate D). Expects the items DB attached as `it`. Principled (not overfit): on the
    unbiased non-cognate subset, having any P31 is +16pt (59.7% vs 43.8% hit).
    NOTE: the live SQL gate (gates.concrete_anchor_clause) additionally excludes
    out-of-scope classes; the harnesses keep the simpler has-P31 definition."""
    return {r[0] for r in conn.execute("SELECT DISTINCT qid FROM it.item_type WHERE prop='P31'")}


def load_types():
    """{qid: set(parent qids via P31/P279)} from the items cache, or None."""
    if not os.path.exists(config.ITEMS):
        return None
    conn = sqlite3.connect(config.ITEMS)
    types = collections.defaultdict(set)
    for qid, _prop, value in conn.execute("SELECT qid, prop, value FROM item_type"):
        types[qid].add(value)
    conn.close()
    return types if types else None


def tolerant_precision(records):
    """Reclassify strict misses by how close predicted is to q_true in the P31/P279
    graph: a parent/child hop, or a shared direct parent (siblings)."""
    types = load_types()
    if types is None:
        print("\n(P279-tolerant precision skipped — run fetch_items.py first.)")
        return
    decided = [r for r in records if r[3] in ("hit", "miss")]
    exact = sum(1 for r in decided if r[3] == "hit")
    parent_child = sibling = 0
    for _lid, q_true, pred, outcome, *_ in decided:
        if outcome == "hit":
            continue
        tp, tt = types.get(pred, set()), types.get(q_true, set())
        if q_true in tp or pred in tt:
            parent_child += 1
        elif tp & tt:
            sibling += 1
    n = len(decided)
    print("\nP279-TOLERANT PRECISION (how many 'misses' are really neighbouring Q-items):")
    print(f"  exact match                : {pct(exact, n)}  ({exact:,}/{n:,})")
    print(f"  + parent/child (1 hop)     : {pct(exact + parent_child, n)}  (+{parent_child:,})")
    print(f"  + sibling (shared parent)  : {pct(exact + parent_child + sibling, n)}  (+{sibling:,})")
    print(f"  remaining genuine misses   : {n - exact - parent_child - sibling:,}")


def report(records):
    records = list(records)
    n_total = len(records)
    by = collections.Counter(r[3] for r in records)
    decided = by["hit"] + by["miss"]  # cases where the method made a single prediction

    print(f"held-out clean anchors evaluated: {n_total:,}")
    print(f"  predicted (single dominant other-anchor): {decided:,}  ({pct(decided, n_total)} coverage)")
    print(f"  abstained (other anchors disagree):       {by['abstain']:,}")
    print(f"  no other anchor (uncovered):              {by['no_anchor']:,}")
    print(f"\nPROPAGATION PRECISION (per held-out decision): {pct(by['hit'], decided)}  "
          f"({by['hit']:,}/{decided:,})")

    def stratum(name, keyfn, buckets):
        print(f"\n  by {name}:")
        for label, pred in buckets:
            sub = [r for r in records if r[3] in ("hit", "miss") and pred(keyfn(r))]
            hits = sum(1 for r in sub if r[3] == "hit")
            print(f"    {label:<14} precision={pct(hits, len(sub)):>6}  n={len(sub):,}")

    stratum("held-out polysemy", lambda r: r[5],
            [("monosemous", lambda v: v == 1), ("polysemous", lambda v: v > 1)])
    stratum("agreeing anchors", lambda r: r[6],
            [(">=2 anchors", lambda v: v >= 2), ("exactly 1", lambda v: v == 1)])
    # every language present in the decided records (the held-out anchors span all loaded
    # languages, not just the review set) — derived, so a new language can't go unreported
    langs = sorted({r[4] for r in records if r[3] in ("hit", "miss")})
    stratum("language", lambda r: r[4],
            [(lg, (lambda v, lg=lg: v == lg)) for lg in langs])

    # per-write-unit view: dedupe to (held-out lid, predicted Q) — the write unit.
    write_units = {}  # (lid, predicted) -> True if matches that lid's q_true
    truth = {}        # lid -> set of true qids
    for lid, q_true, predicted, outcome, *_ in records:
        truth.setdefault(lid, set()).add(q_true)
    for lid, q_true, predicted, outcome, *_ in records:
        if predicted is not None:
            write_units[(lid, predicted)] = predicted in truth[lid]
    if write_units:
        correct = sum(1 for v in write_units.values() if v)
        print(f"\nWRITE-UNIT PRECISION (deduped (lid,Q) proposals): {pct(correct, len(write_units))}  "
              f"({correct:,}/{len(write_units):,})")

    tolerant_precision(records)


def provenance_note(conn):
    """Why the numbers above are agreement-with-existing-data, not absolute truth.

    De-biasing would exclude label-matched P5137 (identifiable by their P887
    reference). load_lexemes.py *does* extract references (sense_ref), but they are
    far too sparse to stratify on — so the de-bias is not viable from data alone."""
    total = conn.execute("SELECT count(DISTINCT lid||sid||qid) FROM wd.sense_item").fetchone()[0]
    refs = conn.execute("SELECT count(DISTINCT lid||sid||qid) FROM wd.sense_ref").fetchone()[0]
    p887 = conn.execute(
        "SELECT count(DISTINCT lid||sid||qid) FROM wd.sense_ref WHERE prop='P887'").fetchone()[0]
    print(f"\nProvenance: only {refs:,} of {total:,} P5137 statements carry any reference "
          f"({p887:,} with P887) — load_lexemes.py extracts these (sense_ref) but they are far "
          f"too sparse to de-bias, so treat the numbers above as agreement-with-existing-data "
          f"(an upper bound on true precision).")


def main():
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ? AS cn", [config.CANDIDATES])
    conn.execute("ATTACH DATABASE ? AS wd", [config.WIKIDATA])
    clusters = load_clusters(conn)
    report(evaluate(clusters))
    provenance_note(conn)
    conn.close()


if __name__ == "__main__":
    main()
