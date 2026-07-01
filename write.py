#!/usr/bin/env python3
"""Write P5137 sense statements to Wikidata — gated on human approval.

Reads ONLY the rows the human approved in the decision log (config.DECISIONS,
status='approved') and, for each, builds a `wbeditentity` payload adding
P5137 → anchor Q to a sense, with a MANDATORY reference (never bare): based on
heuristic (P887) + retrieved (P813). The CC-BY-SA DBnary gloss is never written.
On success it appends a 'written' event (with the revid) to the same log.

    uv run write.py --dry-run                 # print exact payloads, submit nothing
    uv run write.py --dry-run --preview       # preview verifier-'yes' rows (no approval needed)
    uv run write.py --write --limit 10        # submit 10 approved edits (needs credentials)

Safety: --dry-run is the default. Live submit needs --write AND WIKI_USER/WIKI_PASS
env vars (a Special:BotPasswords credential on your operator account). Submission goes
through the MediaWiki Action API (wbeditentity) using only the standard library — no
extra dependency, and it posts the exact JSON shown in --dry-run. Edits are NOT
bot-flagged (assisted editing on the operator account). Pilot writes MONOSEMOUS targets
only (add to the existing sense); senseless targets need a gloss policy (--include-senseless).

Before each live edit the live lexeme is re-fetched: a monosemous target must still have
exactly one sense, and that sense must NOT already carry a P5137 (never override existing
data); a sense-creation target must not already have ANY sense carrying this P5137 (a
previous run may have created it but died before its 'written' event was logged) — else skip.
"""

import argparse
import datetime
import http.cookiejar
import json
import os
import random
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

import config
from fetch_items import API, USER_AGENT  # one copy of the endpoint + contact User-Agent
from review_queue import append_decision, load_decisions, now, pool

SUMMARY = ("Add [[Property:P5137]] (item for this sense) inferred from translation, "
           "ref [[Q140308675]] — LLM-verified, human-approved assisted edit")
TOOL = "CB"  # EditGroups "custom bot" id (self-service, no registration); batch id is per-run

# P887 "based on heuristic" value: the purpose-made item "inferred from translation"
# (created 2026-06 on the operator account; P31 -> heuristic Q201413, constraint-clean).
HEURISTIC_ITEM = "Q140308675"


def entity_snak(prop, qid):
    return {"snaktype": "value", "property": prop,
            "datavalue": {"type": "wikibase-entityid", "value": {"id": qid}}}


def build_statement(anchor_qid, today):
    """The P5137 claim dict with its mandatory reference (CC0-clean)."""
    time_snak = {"snaktype": "value", "property": "P813",
                 "datavalue": {"type": "time", "value": {
                     "time": f"+{today}T00:00:00Z", "precision": 11,
                     "timezone": 0, "before": 0, "after": 0,
                     "calendarmodel": "http://www.wikidata.org/entity/Q1985727"}}}
    return {
        "mainsnak": entity_snak("P5137", anchor_qid),
        "type": "statement", "rank": "normal",
        "references": [{"snaks": {
            "P887": [entity_snak("P887", HEURISTIC_ITEM)],  # based on heuristic
            "P813": [time_snak],                            # retrieved
        }}],
    }


def anchor_label(qid, lang):
    """CC0 label for a would-be new sense's gloss (target lang, fallback en)."""
    it = sqlite3.connect(config.ITEMS)
    # The wide `item` table only has de/en/sv columns; for any other target language
    # (fr/no) read its own-language label from the all-language item_lang cache, then
    # fall back to the en column. (Senseless-target writing is a gated phase-2 path.)
    own = None
    if lang in ("de", "en", "sv"):
        r = it.execute(f"SELECT label_{lang} FROM item WHERE qid=? AND NOT missing",
                       [qid]).fetchone()
        own = r[0] if r else None
    elif it.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='item_lang'"
                    ).fetchone():
        r = it.execute("SELECT label FROM item_lang WHERE qid=? AND lang=?",
                       [qid, lang]).fetchone()
        own = r[0] if r else None
    en = it.execute("SELECT label_en FROM item WHERE qid=? AND NOT missing", [qid]).fetchone()
    it.close()
    return own or (en[0] if en else None)


