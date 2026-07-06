#!/usr/bin/env python3
"""Measure the verifier's precision lift on held-out ground truth.

The precision harness gives, for each leave-one-out decision, whether the
structural prediction was a HIT (predicted Q == the lexeme's real P5137) or a
MISS. Here we run the LLM verifier on a random sample of those decisions —
asking yes/no whether the held-out noun denotes the *predicted* concept — and
cross-tabulate the verdict against the known truth.

If verifier-"yes" cases are hits far more often than the ~62% baseline (and far
more than verifier-"no" cases), the verifier raises precision: writing only
verifier-approved candidates yields a much cleaner stream. This is the number
that justifies the pipeline for the BRFA.

All inputs are CC0 (predicted Q's own label/description + the bare lemma).

Run after fetch_items.py:
    uv run measure_lift.py --sample 120
"""

import argparse
import collections
import random
import sqlite3

import config
import precision
import verify


def lemmas_by_lid(conn):
    out = {}
    for lid, lemma in conn.execute("SELECT lid, lemma FROM wd.lemma"):
        out.setdefault(lid, lemma)
    return out


def item_rows(conn):
    """qid -> (label_de,label_en,label_sv,desc_de,desc_en,desc_sv) from the cache."""
    out = {}
    for row in conn.execute(
        "SELECT qid,label_de,label_en,label_sv,desc_de,desc_en,desc_sv FROM it.item WHERE NOT missing"):
        out[row[0]] = row[1:]
    return out


def passes_gate(gate, predicted, n_senses, n_other, typed):
    """Structural gate filter. gate is a string of letters: A (>=2 anchors),
    B (monosemous target), D (concrete anchor with P31)."""
    if "A" in gate and not (n_other >= 2):
        return False
    if "B" in gate and not (n_senses == 1):
        return False
    if "D" in gate and predicted not in typed:
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=120)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--gate", default="", help="structural gate filter, e.g. ABD")
    args = ap.parse_args()
    gate = args.gate.upper()

    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ? AS cn", [config.CANDIDATES])
    conn.execute("ATTACH DATABASE ? AS wd", [config.WIKIDATA])
    conn.execute("ATTACH DATABASE ? AS it", [config.ITEMS])

    clusters = precision.load_clusters(conn)
    decided = [r for r in precision.evaluate(clusters) if r[3] in ("hit", "miss")]
    lemma = lemmas_by_lid(conn)
    items = item_rows(conn)
    typed = precision.typed_qids(conn) if "D" in gate else set()

    # build verifiable cases: (lid, lang, lemma, predicted_qid, label/desc...) + truth
    cases = []
    for lid, q_true, predicted, outcome, lang, n_senses, n_other in decided:
        if predicted not in items or lid not in lemma:
            continue
        if not passes_gate(gate, predicted, n_senses, n_other, typed):
            continue
        row = (lid, lang, lemma[lid], predicted, 0) + items[predicted]
        if verify.has_evidence(row):
            cases.append((row, outcome))

    rng = random.Random(args.seed)
    sample = rng.sample(cases, min(args.sample, len(cases)))
    print(f"gate={gate or '(none)'}; verifiable decided cases: {len(cases):,}; "
          f"sampling {len(sample)} (batch={args.batch}, model={args.model})")
    base_hits = sum(1 for _, o in sample if o == "hit")
    print(f"baseline structural hit-rate in sample: {precision.pct(base_hits, len(sample))} "
          f"({base_hits}/{len(sample)})")

    # verify in batches (gloss map keyed by held-out lid, matching production verify.py)
    gmap = verify.load_glosses({r[0] for r, _ in sample})
    verdicts = []
    for i in range(0, len(sample), args.batch):
        chunk = sample[i : i + args.batch]
        try:
            vs = verify.run_batch([r for r, _ in chunk], args.model, gmap)
        except Exception as e:  # noqa: BLE001 — a transient batch failure shouldn't abort the whole run
            print(f"  batch {i // args.batch} failed: {e}")
            vs = [{"verdict": "error"}] * len(chunk)
        verdicts.extend(v.get("verdict", "parse_error") for v in vs)
        print(f"  verified {min(i + args.batch, len(sample))}/{len(sample)}")

    # cross-tabulate verdict x truth
    tab = collections.defaultdict(lambda: {"hit": 0, "miss": 0})
    for (_row, outcome), verdict in zip(sample, verdicts):
        tab[verdict][outcome] += 1

    print("\nverdict x structural truth:")
    print(f"  {'verdict':<12} {'n':>5} {'hits':>5} {'precision':>10}")
    for verdict in ("yes", "uncertain", "no", "parse_error", "error"):
        if verdict not in tab:
            continue
        h, m = tab[verdict]["hit"], tab[verdict]["miss"]
        print(f"  {verdict:<12} {h + m:>5} {h:>5} {precision.pct(h, h + m):>10}")

    yes = tab["yes"]
    n_yes = yes["hit"] + yes["miss"]
    print(f"\nVERIFIER LIFT:")
    print(f"  write-everything precision : {precision.pct(base_hits, len(sample))}  (baseline)")
    print(f"  write-only-'yes' precision : {precision.pct(yes['hit'], n_yes)}  "
          f"(+{(100.0*yes['hit']/n_yes - 100.0*base_hits/len(sample)):.1f} pts)" if n_yes else "  n/a")
    print(f"  true hits kept (recall)    : {precision.pct(yes['hit'], base_hits)}  "
          f"({yes['hit']}/{base_hits} structural hits survive the verifier)")
    conn.close()


if __name__ == "__main__":
    main()
