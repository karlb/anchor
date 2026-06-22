#!/usr/bin/env python3
"""Structural ablation: held-out precision vs. volume for candidate gates.

Reuses the leave-one-out decisions from precision.py and augments each with
structural features, then reports precision and surviving volume for individual
gates and their combinations. The point: find the structural tier that is as
precise as possible while keeping useful volume, BEFORE leaning on the LLM —
every structural point won is a point the verifier doesn't have to earn.

Gates measured (all CC0 / structural, no gloss):
  A  >=2 agreeing anchors   (the other cluster members agreeing on the Q number >= 2)
  B  monosemous target      (held-out lexeme has exactly one sense)
  C  same-label / cognate    (target lemma equals the predicted Q's CC0 label in its language)

CAVEAT on gate C: much of the held-out P5137 ground truth was itself created by
label-matching tools (makesense), so C's measured precision is *circularly
inflated*. Trust it only after de-biasing on the non-label-matched subset —
which would need per-statement P887 provenance; load_lexemes.py extracts it
(sense_ref), but references are far too sparse to use. Flagged in the output.

Run after match.py + fetch_items.py:
    uv run ablation.py
"""

import sqlite3

import config
import precision


def lemma_sets(conn):
    """lid -> set of all its lemma representations."""
    out = {}
    for lid, lemma in conn.execute("SELECT lid, lemma FROM wd.lemma"):
        out.setdefault(lid, set()).add(lemma)
    return out


def item_labels(conn):
    """qid -> {'de':..,'en':..,'sv':..} CC0 labels."""
    out = {}
    for qid, lde, len_, lsv in conn.execute(
        "SELECT qid, label_de, label_en, label_sv FROM it.item WHERE NOT missing"):
        out[qid] = {"de": lde, "en": len_, "sv": lsv}
    return out


def typed_qids(conn):
    """qids that have at least one P31 (instance of) — a concrete/typed concept.
    Principled (not overfit): on the unbiased non-cognate subset, having any P31
    is +16pt (59.7% vs 43.8% hit). Abstract typed classes (concept/activity/role)
    still drag, but excluding those needs a blocklist we don't have here."""
    return {r[0] for r in conn.execute("SELECT DISTINCT qid FROM it.item_type WHERE prop='P31'")}


def build_records(conn):
    """Decided leave-one-out records + structural feature flags.

    Each record: (outcome, A_ge2_anchors, B_monosemous, C_same_label, truth_cognate).
    truth_cognate = the held-out lexeme's *own* lemma equals its true Q's label in
    its language → that P5137 could have been produced by label-matching, so it is
    a *biased* ground-truth row for gate C. The complement (non-cognate truth) is
    an unbiased test bed for the gates that don't depend on the cognate relation.
    """
    clusters = precision.load_clusters(conn)
    lemmas = lemma_sets(conn)
    labels = item_labels(conn)
    typed = typed_qids(conn)
    recs = []
    for lid, q_true, predicted, outcome, lang, n_senses, n_other in precision.evaluate(clusters):
        if outcome not in ("hit", "miss"):
            continue
        mine = lemmas.get(lid, set())
        same_label = labels.get(predicted, {}).get(lang) in mine
        truth_cognate = labels.get(q_true, {}).get(lang) in mine
        concrete = predicted in typed
        recs.append((outcome, n_other >= 2, n_senses == 1, same_label, truth_cognate, concrete))
    return recs


def measure(recs, name, pred):
    sub = [r for r in recs if pred(r)]
    hits = sum(1 for r in sub if r[0] == "hit")
    n = len(sub)
    vol = f"{100.0 * n / len(recs):.0f}%" if recs else "n/a"
    print(f"  {name:<34} precision={precision.pct(hits, n):>6}  n={n:>6,}  ({vol} of decided)")


def main():
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ? AS cn", [config.CANDIDATES])
    conn.execute("ATTACH DATABASE ? AS wd", [config.WIKIDATA])
    conn.execute("ATTACH DATABASE ? AS it", [config.ITEMS])
    recs = build_records(conn)
    print(f"decided held-out cases: {len(recs):,}\n")

    A = lambda r: r[1]
    B = lambda r: r[2]
    C = lambda r: r[3]
    D = lambda r: r[5]  # concrete: predicted anchor has a P31 type
    print("single gates:")
    measure(recs, "baseline (none)", lambda r: True)
    measure(recs, "A: >=2 agreeing anchors", A)
    measure(recs, "B: monosemous target", B)
    measure(recs, "C: same-label / cognate", C)
    measure(recs, "D: concrete anchor (has P31)", D)
    print("\ncombinations:")
    measure(recs, "A & C", lambda r: A(r) and C(r))
    measure(recs, "A & D", lambda r: A(r) and D(r))
    measure(recs, "C & D", lambda r: C(r) and D(r))
    measure(recs, "A & C & D", lambda r: A(r) and C(r) and D(r))
    measure(recs, "A & B & C & D", lambda r: A(r) and B(r) and C(r) and D(r))

    # De-biased view: per-statement provenance is far too sparse to use (only ~48/65k
    # P5137 carry any reference, ≈1 with P887), so we can't exclude label-matched edits.
    # Instead restrict to NON-cognate ground truth (lemma != true Q's label) — rows
    # label-matching could not have produced. Gate C degenerates here (a correct
    # prediction has label == true-label != lemma, so C can't fire), so we report
    # only the gates that remain meaningful.
    unbiased = [r for r in recs if not r[4]]
    print(f"\nDE-BIASED — non-cognate ground truth only (n={len(unbiased):,}, "
          f"{100.0*len(unbiased)/len(recs):.0f}% of decided):")
    measure(unbiased, "baseline (none)", lambda r: True)
    measure(unbiased, "A: >=2 agreeing anchors", A)
    measure(unbiased, "B: monosemous target", B)
    measure(unbiased, "D: concrete anchor (has P31)", D)
    measure(unbiased, "A & D", lambda r: A(r) and D(r))
    measure(unbiased, "A & B & D", lambda r: A(r) and B(r) and D(r))
    print("  (gate C omitted — degenerate on non-cognate truth)")

    print("\nTakeaways: gate A (>=2 anchors) is non-circular and holds on hard non-cognate "
          "cases. Gate C cannot be de-biased from data alone (provenance too sparse + 82.7% of "
          "truth is cognate); its real precision must be checked by the independent LLM verifier, "
          "which judges from the description, not the label.")
    conn.close()


if __name__ == "__main__":
    main()