def build_edit(row, today, include_senseless):
    """Return (wbeditentity_data, note) for one approved candidate.

    Monosemous: add the claim to the lexeme's single existing sense (its S-id is
    resolved against the live lexeme at write time). Senseless: must create a
    sense, which needs a gloss — seeded from the anchor's own CC0 label."""
    lid, qid, lemma, lang, n_senses = row
    claim = build_statement(qid, today)

    if n_senses == 1:
        sense = {"id": f"<{lid}-S? resolved live>", "claims": {"P5137": [claim]}}
        return {"senses": [sense]}, "add to existing sense"
    if include_senseless:
        gloss = anchor_label(qid, lang)
        if not gloss:  # no CC0 anchor label -> never fall back to the headword (degenerate)
            return None, "skipped: senseless, no CC0 anchor label (no gloss source)"
        sense = {"add": "", "glosses": {lang: {"language": lang, "value": gloss}},
                 "claims": {"P5137": [claim]}}
        return {"senses": [sense]}, f"CREATE sense (gloss = CC0 anchor label: {gloss!r})"
    return None, "skipped: senseless (needs gloss policy; use --include-senseless)"


def load_rows(args):
    """(lid, anchor_qid, lemma, lang, n_senses) rows to build edits from.

    --preview reuses the review queue's verifier-'yes' High-tier pool directly
    (inspect before any approval). Otherwise read the rows the human approved from
    the decision log (the single source of truth); 'approved' is the current
    replayed status, so anything later written/rejected is naturally excluded."""
    if args.preview:
        return [r[:5] for r in pool()[: args.limit]]
    rows = [(d["target_lid"], d["anchor_qid"], d["target_lemma"], d["target_lang"],
             int(d["target_n_senses"]))
            for d in load_decisions().values() if d["status"] == "approved"]
    rows.sort(key=lambda r: (r[3], r[2]))
    return rows[: args.limit]


# ---- Action API submission (stdlib only) -----------------------------------

MAXLAG_RETRIES = 6  # back-off attempts on maxlag / 429 / 503 before giving up


def _api(opener, params, post=False):
    """One Action API call, honouring maxlag back-off (API:Etiquette).

    A maxlag rejection (JSON `error.code=maxlag`) or a 429 happens *before* the
    edit is applied, so retrying a POST is safe. A 503 or a bare network failure
    on a POST is NOT retried — an intermediary can return either after the edit
    already landed, and we must not risk a duplicate."""
    params = {**params, "format": "json", "formatversion": "2"}
    backoff = 1.0
    for attempt in range(MAXLAG_RETRIES):
        if post:
            req = urllib.request.Request(API, data=urllib.parse.urlencode(params).encode(),
                                         headers={"User-Agent": USER_AGENT})
        else:
            req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params),
                                         headers={"User-Agent": USER_AGENT})
        last = attempt == MAXLAG_RETRIES - 1
        try:
            with opener.open(req, timeout=30) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # 429 is a pre-processing reject, safe to retry even for a POST. A 503 is
            # not: an intermediary can emit it after MediaWiki already applied the
            # edit, so re-POSTing risks a duplicate — same rule as URLError below.
            if (e.code == 429 or (e.code == 503 and not post)) and not last:
                time.sleep(min(float(e.headers.get("Retry-After", backoff)), 30))
                backoff *= 2
                continue
            raise
        except urllib.error.URLError:
            if post or last:  # don't silently re-POST a possibly-applied edit
                raise
            time.sleep(min(backoff, 30))
            backoff *= 2
            continue
        if data.get("error", {}).get("code") == "maxlag" and not last:
            time.sleep(min(float(data["error"].get("lag", backoff)) or backoff, 30))
            backoff *= 2
            continue
        return data
    raise RuntimeError(f"maxlag back-off: gave up after {MAXLAG_RETRIES} attempts")


