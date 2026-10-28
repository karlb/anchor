#!/usr/bin/env python3
"""Fetch P31/P279 for the anchor Q-items.

Reads the distinct anchor Q-ids from candidates.sqlite3 and fetches, from the
Wikidata Action API (``wbgetentities``), each item's:
  * P31 (instance of) + P279 (subclass of) targets -> item_type, the STRUCTURAL,
    language-independent data behind gate D (concrete anchor) and the scope filter.
  * existence -> the `item` table (`missing` = 1 if the API has no such entity).
Labels and descriptions come from fetch_anchor_langs.py (item_lang, all languages).

Everything fetched is CC0 (Wikidata's own data). Results are cached in
``data/wd_items.sqlite3`` keyed by Q-id, so the script is fully resumable and a
re-run fetches nothing already stored.

Good-API-citizen behaviour (Wikimedia API etiquette):
  * descriptive User-Agent with a contact address;
  * serial requests only (no parallelism), 50 ids per batch;
  * honour ``Retry-After`` and exponential backoff on 429/503;
  * gzip transfer; a small inter-batch sleep;
  * maxlag=5 on every request (Manual:Maxlag: "noninteractive tasks should always use it").
    Wikidata's lag includes the Query Service's, so a WDQS backlog stalls a fetch until it
    clears (2026-08-02: refused for hours at queryserviceLag 2899) — accepted for compliance.

Run after match.py (allow www.wikidata.org):
    uv run fetch_items.py            # fetch all uncached anchor items
    uv run fetch_items.py --limit 50 # smoke test one batch
"""

import argparse
import gzip
import io
import json
import sqlite3
import time
import urllib.parse
import urllib.request

import config

API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "Karlb42Bot/0.1 (https://www.wikidata.org/wiki/User:Karlb42Bot; karl42@gmail.com) python-urllib"
BATCH = 50
SLEEP = 0.2          # polite gap between successful batches (s)
MAX_RETRIES = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS item(
    qid TEXT PRIMARY KEY,
    missing INT DEFAULT 0          -- 1 if the API returned no such entity
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS item_type(qid TEXT, prop TEXT, value TEXT);
CREATE INDEX IF NOT EXISTS item_type_qid ON item_type(qid);
"""


def http_get(params):
    """GET with gzip + rate-limit-aware retry/backoff. Returns parsed JSON.

    A maxlag rejection waits and retries without limit: lag is the server asking us to
    pause, not a failure, so it does not use up MAX_RETRIES."""
    url = API + "?" + urllib.parse.urlencode({"maxlag": "5", **params})
    backoff = 1.0
    attempt = 0
    while attempt < MAX_RETRIES:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                data = json.loads(raw)
            if isinstance(data, dict) and data.get("error", {}).get("code") == "maxlag":
                lag = float(data["error"].get("lag") or 5)
                print(f"  maxlag: server {lag:.0f}s behind, waiting", flush=True)
                time.sleep(min(max(lag, 5), 60))
                continue
            if isinstance(data, dict) and "error" in data:
                # any other API error: fail loudly — the response has no 'entities', so
                # returning it would make callers permanently cache the whole batch as
                # missing (item) or fetched-with-no-labels (item_lang/item_alias)
                raise RuntimeError(f"API error: {data['error']}")
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                wait = float(e.headers.get("Retry-After", backoff))
                time.sleep(min(wait, 30))
                backoff *= 2
                attempt += 1
                continue
            raise
        except urllib.error.URLError:
            time.sleep(backoff)
            backoff *= 2
            attempt += 1
    raise RuntimeError(f"giving up after {MAX_RETRIES} retries: {url}")


def parse_types(qid, ent):
    """[(qid, prop, value), ...] P31/P279 edges of a (non-missing) entity blob."""
    types = []
    claims = ent.get("claims", {})
    for prop in ("P31", "P279"):
        for c in claims.get(prop, []):
            snak = c.get("mainsnak", {})
            if snak.get("snaktype") != "value":
                continue
            v = snak.get("datavalue", {}).get("value", {}).get("id")
            if v:
                types.append((qid, prop, v))
    return types


def fetch_batch(ids):
    data = http_get({
        "action": "wbgetentities",
        "ids": "|".join(ids),
        "props": "claims",
        "format": "json",
    })
    return data.get("entities", {})


def needed_qids(cache):
    # superset: every Q appearing as a clean anchor. Covers both the candidate
    # anchors (verifier) and every held-out true/predicted Q (precision tolerance).
    src = sqlite3.connect(config.CANDIDATES)
    want = [r[0] for r in src.execute("SELECT DISTINCT qid FROM clean_anchor")]
    src.close()
    have = {r[0] for r in cache.execute("SELECT qid FROM item")}
    return [q for q in want if q not in have]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap number of items (smoke test)")
    args = ap.parse_args()

    cache = sqlite3.connect(config.ITEMS)
    cache.executescript(SCHEMA)

    todo = needed_qids(cache)
    if args.limit:
        todo = todo[: args.limit]
    print(f"items to fetch (uncached): {len(todo):,}")
    if not todo:
        return

    done = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        entities = fetch_batch(batch)
        rows, types, missing = [], [], []
        for qid in batch:
            ent = entities.get(qid)
            if ent is None or "missing" in ent:
                missing.append((qid,))
                continue
            rows.append((qid,))
            types.extend(parse_types(qid, ent))
        cache.executemany("INSERT OR REPLACE INTO item(qid) VALUES (?)", rows)
        cache.executemany("INSERT OR REPLACE INTO item(qid,missing) VALUES (?,1)", missing)
        cache.executemany("INSERT INTO item_type VALUES (?,?,?)", types)
        cache.commit()
        done += len(batch)
        print(f"  {done:,}/{len(todo):,}  (+{len(rows)} items, {len(missing)} missing)")
        time.sleep(SLEEP)

    n_items = cache.execute("SELECT count(*) FROM item WHERE NOT missing").fetchone()[0]
    n_types = cache.execute("SELECT count(*) FROM item_type").fetchone()[0]
    print(f"cache now holds {n_items:,} items, {n_types:,} P31/P279 edges")
    cache.close()


if __name__ == "__main__":
    main()
