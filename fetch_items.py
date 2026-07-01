#!/usr/bin/env python3
"""Fetch CC0 labels/descriptions + P31/P279 for the anchor Q-items.

Reads the distinct anchor Q-ids from candidates.sqlite3 and fetches, from the
Wikidata Action API (``wbgetentities``), each item's:
  * CC0 label + description in de/en/sv  -> inputs for the LLM verifier;
  * P31 (instance of) + P279 (subclass of) targets -> for the P279-hop
    tolerance in the precision re-measure.

Everything fetched is CC0 (Wikidata's own data). Results are cached in
``data/wd_items.sqlite3`` keyed by Q-id, so the script is fully resumable and a
re-run fetches nothing already stored.

Good-API-citizen behaviour (Wikimedia API etiquette):
  * descriptive User-Agent with a contact address;
  * serial requests only (no parallelism), 50 ids per batch;
  * ``maxlag=5`` so we back off when the servers are under replication lag;
  * honour ``Retry-After`` and exponential backoff on 429/503/maxlag;
  * gzip transfer; a small inter-batch sleep.

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
USER_AGENT = "anchor-p5137-pilot/0.1 (https://www.wikidata.org/; karl42@gmail.com) python-urllib"
LANGS = ("de", "en", "sv")
BATCH = 50
SLEEP = 0.2          # polite gap between successful batches (s)
MAX_RETRIES = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS item(
    qid TEXT PRIMARY KEY,
    label_de TEXT, label_en TEXT, label_sv TEXT,
    desc_de TEXT, desc_en TEXT, desc_sv TEXT,
    missing INT DEFAULT 0          -- 1 if the API returned no such entity
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS item_type(qid TEXT, prop TEXT, value TEXT);
CREATE INDEX IF NOT EXISTS item_type_qid ON item_type(qid);
"""


def http_get(params):
    """GET with gzip + maxlag-aware retry/backoff. Returns parsed JSON."""
    url = API + "?" + urllib.parse.urlencode(params)
    backoff = 1.0
    for attempt in range(MAX_RETRIES):
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
                wait = float(data["error"].get("lag", backoff)) or backoff
                time.sleep(min(wait, 30))
                backoff *= 2
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
                continue
            raise
        except urllib.error.URLError:
            time.sleep(backoff)
            backoff *= 2
    raise RuntimeError(f"giving up after {MAX_RETRIES} retries: {url}")


def parse_entity(qid, ent):
    """Extract (label/desc row, [(qid, prop, value), ...]) from an entity blob."""
    if ent.get("missing") is not None and "id" not in ent:
        return ("missing", None)
    labels = ent.get("labels", {})
    descs = ent.get("descriptions", {})
    row = [qid]
    row += [labels.get(l, {}).get("value") for l in LANGS]
    row += [descs.get(l, {}).get("value") for l in LANGS]
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
    return (row, types)


def fetch_batch(ids):
    data = http_get({
        "action": "wbgetentities",
        "ids": "|".join(ids),
        "props": "labels|descriptions|claims",
        "languages": "|".join(LANGS),
        "format": "json",
        "maxlag": 5,
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
            if ent is None or (ent.get("missing") is not None and "labels" not in ent):
                missing.append((qid,))
                continue
            row, ts = parse_entity(qid, ent)
            if row == "missing":
                missing.append((qid,))
                continue
            rows.append(row)
            types.extend(ts)
        cache.executemany(
            "INSERT OR REPLACE INTO item(qid,label_de,label_en,label_sv,desc_de,desc_en,desc_sv) "
            "VALUES (?,?,?,?,?,?,?)", rows)
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
