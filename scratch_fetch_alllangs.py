#!/usr/bin/env python3
"""LEAN TEST fetcher: all-language CC0 labels/descriptions for the suspect-set anchors.

Throwaway probe for the "does >3 languages help?" A/B. Reads anchor QIDs from a file
(one per line) and fetches every language's label+description from the Wikidata Action
API (wbgetentities, no `languages` filter = all langs). Stores into a normalized table
in data/ab_alllangs.sqlite3 so ab_concept's +alllangs arm can read it. Resumable.

Reuses fetch_items.py's API etiquette (UA, maxlag, backoff). Needs network access to
www.wikidata.org. NOT the production path — if the A/B wins we build the real pipeline.

    uv run scratch_fetch_alllangs.py <qids_file>
"""
import sqlite3
import sys
import time

import config
import fetch_items as fi  # reuse http_get + etiquette

OUT = config.DATA + "/ab_alllangs.sqlite3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS item_label_all(
    qid TEXT, lang TEXT, label TEXT, desc TEXT, PRIMARY KEY(qid, lang)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fetched(qid TEXT PRIMARY KEY);
"""


def main():
    qids = [q.strip() for q in open(sys.argv[1]) if q.strip()]
    db = sqlite3.connect(OUT)
    db.executescript(SCHEMA)
    have = {r[0] for r in db.execute("SELECT qid FROM fetched")}
    todo = [q for q in qids if q not in have]
    print(f"qids: {len(qids)}  to fetch: {len(todo)}  -> {OUT}")
    for i in range(0, len(todo), fi.BATCH):
        batch = todo[i:i + fi.BATCH]
        data = fi.http_get({
            "action": "wbgetentities", "ids": "|".join(batch),
            "props": "labels|descriptions", "format": "json", "maxlag": 5,
        })
        ents = data.get("entities", {})
        rows, ncov = [], []
        for qid in batch:
            ent = ents.get(qid, {})
            labels = ent.get("labels", {}) or {}
            descs = ent.get("descriptions", {}) or {}
            langs = set(labels) | set(descs)
            for lg in langs:
                rows.append((qid, lg,
                             labels.get(lg, {}).get("value"),
                             descs.get(lg, {}).get("value")))
            ncov.append(len(langs))
        db.executemany("INSERT OR REPLACE INTO item_label_all VALUES (?,?,?,?)", rows)
        db.executemany("INSERT OR REPLACE INTO fetched VALUES (?)", [(q,) for q in batch])
        db.commit()
        print(f"  {min(i + fi.BATCH, len(todo))}/{len(todo)}  +{len(rows)} label rows "
              f"(langs/item: {min(ncov) if ncov else 0}-{max(ncov) if ncov else 0})")
        time.sleep(fi.SLEEP)
    tot = db.execute("SELECT count(*) FROM item_label_all").fetchone()[0]
    avg = db.execute("SELECT count(*)*1.0/count(DISTINCT qid) FROM item_label_all").fetchone()[0]
    print(f"done: {tot} label rows, avg {avg:.0f} languages/item")


if __name__ == "__main__":
    main()
