#!/usr/bin/env python3
"""Export the crowd-review suggestion feed (Distributed Game tiles).

The generation side of the crowd-review boundary (notes/crowd_review_plan.md §2-3):
the ONLY producer of what the Toolforge tile spooler serves. Tiles are fully
pre-rendered here — sections, buttons, wording — so the service stays a dumb spooler
with no P5137 semantics; any eligibility or copy change ships as a new feed version,
diffable in this repo.

Eligible pool = the crowd slice of review_queue.pool(): verifier-'yes' High tier,
keep-classes only (EQUAL / BROADER·convention / NARROWER·convention — the
confirmation work; scope triage stays in the local queue), no polysemy-collapse
flag, not yet decided. Trap tiles are already-decided pairs passing the SAME
filters, labelled gold=approved/rejected ('written' folds into approved) — content
indistinguishable from live tiles; the importer scores crowd accuracy on them and
never promotes them. Every run re-checks that no gold label leaks into tile/meta.

    uv run game_export.py                          # full pool -> data/game_feed/feed_vNNN.jsonl.gz
    uv run game_export.py --langs de,en,sv --limit 200   # the Phase-2 pilot slice

FEED SCHEMA v1 — one JSON object per line. The service repo OWNS the contract
(config.GAME_REPO: CONTRACT.md + schema/feed_row.schema.json); every row is
validated against that schema file at export time (schema_check below) — producer
asserts, consumer owns the spec. Summary:
  sid    "L123|Q456|v1"   stable id (lid|qid|tile schema); the hub's log_action tile id
  lane   "mono"           senseless sense-creation is a later phase
  mode   "B"              log-only buttons — no api_action anywhere in the feed
  class  rel_class()      the calibration bucket the pair belongs to
  lang   target language, top level — the service matches ?action=tiles&lang=xx
                          against it and must not peek into meta for it
  gold   null | "approved" | "rejected"      trap label (null = live suggestion)
  tile   {sections: [...], controls: [{type:'buttons', entries: [...]}]}
                          WITHOUT id — the service adds id=sid when serving
  meta   optimization snapshot (plan §7) — opaque to the service
"""

import argparse
import collections
import gzip
import html
import json
import os
import re
import sqlite3
import subprocess

import config
import review_queue as rq

FEED_DIR = os.path.join(config.DATA, "game_feed")
TILE_SCHEMA = "v1"
# The crowd gets confirmation work only: classes whose suggested action is 'keep'.
# Defer/reject classes (scope triage) stay in the local expert queue — plan §6.
CROWD_CLASSES = ("EQUAL", "BROADER·convention", "NARROWER·convention")
BUTTONS = [  # Mode B: log-only — deliberately NO api_action on any entry
    {"type": "green", "decision": "yes", "label": "match"},
    {"type": "white", "decision": "skip", "label": "can't tell"},
    {"type": "blue", "decision": "no", "label": "no match"},
]


def sid(lid, qid):
    return f"{lid}|{qid}|{TILE_SCHEMA}"


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.HERE,
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def next_version(outdir):
    vs = [int(m.group(1)) for f in os.listdir(outdir)
          if (m := re.match(r"feed_v(\d+)\.jsonl\.gz$", f))]
    return max(vs, default=0) + 1


def spaced(seq, n):
    """n items evenly spread over seq (deterministic — no RNG in the pipeline)."""
    if n <= 0 or not seq:
        return []
    if n >= len(seq):
        return list(seq)
    step = len(seq) / n
    return [seq[int(i * step)] for i in range(n)]


