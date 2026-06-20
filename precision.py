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
stratification (makesense-origin vs. other) needs the P887 reference on each
P5137 statement, which ``load_lexemes.py`` does not yet extract — see "Provenance"
at the end of the output. All inputs here are CC0 / structural; no gloss is read.

Run after match.py:
    uv run precision.py
"""

import collections
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
    stratum("language", lambda r: r[4],
            [("de", lambda v: v == "de"), ("en", lambda v: v == "en"), ("sv", lambda v: v == "sv")])

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

    print("\nProvenance: not stratified — needs the P887 reference on each P5137 "
          "statement, which load_lexemes.py does not extract yet. Treat the numbers "
          "above as agreement-with-existing-data (an upper bound on true precision).")


def main():
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ? AS cn", [config.CANDIDATES])
    conn.execute("ATTACH DATABASE ? AS wd", [config.WIKIDATA])
    clusters = load_clusters(conn)
    report(evaluate(clusters))
    conn.close()


if __name__ == "__main__":
    main()
