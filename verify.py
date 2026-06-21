#!/usr/bin/env python3
"""LLM yes/no verifier for P5137 candidates — CC0 inputs only.

For each candidate ``(target lexeme L, anchor Q)``, the model is asked a single
constrained question: does the noun ``L.lemma`` (in its language) denote the
concept identified by Q's **CC0** label + description? The answer is a verdict
(yes / no / uncertain) plus a one-line reason — never copied text.

License: the only concept evidence shown is Q's own CC0 label/description
(Wikidata→Wikidata) and the bare lemma. No DBnary gloss is ever read.

Backend: the local ``claude`` CLI in print mode (reuses your existing auth, no
API key). Each CLI call reloads ~19k tokens of agent context, so candidates are
**batched** (default 8/call) to amortise that ~10×. Verdicts are cached in
``data/verify.sqlite3`` keyed by (target_lid, anchor_qid) — re-runs skip done work.

Run after fetch_items.py (defaults to a 40-candidate sample):
    uv run verify.py                       # sample the highest-support clean candidates
    uv run verify.py --senseless-only      # only candidates grounding senseless lexemes
    uv run verify.py --all --batch 8       # full run (costs add up — see note above)
    uv run verify.py --gate ABD --all --model sonnet --csv notes/runs/high_tier_verdicts.csv
"""

import argparse
import csv
import json
import subprocess
import sqlite3

import config

SYSTEM = (
    "You are a lexicographer. For each item you are given a NOUN, its LANGUAGE, and a "
    "candidate concept described by a Wikidata CC0 label and description. Decide whether the "
    "noun, used as a common noun, denotes that exact concept. "
    'Reply with ONLY a JSON array (no prose, no code fences), one object per item in order: '
    '[{"i":1,"verdict":"yes|no|uncertain","reason":"<=12 words"}]. '
    'Use "yes" only if the noun clearly denotes that exact concept; "no" if it denotes a '
    'different concept; "uncertain" if the description is too sparse to decide.'
)

