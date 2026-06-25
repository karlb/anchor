#!/usr/bin/env python3
"""LLM semantic-gender tagger for P10339 — CC0 inputs only (the bare lemma).

The Wikidata convention for gendered lexemes is two-part: P5137 links the sense to
the *gender-neutral* concept, and the word's sex is recorded separately on the
sense via semantic gender (P10339) — see Wikidata:Lexicographical_data/Documentation/Senses
(the German 'Person' example uses P10339=novalue to mark a grammatically-feminine
but gender-neutral word). So writing P5137 alone on a feminine demonym/occupation is
convention-incomplete; this tagger supplies the missing P10339 input.

For each approved write candidate we ask the model ONE thing: is the noun's meaning
specifically female, specifically male, or gender-neutral? The answer drives P10339
in write.py. Only the bare lemma (+ language) is shown — no DBnary gloss, no item
text — so it stays inside the CC0 boundary. This is deliberately a separate question
from verify.py's concept match: gender is a property of the lexeme, so it is cached
in data/gender.sqlite3 keyed by target_lid and SURVIVES verifier-prompt rebuilds.

Run after the human review (reads status='approved' from the decision log):
    uv run gender.py --all --model sonnet
"""

import argparse
import sqlite3

import config
import llm
from review_queue import load_decisions

SYSTEM = (
    "You are a lexicographer. For each item you are given a NOUN and its LANGUAGE. Decide the "
    "SEMANTIC gender of the word's meaning: 'female' if it denotes specifically female people or "
    "animals, 'male' if specifically male, otherwise 'neutral' (used for any/all sexes). Judge by "
    "MEANING, not grammatical gender. TWO STRICT RULES: (1) a generic masculine form or a "
    "common-gender demonym used for everyone is NEUTRAL, not male (e.g. German 'Iraner' as the "
    "general term, Swedish 'belgare' = a Belgian of any sex); (2) a word that is NOT a "
    "person/animal-by-sex noun at all — an animal species, an object, a place — is NEUTRAL (e.g. "
    "Swedish 'and' = a duck). When unsure between a sex and neutral, choose neutral. "
    "Examples: German 'Person' (grammatically feminine but denotes "
    "anyone) -> neutral; 'Maedchen' (a girl) -> female; 'Franzoesin' (a Frenchwoman) -> female; "
    "'Arzt' (any doctor) -> neutral; 'Witwer' (a widower) -> male; English 'actress' -> female; "
    "'teacher' -> neutral; Swedish 'skadespelerska' (an actress) -> female. "
    'Reply with ONLY a JSON array (no prose, no code fences), one object per item in order: '
    '[{"i":1,"gender":"female|male|neutral"}].'
)

LANGNAME = {"de": "German", "en": "English", "sv": "Swedish"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS gender(
    target_lid TEXT PRIMARY KEY, target_lemma TEXT, target_lang TEXT,
    gender TEXT, model TEXT
) WITHOUT ROWID;
"""


def approved_lexemes():
    """Distinct (lid, lemma, lang) the human approved — the write set, deduped by lexeme."""
    seen = {}
    for d in load_decisions().values():
        if d["status"] == "approved":
            seen.setdefault(d["target_lid"], (d["target_lemma"], d["target_lang"]))
    return [(lid, lemma, lang) for lid, (lemma, lang) in seen.items()]


def run_batch(rows, model):
    lines = [f'{n}. language={LANGNAME.get(lang, lang)} noun="{lemma}"'
             for n, (_lid, lemma, lang) in enumerate(rows, 1)]
    return llm.batch_json(SYSTEM, "Items:\n" + "\n".join(lines), model, len(rows),
                          {"gender": "parse_error"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40, help="max lexemes (default 40 sample)")
    ap.add_argument("--all", action="store_true", help="classify every approved lexeme")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="haiku")
    args = ap.parse_args()

    out = sqlite3.connect(config.GENDER)
    out.executescript(SCHEMA)
    done = {r[0] for r in out.execute("SELECT target_lid FROM gender")}
    rows = [r for r in approved_lexemes() if r[0] not in done]
    if not args.all:
        rows = rows[: args.limit]
    print(f"lexemes to classify: {len(rows)}  (batch={args.batch}, model={args.model})")

    counts = {}
    for i in range(0, len(rows), args.batch):
        batch = rows[i : i + args.batch]
        try:
            genders = run_batch(batch, args.model)
        except Exception as e:  # noqa: BLE001 — one bad batch shouldn't kill the run
            print(f"  batch {i // args.batch} failed: {e}")
            genders = [{"gender": "error"}] * len(batch)
        recs = []
        for (lid, lemma, lang), g in zip(batch, genders):
            gg = g.get("gender", "parse_error")
            counts[gg] = counts.get(gg, 0) + 1
            recs.append((lid, lemma, lang, gg, args.model))
        out.executemany("INSERT OR REPLACE INTO gender VALUES (?,?,?,?,?)", recs)
        out.commit()
        print(f"  {min(i + args.batch, len(rows))}/{len(rows)}  {counts}")

    print("\ngender distribution:")
    for g, n in out.execute("SELECT gender, count(*) FROM gender GROUP BY gender ORDER BY 2 DESC"):
        print(f"  {g:<10} {n}")
    out.close()


if __name__ == "__main__":
    main()
