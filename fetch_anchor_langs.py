#!/usr/bin/env python3
"""Fetch all-language CC0 labels/descriptions + aliases for the anchor Q-items.

The review UI (review_queue.py) shows each anchor's label+description so a human can
judge the scope match. fetch_items.py only caches de/en/sv (the verifier's inputs);
this fills in the rest so the reviewer can cross-check a scope call against more
languages (e.g. a demonym vs its ethnic-group concept reads differently across langs).
It ALSO fetches each item's aliases ("also known as"), so the review UI can flag when a
lexeme's lemma exactly matches one of the concept's official names — a single
wbgetentities request returns labels+descriptions+aliases together, so the two are
fetched in lockstep and can't drift apart (one pass, not two).

Targets the +multi verifier's universe — the distinct anchor Q-ids of the ABD candidate
set (a superset of review_queue.pool()) — and fetches EVERY language (no `languages`
filter), so which subset the UI displays is a pure review_queue.py choice, changeable
without a re-fetch. Everything fetched is CC0.

Stored in data/wd_items.sqlite3 (alongside the de/en/sv `item` table):
  * item_lang(qid, lang, label, desc)  — one row per (item, language)
  * item_alias(qid, lang, aliases)     — '; '-joined aliases per (item, language)
  * item_lang_fetched(qid)             — resumability marker (an item may legitimately
  * item_alias_fetched(qid)              have zero rows, so presence != fetched)

Reuses fetch_items.py's API etiquette (UA, maxlag, backoff, serial 50/batch). Needs
network access to www.wikidata.org (allow it).

    uv run fetch_anchor_langs.py            # fetch all uncached anchors
    uv run fetch_anchor_langs.py --limit 50 # smoke test one batch
"""

import argparse
import sqlite3
import time

import config
import fetch_items as fi  # reuse http_get + BATCH + SLEEP + etiquette
import verify

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_lang(
    qid TEXT, lang TEXT, label TEXT, desc TEXT, PRIMARY KEY(qid, lang)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS item_alias(
    qid TEXT, lang TEXT, aliases TEXT, PRIMARY KEY(qid, lang)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS item_lang_fetched(qid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS item_alias_fetched(qid TEXT PRIMARY KEY);
"""


def needed_qids(cache):
    """Distinct anchor Q-ids of the production verify set (ABD candidates) not yet fetched.

    This is the +multi verifier's universe — every anchor it will score needs its all-language
    concept, so the multilingual format doesn't silently fall back to de/en/sv. An anchor counts
    as done only when BOTH its labels/descriptions AND its aliases are cached, so a cache built
    before aliases were added self-heals: those anchors re-fetch once (labels rewritten idempotently)."""
    args = argparse.Namespace(gate="ABD", min_support=1, senseless_only=False, all=True, limit=None)
    want = sorted({r[3] for r in verify.candidates(args) if verify.has_evidence(r)})
    lang_done = {r[0] for r in cache.execute("SELECT qid FROM item_lang_fetched")}
    alias_done = {r[0] for r in cache.execute("SELECT qid FROM item_alias_fetched")}
    done = lang_done & alias_done
    return [q for q in want if q not in done]


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
            "props": "labels|descriptions|aliases", "format": "json", "maxlag": 5,
        })
        ents = data.get("entities", {})
        rows, arows, ncov = [], [], []
        for qid in batch:
            ent = ents.get(qid, {})
            labels = ent.get("labels", {}) or {}
            descs = ent.get("descriptions", {}) or {}
            aliases = ent.get("aliases", {}) or {}
            langs = set(labels) | set(descs)
            for lg in langs:
                rows.append((qid, lg, labels.get(lg, {}).get("value"),
                             descs.get(lg, {}).get("value")))
            for lg, lst in aliases.items():
                vals = "; ".join(a["value"] for a in lst if a.get("value"))
                if vals:
                    arows.append((qid, lg, vals))
            ncov.append(len(langs))
        cache.executemany("INSERT OR REPLACE INTO item_lang VALUES (?,?,?,?)", rows)
        cache.executemany("INSERT OR REPLACE INTO item_alias VALUES (?,?,?)", arows)
        cache.executemany("INSERT OR REPLACE INTO item_lang_fetched VALUES (?)",
                          [(q,) for q in batch])
        cache.executemany("INSERT OR REPLACE INTO item_alias_fetched VALUES (?)",
                          [(q,) for q in batch])
        cache.commit()
        done += len(batch)
        print(f"  {done:,}/{len(todo):,}  +{len(rows)} label rows, +{len(arows)} alias rows "
              f"(langs/item: {min(ncov) if ncov else 0}-{max(ncov) if ncov else 0})")
        time.sleep(fi.SLEEP)

    tot = cache.execute("SELECT count(*) FROM item_lang").fetchone()[0]
    avg = cache.execute(
        "SELECT count(*)*1.0/count(DISTINCT qid) FROM item_lang").fetchone()[0]
    atot = cache.execute("SELECT count(*) FROM item_alias").fetchone()[0]
    aq = cache.execute("SELECT count(DISTINCT qid) FROM item_alias").fetchone()[0]
    print(f"item_lang now holds {tot:,} rows, avg {avg:.0f} languages/item; "
          f"item_alias {atot:,} rows across {aq:,} items")
    cache.close()


if __name__ == "__main__":
    main()
