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

FEED SCHEMA v1 — one JSON object per line. The service repo owns this contract
once it exists (plan §2); until then this docstring is the spec:
  sid    "L123|Q456|v1"   stable id (lid|qid|tile schema); the hub's log_action tile id
  lane   "mono"           senseless sense-creation is a later phase
  mode   "B"              log-only buttons — no api_action anywhere in the feed
  class  rel_class()      the calibration bucket the pair belongs to
  gold   null | "approved" | "rejected"      trap label (null = live suggestion)
  tile   {id, sections: [...], controls: [{type:'buttons', entries: [...]}]}
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
    """The hub-ready tile JSON for one pool row — same composers as the local UI
    (anchor_langs / alias_match / demonym_context gloss), so the crowd sees what the
    local reviewer sees, minus the scope machinery the keep-classes don't need."""
    lid, qid, lemma, lang = row[0], row[1], row[2], row[3]
    gloss, glang, _adesc = ctx.get((lid, qid), ("", None, ""))
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
    # The item section renders in the player's UI language only; the multilingual CC0
    # concept block is the cross-check that catches homographs (same reasoning as the
    # local queue's anchor_langs display).
    concept = "<br>".join(
        f"<small>{html.escape(str(disp).upper())}</small> <b>{html.escape(lab or '')}</b>"
        + (f" — {html.escape(ds)}" if ds else "")
        for disp, lab, ds in rq.anchor_langs(it, qid))
    aka = rq.alias_match(it, qid, lang, lemma) if have_alias else ""
    if aka:
        concept += (f"<br>“{html.escape(aka)}” is a listed “also known as” "
                    "name of this item")
    if concept:
        sections.append({"type": "html",
                         "title": "Concept (CC0 labels / descriptions)", "text": concept})
    return {"id": sid(lid, qid), "sections": sections,
            "controls": [{"type": "buttons", "entries": BUTTONS}]}


def build_meta(it, ctx, row, common):
    """The per-suggestion optimization snapshot (plan §7) — denormalized so later
    analysis joins votes against it without needing the regenerable DBs at this commit."""
    (lid, qid, lemma, lang, n_senses, verdict, reason, _model,
     dde, den, dsv, support, prompt_ver) = row
    gloss = ctx.get((lid, qid), ("", None, ""))[0] or ""
    rel, _kind = rq.scope_kind(reason, gloss, rq.anchor_desc(dde, den, dsv))
    return dict(common,
                support_clusters=support,
                n_anchor_langs=len(rq.anchor_langs(it, qid)),
                alias_match=bool(rq.alias_match(it, qid, lang, lemma)),
                verdict=verdict, relation=rel, reason=reason,
                model=_model, prompt_ver=prompt_ver,
                lang=lang, lemma=lemma, n_senses=n_senses,
                gloss_len=len(gloss.split()) if gloss else 0,
                gloss_undiff=rq.undifferentiated_gloss(gloss, lemma))


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
    assert set(rec) == {"sid", "lane", "mode", "class", "gold", "tile", "meta"}, rec.keys()
    assert rec["sid"] not in seen, f"duplicate sid {rec['sid']}"
    assert rec["mode"] == "B" and rec["lane"] == "mono"
    assert rec["gold"] in (None, "approved", "rejected")
    if rec["gold"] != "rejected":   # rejected traps may carry any class (see build)
        assert rec["class"] in CROWD_CLASSES
    tile = rec["tile"]
    assert tile["id"] == rec["sid"] and tile["sections"] and tile["controls"]
    for entry in tile["controls"][0]["entries"]:
        assert "api_action" not in entry, "Mode B feed must not carry edits"
    assert no_gold_key([tile, rec["meta"]]), "trap label leaked into content"
    seen.add(rec["sid"])


def build(langs=None, limit=0, trap_frac=0.15):
    rows = rq.pool(senseless=False)
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
               "class": rq.rel_of((r[0], r[1]), r[6], ctx), "gold": gold,
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
    for rec in recs:
        rec["meta"]["feed_version"] = version
    path = os.path.join(FEED_DIR, f"feed_v{version:03d}.jsonl.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for rec in recs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_lang = collections.Counter(r["meta"]["lang"] for r in recs)
    by_class = collections.Counter(r["class"] for r in recs)
    print(f"wrote {len(recs):,} tiles -> {path}")
    print(f"  live {n_live:,} · traps {n_traps:,} "
          f"({sum(1 for r in recs if r['gold'] == 'rejected')} rejected / "
          f"{sum(1 for r in recs if r['gold'] == 'approved')} approved)")
    print("  by lang:  " + " · ".join(f"{k} {v}" for k, v in sorted(by_lang.items())))
    print("  by class: " + " · ".join(f"{k} {v}" for k, v in sorted(by_class.items())))


if __name__ == "__main__":
    main()
