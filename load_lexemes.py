#!/usr/bin/env python3
"""Stream the Wikidata lexeme dump into SQLite: review languages in full, the rest
anchor-only.

Consumes ``latest-lexemes.json.gz`` (the weekly Wikidata lexeme dump: a JSON
array with one lexeme entity per line) and extracts bare facts: L-id, language,
lexical category, every lemma representation, each sense's P5137 ("item for this
sense") target Q-id, and each sense's own CC0 gloss.

Two loading modes, keyed on ``config.REVIEW_LANGS``:
  - **Review languages** (de/en/sv/fr/no): kept in full — they can be write
    *targets* (grounded + reviewed) or anchors.
  - **Every other WikDict language** (``LANG_QID`` covers all ~26): kept
    **anchor-only** — a lexeme is loaded only if it carries a P5137. match.py
    restricts targets to the review languages, so a non-review lexeme can only
    ever serve as an anchor (an existing P5137 concept another cluster member
    inherits); one without P5137 is dead weight. This is what lets a cluster
    whose only grounded member is, say, Italian still anchor a German target.

License: Wikidata's own data is CC0, including sense glosses (``senses[].glosses``),
so we DO store them — they let a reviewer see the lexeme's own sense and let us audit
sense matches (a Wikidata gloss like "a piece of paper" reveals that a monosemous
"sheet" is not the nautical-line sense). It is *DBnary's* glosses that are CC-BY-SA;
those are never touched. We also keep each sense *id* and its P5137 Q-id, bare facts.

Run once you have the dump (default --dump is data/latest-lexemes.json.gz):
    uv run load_lexemes.py --dump path/to/latest-lexemes.json.gz
"""

import argparse
import gzip
import json
import os
import sqlite3

import config

# Wikidata language item -> WikDict 2-letter code, for all ~26 languages in
# infer.all_trans. Each QID is the one the dump actually tags that language's lexemes
# with (derived by scanning latest-lexemes.json.gz, not guessed): a mistyped QID would
# silently load zero anchors. Note the non-obvious ones — no = Bokmaal Q25167 (its lemma
# tag is "nb"; matches WikDict `no`, sourced from the Bokmaal Wiktionary; Nynorsk Q25164
# excluded), and zh lexemes live under Q727694, not the macrolanguage Q7850.
LANG_QID = {
    # review languages (config.REVIEW_LANGS) — loaded in full (can be targets)
    "Q188": "de", "Q1860": "en", "Q9027": "sv", "Q150": "fr", "Q25167": "no",
    # remaining WikDict languages — loaded anchor-only (P5137-bearing lexemes)
    "Q652": "it", "Q9056": "cs", "Q7737": "ru", "Q9035": "da", "Q5287": "ja",
    "Q1321": "es", "Q5146": "pt", "Q397": "la", "Q809": "pl", "Q1412": "fi",
    "Q7411": "nl", "Q256": "tr", "Q727694": "zh", "Q9240": "id", "Q7026": "ca",
    "Q7918": "bg", "Q36510": "el", "Q9142": "ga", "Q36163": "ku", "Q7930": "mg",
    "Q9083": "lt",
}
assert set(config.REVIEW_LANGS) <= set(LANG_QID.values()), "LANG_QID must cover config.REVIEW_LANGS"

SCHEMA = """
DROP TABLE IF EXISTS lexeme;
DROP TABLE IF EXISTS lemma;
DROP TABLE IF EXISTS sense;
DROP TABLE IF EXISTS sense_item;
DROP TABLE IF EXISTS sense_ref;
DROP TABLE IF EXISTS sense_gloss;
CREATE TABLE lexeme(lid TEXT PRIMARY KEY, lang TEXT, lexcat TEXT) WITHOUT ROWID;
CREATE TABLE lemma(lid TEXT, tag TEXT, lemma TEXT);
CREATE TABLE sense(lid TEXT, sid TEXT);
CREATE TABLE sense_item(lid TEXT, sid TEXT, qid TEXT);  -- P5137 targets
CREATE TABLE sense_ref(lid TEXT, sid TEXT, qid TEXT, prop TEXT, value TEXT);  -- P5137 ref snaks
CREATE TABLE sense_gloss(lid TEXT, sid TEXT, lang TEXT, gloss TEXT);  -- CC0 Wikidata glosses
"""

INDEXES = """
CREATE INDEX lemma_match ON lemma(lemma);
CREATE INDEX lemma_lid ON lemma(lid);
CREATE INDEX sense_lid ON sense(lid);
CREATE INDEX sense_item_lid ON sense_item(lid);
CREATE INDEX sense_item_qid ON sense_item(qid);
CREATE INDEX sense_ref_key ON sense_ref(lid, qid);
CREATE INDEX sense_ref_prop ON sense_ref(prop);
CREATE INDEX sense_gloss_lid ON sense_gloss(lid);
"""


def open_dump(path):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, encoding="utf-8")


def iter_entities(path):
    """Yield one parsed entity per line, tolerating the [ ... , ] array wrapper."""
    with open_dump(path) as f:
        for line in f:
            line = line.strip().rstrip(",")
            if not line or line in "[]":
                continue
            yield json.loads(line)