def build_tile(it, have_alias, ctx, row):
    """The hub-ready tile JSON for one pool row. The anchor item is NOT re-described —
    the hub's item section already renders it in detail (user call 2026-07-07 dropped
    the multilingual CC0 block). The verifier's free-text reason stays OUT of the tile
    deliberately: every live tile is verifier-'yes', so the reason always argues
    'match' — showing it anchors players toward yes (degrading the independent signal
    plan §7 measures) and on rejected traps it leaks the expected answer via the
    relation token. Only DETERMINISTIC notes ship, and they derive from the class/data,
    never from gold, so traps stay indistinguishable: the alias hit (neutral evidence
    the frontend won't compute) and the convention note (task definition — without it
    honest players mark gendered/demonym forms 'no match' against the blessed
    convention)."""
    lid, qid, lemma, lang = row[0], row[1], row[2], row[3]
    gloss, glang, adesc = ctx.get((lid, qid), ("", None, ""))
    lname = config.LANGNAME.get(lang, lang)
    word = (f"<b>{html.escape(lemma)}</b> <small>[{html.escape(lname)}]</small> — "
            f"<a href='https://www.wikidata.org/wiki/Lexeme:{lid}' target='_blank'>{lid}</a>")
    if gloss:
        gtag = f" [{glang}]" if glang and glang != lang else ""
        word += f"<br>sense: “{html.escape(gloss)}”{gtag}"
    else:
        word += "<br><i>(no sense gloss on the lexeme)</i>"
    sections = [{"type": "html", "title": "Word sense", "text": word},
                {"type": "item", "q": qid}]
    notes = []
    aka = rq.alias_match(it, qid, lang, lemma) if have_alias else ""
    if aka:
        notes.append(f"“{html.escape(aka)}” is a listed “also known as” "
                     "name of this item.")
    if rq.scope_kind(row[6], gloss, adesc)[1] == "convention":
        notes.append("The word is a gendered or inhabitant/demonym form. By Wikidata "
                     "convention it still links to the neutral concept — if that is "
                     "the only difference, it counts as a match.")
    if notes:
        sections.append({"type": "html", "title": "Notes", "text": "<br>".join(notes)})
    # No id here: the contract has the service stamp id=sid at serve time.
    return {"sections": sections,
            "controls": [{"type": "buttons", "entries": BUTTONS}]}


def build_meta(it, ctx, row, common):
    """The per-suggestion optimization snapshot (plan §7) — denormalized so later
    analysis joins votes against it without needing the regenerable DBs at this commit."""
    (lid, qid, lemma, lang, n_senses, verdict, reason, _model,
     support, prompt_ver, _sid) = row
    gloss, _glang, adesc = ctx.get((lid, qid), ("", None, ""))
    rel, _kind = rq.scope_kind(reason, gloss or "", adesc)
    return dict(common,
                support_clusters=support,
                n_anchor_langs=len(rq.anchor_langs(it, qid)),
                alias_match=bool(rq.alias_match(it, qid, lang, lemma)),
                verdict=verdict, relation=rel, reason=reason,
                model=_model, prompt_ver=prompt_ver,
                lang=lang, lemma=lemma, n_senses=n_senses,
                gloss_len=len(gloss.split()) if gloss else 0,
                gloss_undiff=rq.undifferentiated_gloss(gloss, lemma))


# ---- contract validation (plan §2's schema-drift mitigation) -----------------
# The service repo owns schema/feed_row.schema.json; we validate every exported row
# against that FILE, so a contract change over there is enforced here on the next
# export with no code sync. No jsonschema dependency (Phase 1: none) — this
# implements exactly the keyword subset the contract uses and hard-fails on any
# other, so the schema can never silently outgrow the validator.

_ANNOTATIONS = {"$schema", "$id", "$comment", "title", "description"}
_KEYWORDS = {"type", "required", "properties", "additionalProperties", "enum", "const",
             "pattern", "minLength", "minItems", "items", "allOf", "if", "then", "not"}
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def _satisfies(inst, schema, path):
    try:
        schema_check(inst, schema, path)
        return True
    except ValueError:
        return False