LANGNAME = {"de": "German", "en": "English", "sv": "Swedish"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict(
    target_lid TEXT, anchor_qid TEXT, target_lemma TEXT, target_lang TEXT,
    verdict TEXT, reason TEXT, model TEXT,
    PRIMARY KEY (target_lid, anchor_qid)
) WITHOUT ROWID;
"""


def candidates(args):
    """Clean candidates joined to the anchor Q's CC0 label/description.

    --gate adds structural gates matching measure_lift/ablation: A (>=2 anchors),
    B (monosemous target, n_senses==1), D (concrete anchor with a P31). ABD is the
    measured ~95.9%-precision High tier.
    """
    cn = sqlite3.connect(config.CANDIDATES)
    cn.execute("ATTACH DATABASE ? AS it", [config.ITEMS])
    gate = args.gate.upper()
    where = "c.homograph_n_lid = 1 AND NOT c.cross_pivot_conflict AND c.support_clusters >= ?"
    params = [max(args.min_support, 2 if "A" in gate else args.min_support)]
    if args.senseless_only:
        where += " AND c.target_n_senses = 0"
    if "B" in gate:
        where += " AND c.target_n_senses = 1"
    if "D" in gate:
        where += (" AND EXISTS(SELECT 1 FROM it.item_type t "
                  "WHERE t.qid = c.anchor_qid AND t.prop = 'P31')")
    rows = cn.execute(
        f"""
        SELECT c.target_lid, c.target_lang, c.target_lemma, c.anchor_qid, c.support_clusters,
               i.label_de, i.label_en, i.label_sv, i.desc_de, i.desc_en, i.desc_sv
        FROM candidate c
        JOIN it.item i ON i.qid = c.anchor_qid AND NOT i.missing
        WHERE {where}
        ORDER BY c.support_clusters DESC, c.target_lid
        """,
        params,
    ).fetchall()
    cn.close()
    return rows


def fmt_concept(row):
    _lid, lang, _lemma, qid, _sup, lde, len_, lsv, dde, den, dsv = row
    labels = {"de": lde, "en": len_, "sv": lsv}
    descs = {"de": dde, "en": den, "sv": dsv}
    # label in target language plus English, deduped; description target-lang-first.
    lab = [labels[lang], len_] if lang != "en" else [len_]
    lab = " / ".join(dict.fromkeys(x for x in lab if x)) or "(no label)"
    desc = descs[lang] or den or dde or dsv or "(no description)"
    return qid, lab, desc


def has_evidence(row):
    qid, lab, desc = fmt_concept(row)
    return lab != "(no label)" or desc != "(no description)"


def run_batch(rows, model):
    lines = []
    for n, row in enumerate(rows, 1):
        _lid, lang, lemma, *_ = row
        qid, lab, desc = fmt_concept(row)
        lines.append(
            f'{n}. language={LANGNAME.get(lang, lang)} noun="{lemma}" | '
            f'concept {qid}: label="{lab}" description="{desc}"'
        )
    prompt = "Items:\n" + "\n".join(lines)
    out = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--model", model,
         "--system-prompt", SYSTEM],
        input=prompt, capture_output=True, text=True, timeout=300,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:300])
    result = json.loads(out.stdout)["result"].strip()
    if result.startswith("```"):
        result = result.split("\n", 1)[1].rsplit("```", 1)[0]
    verdicts = json.loads(result)
    by_i = {v.get("i"): v for v in verdicts}
    return [by_i.get(n, {"verdict": "parse_error", "reason": ""}) for n in range(1, len(rows) + 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40, help="max candidates (default 40 sample)")
    ap.add_argument("--all", action="store_true", help="verify every eligible candidate")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--min-support", type=int, default=1)
    ap.add_argument("--senseless-only", action="store_true")
    ap.add_argument("--gate", default="", help="structural gate filter, e.g. ABD (the High tier)")
    ap.add_argument("--csv", default="", help="also write the full verdict table to this CSV "
                    "(committed audit trail; data/verify.sqlite3 is gitignored)")
    args = ap.parse_args()

    out = sqlite3.connect(config.VERIFY)
    out.executescript(SCHEMA)
    done = {(r[0], r[1]) for r in out.execute("SELECT target_lid, anchor_qid FROM verdict")}

    rows = [r for r in candidates(args) if (r[0], r[3]) not in done and has_evidence(r)]
    if not args.all:
        rows = rows[: args.limit]
    print(f"candidates to verify: {len(rows):,}  (batch={args.batch}, model={args.model})")

    counts = {}
    for i in range(0, len(rows), args.batch):
        batch = rows[i : i + args.batch]
        try:
            verdicts = run_batch(batch, args.model)
        except Exception as e:  # noqa: BLE001 — one bad batch shouldn't kill the run
            print(f"  batch {i // args.batch} failed: {e}")
            verdicts = [{"verdict": "error", "reason": ""}] * len(batch)
        recs = []
        for row, v in zip(batch, verdicts):
            lid, lang, lemma, qid = row[0], row[1], row[2], row[3]
            verdict = v.get("verdict", "parse_error")
            counts[verdict] = counts.get(verdict, 0) + 1
            recs.append((lid, qid, lemma, lang, verdict, v.get("reason", ""), args.model))
        out.executemany(
            "INSERT OR REPLACE INTO verdict VALUES (?,?,?,?,?,?,?)", recs)
        out.commit()
        print(f"  {min(i + args.batch, len(rows)):,}/{len(rows):,}  {counts}")

    print("\nverdict distribution:")
    for v, n in out.execute("SELECT verdict, count(*) FROM verdict GROUP BY verdict ORDER BY 2 DESC"):
        print(f"  {v:<12} {n:,}")

    if args.csv:
        cols = ["target_lid", "anchor_qid", "target_lemma", "target_lang",
                "verdict", "reason", "model"]
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(out.execute(
                f"SELECT {','.join(cols)} FROM verdict ORDER BY target_lang, target_lemma"))
        print(f"\nwrote audit CSV -> {args.csv}")
    out.close()


if __name__ == "__main__":
    main()