def p5137_claims(sense):
    """Yield (target_qid, [(ref_prop, ref_value), ...]) per P5137 statement.

    ref_value is the snak's entity id (Q…/P…) when entity-valued, else the raw
    value as text, or None for novalue/somevalue. Captures provenance (e.g.
    P887 "based on heuristic") so we can de-bias the precision measurement.
    """
    for claim in sense.get("claims", {}).get("P5137", []):
        snak = claim.get("mainsnak", {})
        if snak.get("snaktype") != "value":
            continue  # skip novalue/somevalue
        qid = snak.get("datavalue", {}).get("value", {}).get("id")
        if not qid:
            continue
        refs = []
        for ref in claim.get("references", []):
            for prop, snaks in ref.get("snaks", {}).items():
                for s in snaks:
                    if s.get("snaktype") != "value":
                        refs.append((prop, None))
                        continue
                    dv = s.get("datavalue", {}).get("value")
                    val = dv.get("id") if isinstance(dv, dict) else dv
                    refs.append((prop, str(val) if val is not None else None))
        yield qid, refs


def load(dump, out):
    if os.path.exists(out):
        os.remove(out)
    conn = sqlite3.connect(out)
    conn.executescript(SCHEMA)

    lexemes, lemmas, senses, items, refs, glosses = [], [], [], [], [], []
    n_kept = 0
    review = set(config.REVIEW_LANGS)

    def flush():
        conn.executemany("INSERT OR IGNORE INTO lexeme VALUES (?,?,?)", lexemes)
        conn.executemany("INSERT INTO lemma VALUES (?,?,?)", lemmas)
        conn.executemany("INSERT INTO sense VALUES (?,?)", senses)
        conn.executemany("INSERT INTO sense_item VALUES (?,?,?)", items)
        conn.executemany("INSERT INTO sense_ref VALUES (?,?,?,?,?)", refs)
        conn.executemany("INSERT INTO sense_gloss VALUES (?,?,?,?)", glosses)
        lexemes.clear(); lemmas.clear(); senses.clear(); items.clear(); refs.clear(); glosses.clear()

    for ent in iter_entities(dump):
        if ent.get("type") != "lexeme":
            continue
        lang = LANG_QID.get(ent.get("language"))
        if lang is None:
            continue  # not a WikDict language
        lid = ent["id"]
        # Buffer this lexeme's sense rows and note whether it is an anchor (carries P5137).
        my_senses, my_glosses, my_items, my_refs = [], [], [], []
        for s in ent.get("senses", []):
            sid = s["id"]
            my_senses.append((lid, sid))
            for lang_code, g in s.get("glosses", {}).items():  # CC0 Wikidata glosses
                my_glosses.append((lid, sid, lang_code, g["value"]))
            for qid, claim_refs in p5137_claims(s):
                my_items.append((lid, sid, qid))
                for prop, value in claim_refs:
                    my_refs.append((lid, sid, qid, prop, value))
        # Non-review languages are loaded ANCHOR-ONLY: match.py restricts targets to
        # REVIEW_LANGS, so a non-review lexeme can only ever serve as an anchor (an
        # existing P5137). One with no P5137 is dead weight — skip it. Review-language
        # lexemes are always kept (they can be targets).
        if lang not in review and not my_items:
            continue
        lexemes.append((lid, lang, ent.get("lexicalCategory")))
        for tag, rep in ent.get("lemmas", {}).items():
            lemmas.append((lid, tag, rep["value"]))
        senses.extend(my_senses); glosses.extend(my_glosses)
        items.extend(my_items); refs.extend(my_refs)
        n_kept += 1
        if len(lemmas) > 50000:
            flush()
    flush()
    conn.executescript(INDEXES)
    conn.commit()

    stats = dict(
        lexemes=n_kept,
        per_lang=conn.execute("SELECT lang, count(*) FROM lexeme GROUP BY lang").fetchall(),
        nouns=conn.execute("SELECT count(*) FROM lexeme WHERE lexcat=?", [config.NOUN_QID]).fetchone()[0],
        p5137_senses=conn.execute("SELECT count(DISTINCT lid||sid) FROM sense_item").fetchone()[0],
        anchored_noun_senses=conn.execute(
            "SELECT count(*) FROM sense_item si JOIN lexeme l USING(lid) WHERE l.lexcat=?", [config.NOUN_QID]
        ).fetchone()[0],
        p5137_with_ref=conn.execute(
            "SELECT count(DISTINCT lid||sid||qid) FROM sense_ref").fetchone()[0],
        ref_props=conn.execute(
            "SELECT prop, count(DISTINCT lid||sid||qid) FROM sense_ref GROUP BY prop ORDER BY 2 DESC LIMIT 8"
        ).fetchall(),
    )
    conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=config.DUMP)
    ap.add_argument("--out", default=config.WIKIDATA)
    args = ap.parse_args()
    os.makedirs(config.DATA, exist_ok=True)
    if not os.path.exists(args.dump):
        raise SystemExit(f"dump not found: {args.dump}\nDownload latest-lexemes.json.gz and pass --dump.")
    stats = load(args.dump, args.out)
    review = set(config.REVIEW_LANGS)
    print(f"kept lexemes: {stats['lexemes']:,}  "
          f"(review {'/'.join(config.REVIEW_LANGS)} in full; other WikDict langs anchor-only)")
    for lang, n in stats["per_lang"]:
        print(f"  {lang}: {n:,}{'' if lang in review else '  (anchor-only)'}")
    print(f"nouns (Q1084): {stats['nouns']:,}")
    print(f"senses with P5137: {stats['p5137_senses']:,}")
    print(f"  on noun lexemes (anchor universe): {stats['anchored_noun_senses']:,}")
    print(f"P5137 statements carrying any reference: {stats['p5137_with_ref']:,}")
    for prop, n in stats["ref_props"]:
        print(f"  ref {prop}: {n:,}")


if __name__ == "__main__":
    main()