def schema_check(inst, schema, path="$"):
    """Raise ValueError at the first violation of the (subset) JSON Schema."""
    unknown = set(schema) - _KEYWORDS - _ANNOTATIONS
    if unknown:
        raise ValueError(f"{path}: schema keyword(s) {sorted(unknown)} not implemented — "
                         "extend schema_check to keep matching the contract")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPE_CHECKS[t](inst) for t in types):
            raise ValueError(f"{path}: expected {schema['type']}, got {type(inst).__name__}")
    if "enum" in schema and inst not in schema["enum"]:
        raise ValueError(f"{path}: {inst!r} not in {schema['enum']}")
    if "const" in schema and inst != schema["const"]:
        raise ValueError(f"{path}: {inst!r} != {schema['const']!r}")
    if isinstance(inst, str):
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            raise ValueError(f"{path}: {inst!r} fails pattern {schema['pattern']!r}")
        if len(inst) < schema.get("minLength", 0):
            raise ValueError(f"{path}: shorter than minLength {schema['minLength']}")
    if isinstance(inst, dict):
        for k in schema.get("required", ()):
            if k not in inst:
                raise ValueError(f"{path}: missing required key {k!r}")
        for k, sub in schema.get("properties", {}).items():
            if k in inst:
                schema_check(inst[k], sub, f"{path}.{k}")
        ap = schema.get("additionalProperties", True)
        if ap not in (True, False):
            raise ValueError(f"{path}: non-boolean additionalProperties not implemented")
        if ap is False and (extra := set(inst) - set(schema.get("properties", {}))):
            raise ValueError(f"{path}: unexpected key(s) {sorted(extra)}")
    if isinstance(inst, list):
        if len(inst) < schema.get("minItems", 0):
            raise ValueError(f"{path}: fewer than minItems {schema['minItems']}")
        if "items" in schema:
            for j, v in enumerate(inst):
                schema_check(v, schema["items"], f"{path}[{j}]")
    for sub in schema.get("allOf", ()):
        schema_check(inst, sub, path)
    if "if" in schema and _satisfies(inst, schema["if"], path) and "then" in schema:
        schema_check(inst, schema["then"], path)
    if "not" in schema and _satisfies(inst, schema["not"], path):
        raise ValueError(f"{path}: matches a forbidden ('not') schema")


def feed_schema():
    path = os.path.join(config.GAME_REPO, "schema", "feed_row.schema.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"feed contract not found: {path} — clone the service repo "
                         "beside this one or set ANCHOR_GAME_REPO")


def no_gold_key(obj):
    """No dict anywhere in obj uses the key 'gold' (substring matching would
    false-positive on lemmas/glosses that merely contain the word)."""
    if isinstance(obj, dict):
        return all(k != "gold" and no_gold_key(v) for k, v in obj.items())
    if isinstance(obj, list):
        return all(no_gold_key(v) for v in obj)
    return True


def check(rec, seen):
    """Feed-contract self-check, run on every row (the plan's always-on unit check):
    schema shape, sid uniqueness, Mode B has no api_action, and the trap label never
    leaks below the top level."""
    assert set(rec) == {"sid", "lane", "mode", "class", "lang", "gold", "tile", "meta"}, \
        rec.keys()
    assert rec["sid"] not in seen, f"duplicate sid {rec['sid']}"
    assert rec["mode"] == "B" and rec["lane"] == "mono"
    assert rec["lang"] and rec["lang"] == rec["meta"]["lang"]
    assert rec["gold"] in (None, "approved", "rejected")
    if rec["gold"] != "rejected":   # rejected traps may carry any class (see build)
        assert rec["class"] in CROWD_CLASSES
    tile = rec["tile"]
    assert set(tile) == {"sections", "controls"} and tile["sections"] and tile["controls"]
    for entry in tile["controls"][0]["entries"]:
        assert "api_action" not in entry, "Mode B feed must not carry edits"
    assert no_gold_key([tile, rec["meta"]]), "trap label leaked into content"
    seen.add(rec["sid"])