def login(opener, user, pw):
    """Log in with a bot password; return a CSRF edit token."""
    tok = _api(opener, {"action": "query", "meta": "tokens", "type": "login"})
    lt = tok["query"]["tokens"]["logintoken"]
    res = _api(opener, {"action": "login", "lgname": user, "lgpassword": pw, "lgtoken": lt},
               post=True)
    if res.get("login", {}).get("result") != "Success":
        raise SystemExit(f"login failed: {res.get('login', {})}")
    ct = _api(opener, {"action": "query", "meta": "tokens", "type": "csrf"})
    return ct["query"]["tokens"]["csrftoken"]


def live_senses(opener, lid):
    """Re-fetch the live lexeme; return its senses."""
    return _api(opener, {"action": "wbgetentities", "ids": lid})["entities"][lid].get("senses", [])


def p5137_targets(sense):
    """The anchor qids of a live sense's existing P5137 claims."""
    out = set()
    for c in sense.get("claims", {}).get("P5137", []):
        try:
            out.add(c["mainsnak"]["datavalue"]["value"]["id"])
        except (KeyError, TypeError):
            pass
    return out


def resolve_sense(opener, lid):
    """Re-fetch the live lexeme. Return (sense_id, existing P5137 target qids).
    Raises if the lexeme no longer has exactly one sense."""
    senses = live_senses(opener, lid)
    if len(senses) != 1:
        raise ValueError(f"{lid} now has {len(senses)} senses (expected 1)")
    return senses[0]["id"], p5137_targets(senses[0])


def submit(opener, csrf, lid, data, summary):
    res = _api(opener, {"action": "wbeditentity", "id": lid, "data": json.dumps(data),
                        "summary": summary, "token": csrf, "maxlag": "5", "bot": "0"},
               post=True)
    if "error" in res:
        raise RuntimeError(res["error"].get("info", str(res["error"])))
    return res.get("entity", {}).get("lastrevid")


