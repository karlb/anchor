#!/usr/bin/env python3
"""Stream the Wikidata lexeme dump into SQLite, filtered to the de/en/sv pilot.

Consumes ``latest-lexemes.json.gz`` (the weekly Wikidata lexeme dump: a JSON
array with one lexeme entity per line). We keep only German/English/Swedish
lexemes and extract bare facts: L-id, language, lexical category, every lemma
representation, and each sense's P5137 ("item for this sense") target Q-id.

License: glosses (``senses[].glosses``) are CC-BY-SA-incompatible creative text
and are **never read or stored**. We keep only the sense *id* and its P5137
Q-id, both bare facts.

Run once you have the dump (default --dump is data/latest-lexemes.json.gz):
    uv run load_lexemes.py --dump path/to/latest-lexemes.json.gz
"""

import argparse
import gzip
import json
import os
import sqlite3

import config

# Wikidata language item -> pilot 2-letter code.
LANG_QID = {"Q188": "de", "Q1860": "en", "Q9027": "sv"}
NOUN_QID = "Q1084"

SCHEMA = """
DROP TABLE IF EXISTS lexeme;
DROP TABLE IF EXISTS lemma;
DROP TABLE IF EXISTS sense;
DROP TABLE IF EXISTS sense_item;
CREATE TABLE lexeme(lid TEXT PRIMARY KEY, lang TEXT, lexcat TEXT) WITHOUT ROWID;
CREATE TABLE lemma(lid TEXT, tag TEXT, lemma TEXT);
CREATE TABLE sense(lid TEXT, sid TEXT);
CREATE TABLE sense_item(lid TEXT, sid TEXT, qid TEXT);  -- P5137 targets
CREATE TABLE sense_ref(lid TEXT, sid TEXT, qid TEXT, prop TEXT, value TEXT);  -- P5137 ref snaks
"""

INDEXES = """
CREATE INDEX lemma_match ON lemma(lemma);
CREATE INDEX lemma_lid ON lemma(lid);
CREATE INDEX sense_lid ON sense(lid);
CREATE INDEX sense_item_lid ON sense_item(lid);
CREATE INDEX sense_item_qid ON sense_item(qid);
CREATE INDEX sense_ref_key ON sense_ref(lid, qid);
CREATE INDEX sense_ref_prop ON sense_ref(prop);
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

    lexemes, lemmas, senses, items, refs = [], [], [], [], []
    n_kept = 0

    def flush():
        conn.executemany("INSERT OR IGNORE INTO lexeme VALUES (?,?,?)", lexemes)
        conn.executemany("INSERT INTO lemma VALUES (?,?,?)", lemmas)
        conn.executemany("INSERT INTO sense VALUES (?,?)", senses)
        conn.executemany("INSERT INTO sense_item VALUES (?,?,?)", items)
        conn.executemany("INSERT INTO sense_ref VALUES (?,?,?,?,?)", refs)
        lexemes.clear(); lemmas.clear(); senses.clear(); items.clear(); refs.clear()

    for ent in iter_entities(dump):
        if ent.get("type") != "lexeme":
            continue
        lang = LANG_QID.get(ent.get("language"))
        if lang is None:
            continue  # not a pilot language
        lid = ent["id"]
        lexemes.append((lid, lang, ent.get("lexicalCategory")))
        for tag, rep in ent.get("lemmas", {}).items():
            lemmas.append((lid, tag, rep["value"]))
        for s in ent.get("senses", []):
            sid = s["id"]
            senses.append((lid, sid))
            for qid, claim_refs in p5137_claims(s):  # glosses deliberately ignored
                items.append((lid, sid, qid))
                for prop, value in claim_refs:
                    refs.append((lid, sid, qid, prop, value))
        n_kept += 1
        if len(lemmas) > 50000:
            flush()
    flush()
    conn.executescript(INDEXES)
    conn.commit()

    stats = dict(
        lexemes=n_kept,
        per_lang=conn.execute("SELECT lang, count(*) FROM lexeme GROUP BY lang").fetchall(),
        nouns=conn.execute("SELECT count(*) FROM lexeme WHERE lexcat=?", [NOUN_QID]).fetchone()[0],
        p5137_senses=conn.execute("SELECT count(DISTINCT lid||sid) FROM sense_item").fetchone()[0],
        anchored_noun_senses=conn.execute(
            "SELECT count(*) FROM sense_item si JOIN lexeme l USING(lid) WHERE l.lexcat=?", [NOUN_QID]
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
    print(f"kept lexemes (de/en/sv): {stats['lexemes']:,}")
    for lang, n in stats["per_lang"]:
        print(f"  {lang}: {n:,}")
    print(f"nouns (Q1084): {stats['nouns']:,}")
    print(f"senses with P5137: {stats['p5137_senses']:,}")
    print(f"  on noun lexemes (anchor universe): {stats['anchored_noun_senses']:,}")
    print(f"P5137 statements carrying any reference: {stats['p5137_with_ref']:,}")
    for prop, n in stats["ref_props"]:
        print(f"  ref {prop}: {n:,}")


if __name__ == "__main__":
    main()
