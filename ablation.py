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
inflated*. Trust it only after de-biasing on the non-label-matched subset
(needs P887 reference extraction in load_lexemes.py). Flagged in the output.

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


def build_records(conn):
    """Decided leave-one-out records + structural feature flags.

    Each record: (outcome, A_ge2_anchors, B_monosemous, C_same_label).
    """
    clusters = precision.load_clusters(conn)
    lemmas = lemma_sets(conn)
    labels = item_labels(conn)
    recs = []
    for lid, _q_true, predicted, outcome, lang, n_senses, n_other in precision.evaluate(clusters):
        if outcome not in ("hit", "miss"):
            continue
        same_label = labels.get(predicted, {}).get(lang) in lemmas.get(lid, set())
        recs.append((outcome, n_other >= 2, n_senses == 1, same_label))
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
    print("single gates:")
    measure(recs, "baseline (none)", lambda r: True)
    measure(recs, "A: >=2 agreeing anchors", A)
    measure(recs, "B: monosemous target", B)
    measure(recs, "C: same-label / cognate", C)
    print("\ncombinations:")
    measure(recs, "A & B", lambda r: A(r) and B(r))
    measure(recs, "A & C", lambda r: A(r) and C(r))
    measure(recs, "B & C", lambda r: B(r) and C(r))
    measure(recs, "A & B & C", lambda r: A(r) and B(r) and C(r))

    print("\n⚠ Gate C (same-label) is circularly inflated against label-matched ground "
          "truth; de-bias on the non-label-matched subset (needs P887 extraction) before "
          "trusting it. Volume here is held-out coverage, a proxy for writable volume.")
    conn.close()


if __name__ == "__main__":
    main()