def write_edits(edits, max_per_min):
    """Resolve each sense live, safety-check, submit, record the revid in the queue.

    Submissions are paced to at most `max_per_min` edits/minute (0 = no cap), the
    per-minute rate limit the bot policy requires editing tools to support."""
    user, pw = os.environ.get("WIKI_USER"), os.environ.get("WIKI_PASS")
    if not (user and pw):
        raise SystemExit("--write needs WIKI_USER / WIKI_PASS (Special:BotPasswords) env vars")
    if HEURISTIC_ITEM.startswith("Q-TODO"):
        raise SystemExit("refusing to write: pin HEURISTIC_ITEM to a real P887 value first")
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf = login(opener, user, pw)
    decisions = load_decisions()  # to carry the audited fields onto the 'written' event
    # One EditGroups batch per run: every edit shares this id, so the whole run is one
    # auditable, one-click-revertible group on editgroups.toolforge.org. Padded to 12 hex
    # digits to stay inside EditGroups' 4–32 lowercase-hex range for any random value.
    batch_id = "%012x" % random.randrange(2**48)
    summary = f"{SUMMARY} ([[:toolforge:editgroups/b/{TOOL}/{batch_id}|details]])"
    group_url = f"https://editgroups.toolforge.org/b/{TOOL}/{batch_id}/"
    interval = 60.0 / max_per_min if max_per_min > 0 else 0.0
    print(f"editgroups batch {TOOL}/{batch_id} → {group_url}")
    print(f"throttle: {'≤%d edits/min' % max_per_min if interval else 'none'}")
    written = 0
    last_edit = 0.0  # monotonic time of the last submitted edit, for pacing
    for lid, data in edits:
        sense = data["senses"][0]
        anchor = sense["claims"]["P5137"][0]["mainsnak"]["datavalue"]["value"]["id"]
        try:
            if "id" in sense:  # monosemous: resolve the live sense id + guard
                sid, p5137 = resolve_sense(opener, lid)
                if anchor in p5137:
                    print(f"  skip {lid}: already has P5137 → {anchor}"); continue
                if p5137:
                    print(f"  skip {lid}: sense already has P5137 {p5137} (never override)")
                    continue
                sense["id"] = sid
            else:
                # sense-creation: if ANY live sense already carries this P5137, a previous
                # run already created it (e.g. killed before its 'written' event was logged)
                if any(anchor in p5137_targets(s) for s in live_senses(opener, lid)):
                    print(f"  skip {lid}: a sense already has P5137 → {anchor}"); continue
            if interval and last_edit:  # pace only between actual submissions
                wait = interval - (time.monotonic() - last_edit)
                if wait > 0:
                    time.sleep(wait)
            revid = submit(opener, csrf, lid, data, summary)
            last_edit = time.monotonic()
        except Exception as e:  # noqa: BLE001 — one bad edit shouldn't abort the batch
            print(f"  FAIL {lid} → {anchor}: {e}"); continue
        d = decisions.get((lid, anchor), {})
        append_decision({
            "ts": now(), "target_lid": lid, "anchor_qid": anchor,
            "target_lemma": d.get("target_lemma", ""), "target_lang": d.get("target_lang", ""),
            "target_n_senses": d.get("target_n_senses", ""), "verdict": d.get("verdict", ""),
            "reason": d.get("reason", ""), "model": d.get("model", ""),
            "status": "written", "reviewer": f"write.py b/{TOOL}/{batch_id}",
            "revid": str(revid),
        })
        written += 1
        print(f"  wrote {lid} → {anchor}  (rev {revid})")
        print(f"    https://www.wikidata.org/wiki/Lexeme:{lid}")
        print(f"    https://www.wikidata.org/wiki/Special:Diff/{revid}")
    print(f"\n{written}/{len(edits)} submitted.")
    if written:
        print(f"editgroups batch → {group_url}")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True,
                   help="print payloads, submit nothing (default)")
    g.add_argument("--write", action="store_true", help="actually submit (needs credentials)")
    ap.add_argument("--preview", action="store_true",
                    help="read verifier-'yes' rows directly (inspect before any approval)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--max-edits-per-min", type=int, default=30,
                    help="cap submission rate (0 = no cap); default 30")
    ap.add_argument("--include-senseless", action="store_true",
                    help="also build sense-creation edits (gloss from CC0 label)")
    args = ap.parse_args()

    today = datetime.date.today().isoformat()
    rows = load_rows(args)
    src = "verifier-'yes' (preview)" if args.preview else "approved"
    print(f"{len(rows)} {src} candidate(s); mode={'WRITE' if args.write else 'dry-run'}; "
          f"reference=P887({HEURISTIC_ITEM})+P813({today})\n")

    edits = []
    for row in rows:
        data, note = build_edit(row, today, args.include_senseless)
        lid, qid, lemma, lang = row[0], row[1], row[2], row[3]
        print(f"# {lang} {lemma!r}  Lexeme:{lid} → {qid}   [{note}]")
        if data is None:
            continue
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print()
        edits.append((lid, data))

    if not args.write:
        print(f"dry-run: {len(edits)} edit(s) built, none submitted.")
        if HEURISTIC_ITEM.startswith("Q-TODO"):
            print("NOTE: P887 heuristic item is a placeholder — pin HEURISTIC_ITEM before writing.")
        return

    if args.preview:
        raise SystemExit("refusing to --write in --preview mode: write only human-approved rows")
    write_edits(edits, args.max_edits_per_min)


if __name__ == "__main__":
    main()