def build(langs=None, limit=0, trap_frac=0.15):
    rows = rq.pool(lane="mono")
    decisions = rq.load_decisions()
    ctx = rq.demonym_context([(r[0], r[1], r[3]) for r in rows])
    it = sqlite3.connect(config.ITEMS)
    if not rq.has_item_lang(it):
        raise SystemExit("no item_lang concept cache — run fetch_anchor_langs.py first")
    have_alias = rq.has_item_alias(it)
    cand = sqlite3.connect(config.CANDIDATES)
    poly = rq.polysemy_index(cand)
    cand.close()

    live, traps_ok, traps_no = [], [], []
    for r in rows:
        lid, qid, lemma, lang = r[0], r[1], r[2], r[3]
        if langs and lang not in langs:
            continue
        key = (lid, qid)
        keep = rq.rel_of(key, r[6], ctx) in CROWD_CLASSES
        status = (decisions.get(key) or {}).get("status", "")
        # Rejected traps come from ANY class: the human's confident 'no' is the gold
        # label, the class is invisible in the tile, and keep-class rejections are too
        # rare (~1 in the whole log) to catch yes-spammers on their own. Live tiles and
        # approved traps stay keep-class so the crowd only ever confirms, never triages.
        if status == "rejected":
            traps_no.append(r)
            continue
        if not keep:
            continue
        gloss = ctx.get(key, ("", None, ""))[0]
        if rq.polysemy_hint(poly, lid, qid, lang, lemma, gloss):
            continue
        if not status:
            live.append(r)
        elif status in rq.CONFIRMED:
            traps_ok.append(r)
        # 'deferred' pairs are neither live nor trap: a hold is not a gold verdict

    if limit:
        live = spaced(live, limit)
    # traps ≈ trap_frac of the total feed, split half rejected (catches yes-spam) /
    # half approved (catches no-spam and random clicking), each side backfilling the
    # other when its material runs short; evenly spaced over each bucket.
    n_traps = int(round(len(live) * trap_frac / (1.0 - trap_frac)))
    n_no = min(len(traps_no), (n_traps + 1) // 2)
    n_ok = min(len(traps_ok), n_traps - n_no)
    n_no = min(len(traps_no), n_traps - n_ok)
    traps = [(r, "rejected") for r in spaced(traps_no, n_no)]
    traps += [(r, "approved") for r in spaced(traps_ok, n_ok)]

    common = {"feed_version": None, "commit": git_commit(), "tile_schema": TILE_SCHEMA,
              "exported": rq.now(), "tier": "high"}
    out, seen = [], set()
    for r, gold in sorted([(r, None) for r in live] + traps,
                          key=lambda x: (x[0][3], x[0][2], x[0][0], x[0][1])):
        rec = {"sid": sid(r[0], r[1]), "lane": "mono", "mode": "B",
               "class": rq.rel_of((r[0], r[1]), r[6], ctx), "lang": r[3], "gold": gold,
               "tile": build_tile(it, have_alias, ctx, r),
               "meta": build_meta(it, ctx, r, common)}
        check(rec, seen)
        out.append(rec)
    it.close()
    return out, len(live), len(traps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--langs", help="comma-separated target languages (default: all)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the LIVE suggestions, evenly spread over the pool (pilot slice)")
    ap.add_argument("--trap-frac", type=float, default=0.15,
                    help="trap tiles as a fraction of the total feed (default 0.15)")
    ap.add_argument("--version", type=int, default=0,
                    help="feed version number (default: next after data/game_feed/)")
    args = ap.parse_args()

    langs = tuple(args.langs.split(",")) if args.langs else None
    recs, n_live, n_traps = build(langs, args.limit, args.trap_frac)
    if not recs:
        raise SystemExit("nothing eligible — is the review queue empty?")

    os.makedirs(FEED_DIR, exist_ok=True)
    version = args.version or next_version(FEED_DIR)
    schema = feed_schema()
    for rec in recs:
        rec["meta"]["feed_version"] = version
        try:
            schema_check(rec, schema)
        except ValueError as e:
            raise SystemExit(f"{rec['sid']} violates the feed contract: {e}")
    path = os.path.join(FEED_DIR, f"feed_v{version:03d}.jsonl.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for rec in recs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_lang = collections.Counter(r["lang"] for r in recs)
    by_class = collections.Counter(r["class"] for r in recs)
    print(f"wrote {len(recs):,} tiles -> {path}")
    print(f"  live {n_live:,} · traps {n_traps:,} "
          f"({sum(1 for r in recs if r['gold'] == 'rejected')} rejected / "
          f"{sum(1 for r in recs if r['gold'] == 'approved')} approved)")
    print("  by lang:  " + " · ".join(f"{k} {v}" for k, v in sorted(by_lang.items())))
    print("  by class: " + " · ".join(f"{k} {v}" for k, v in sorted(by_class.items())))
    # Shipping is two files, not one, and the second is easy to forget: a stale
    # mute list lets spam retire tiles again (CONTRACT.md "Mute list").
    print(f"\nto ship:  cp {path} {os.path.join(config.GAME_REPO, 'feeds')}/")
    print("          uv run import_crowd.py        # refreshes feeds/muted.txt")
    print("          git -C ../anchor-game add feeds && git -C ../anchor-game commit"
          " && git -C ../anchor-game push piku master")


if __name__ == "__main__":
    main()
