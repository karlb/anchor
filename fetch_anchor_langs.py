#!/usr/bin/env python3
"""Fetch all-language CC0 labels/descriptions for the review queue's anchor Q-items.

The review UI (review_queue.py) shows each anchor's label+description so a human can
judge the scope match. fetch_items.py only caches de/en/sv (the verifier's inputs);
this fills in the rest so the reviewer can cross-check a scope call against more
languages (e.g. a demonym vs its ethnic-group concept reads differently across langs).

Targets exactly the anchors the review UI can surface — the distinct anchor Q-ids of
review_queue.pool() (the verifier-'yes' High-tier pool) — and fetches EVERY language's
label+description (no `languages` filter), so which subset the UI displays is a pure
review_queue.py choice, changeable without a re-fetch. Everything fetched is CC0.

Stored in data/wd_items.sqlite3 (alongside the de/en/sv `item` table):
  * item_lang(qid, lang, label, desc)  — one row per (item, language)
  * item_lang_fetched(qid)             — resumability marker (an item may legitimately
                                         have zero rows, so presence != fetched)

Reuses fetch_items.py's API etiquette (UA, maxlag, backoff, serial 50/batch). Needs
network access to www.wikidata.org (allow it).

    uv run fetch_anchor_langs.py            # fetch all uncached pool anchors
    uv run fetch_anchor_langs.py --limit 50 # smoke test one batch
"""

import argparse
import sqlite3
import time

import config
import fetch_items as fi  # reuse http_get + BATCH + SLEEP + etiquette
import review_queue as rq

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_lang(
    qid TEXT, lang TEXT, label TEXT, desc TEXT, PRIMARY KEY(qid, lang)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS item_lang_fetched(qid TEXT PRIMARY KEY);
"""


def needed_qids(cache):
    """Distinct anchor Q-ids of the review pool not yet fetched into item_lang."""
    want = sorted({r[1] for r in rq.pool()})
    have = {r[0] for r in cache.execute("SELECT qid FROM item_lang_fetched")}
    return [q for q in want if q not in have]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap items (smoke test)")
    args = ap.parse_args()

    cache = sqlite3.connect(config.ITEMS)
    cache.executescript(SCHEMA)

    todo = needed_qids(cache)
    if args.limit:
        todo = todo[: args.limit]
    print(f"anchor items to fetch (uncached): {len(todo):,}  -> {config.ITEMS}")
    if not todo:
        return

    done = 0
    for i in range(0, len(todo), fi.BATCH):
        batch = todo[i : i + fi.BATCH]
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
                rows.append((qid, lg, labels.get(lg, {}).get("value"),
                             descs.get(lg, {}).get("value")))
            ncov.append(len(langs))
        cache.executemany("INSERT OR REPLACE INTO item_lang VALUES (?,?,?,?)", rows)
        cache.executemany("INSERT OR REPLACE INTO item_lang_fetched VALUES (?)",
                          [(q,) for q in batch])
        cache.commit()
        done += len(batch)
        print(f"  {done:,}/{len(todo):,}  +{len(rows)} label rows "
              f"(langs/item: {min(ncov) if ncov else 0}-{max(ncov) if ncov else 0})")
        time.sleep(fi.SLEEP)

    tot = cache.execute("SELECT count(*) FROM item_lang").fetchone()[0]
    avg = cache.execute(
        "SELECT count(*)*1.0/count(DISTINCT qid) FROM item_lang").fetchone()[0]
    print(f"item_lang now holds {tot:,} rows, avg {avg:.0f} languages/item")
    cache.close()


if __name__ == "__main__":
    main()
