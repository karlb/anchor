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

Before each live edit the live lexeme is re-fetched: it must still have exactly one sense,
and that sense must NOT already carry a P5137 (never override existing data) — else skip.
"""

import argparse
import datetime
import http.cookiejar
import json
import os
import sqlite3
import urllib.parse
import urllib.request

import config
from review_queue import append_decision, load_decisions, now, pool

API = "https://www.wikidata.org/w/api.php"
USER_AGENT = ("anchor-p5137-pilot/0.1 (https://www.wikidata.org/; karl42@gmail.com) "
             "python-urllib")
SUMMARY = ("Add [[Property:P5137]] (item for this sense) inferred from translation, "
           "ref [[Q140308675]] — LLM-verified, human-approved assisted edit")

# P887 "based on heuristic" value: the purpose-made item "inferred from translation"
# (created 2026-06 on the operator account; P31 -> heuristic Q201413, constraint-clean).
HEURISTIC_ITEM = "Q140308675"


def build_statement(anchor_qid, today):
    """The P5137 claim dict with its mandatory reference (CC0-clean)."""
    def entity_snak(prop, qid):
        return {"snaktype": "value", "property": prop,
                "datavalue": {"type": "wikibase-entityid",
                              "value": {"id": qid}}}
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
    r = it.execute(f"SELECT label_{lang}, label_en FROM item WHERE qid=? AND NOT missing",
                   [qid]).fetchone()
    it.close()
    return (r[0] or r[1]) if r else None


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

def _api(opener, params, post=False):
    params = {**params, "format": "json", "formatversion": "2"}
    if post:
        req = urllib.request.Request(API, data=urllib.parse.urlencode(params).encode(),
                                     headers={"User-Agent": USER_AGENT})
    else:
        req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read().decode())


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


def resolve_sense(opener, lid):
    """Re-fetch the live lexeme. Return (sense_id, {existing P5137 qids}).
    Raises if the lexeme no longer has exactly one sense."""
    ent = _api(opener, {"action": "wbgetentities", "ids": lid})["entities"][lid]
    senses = ent.get("senses", [])
    if len(senses) != 1:
        raise ValueError(f"{lid} now has {len(senses)} senses (expected 1)")
    s = senses[0]
    existing = set()
    for c in s.get("claims", {}).get("P5137", []):
        try:
            existing.add(c["mainsnak"]["datavalue"]["value"]["id"])
        except (KeyError, TypeError):
            pass
    return s["id"], existing


def submit(opener, csrf, lid, data):
    res = _api(opener, {"action": "wbeditentity", "id": lid, "data": json.dumps(data),
                        "summary": SUMMARY, "token": csrf, "maxlag": "5", "bot": "0"},
               post=True)
    if "error" in res:
        raise RuntimeError(res["error"].get("info", str(res["error"])))
    return res.get("entity", {}).get("lastrevid")


def write_edits(edits):
    """Resolve each sense live, safety-check, submit, record the revid in the queue."""
    user, pw = os.environ.get("WIKI_USER"), os.environ.get("WIKI_PASS")
    if not (user and pw):
        raise SystemExit("--write needs WIKI_USER / WIKI_PASS (Special:BotPasswords) env vars")
    if HEURISTIC_ITEM.startswith("Q-TODO"):
        raise SystemExit("refusing to write: pin HEURISTIC_ITEM to a real P887 value first")
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf = login(opener, user, pw)
    decisions = load_decisions()  # to carry the audited fields onto the 'written' event
    written = 0
    for lid, data in edits:
        sense = data["senses"][0]
        anchor = sense["claims"]["P5137"][0]["mainsnak"]["datavalue"]["value"]["id"]
        try:
            if "id" in sense:  # monosemous: resolve the live sense id + guard
                sid, existing = resolve_sense(opener, lid)
                if anchor in existing:
                    print(f"  skip {lid}: already has P5137 → {anchor}"); continue
                if existing:
                    print(f"  skip {lid}: sense already has P5137 {existing} (never override)")
                    continue
                sense["id"] = sid
            revid = submit(opener, csrf, lid, data)
        except Exception as e:  # noqa: BLE001 — one bad edit shouldn't abort the batch
            print(f"  FAIL {lid} → {anchor}: {e}"); continue
        d = decisions.get((lid, anchor), {})
        append_decision({
            "ts": now(), "target_lid": lid, "anchor_qid": anchor,
            "target_lemma": d.get("target_lemma", ""), "target_lang": d.get("target_lang", ""),
            "target_n_senses": d.get("target_n_senses", ""), "verdict": d.get("verdict", ""),
            "reason": d.get("reason", ""), "model": d.get("model", ""),
            "status": "written", "reviewer": "write.py", "revid": str(revid),
        })
        written += 1
        print(f"  wrote {lid} → {anchor}  (rev {revid})")
        print(f"    https://www.wikidata.org/wiki/Lexeme:{lid}")
        print(f"    https://www.wikidata.org/wiki/Special:Diff/{revid}")
    print(f"\n{written}/{len(edits)} submitted.")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True,
                   help="print payloads, submit nothing (default)")
    g.add_argument("--write", action="store_true", help="actually submit (needs credentials)")
    ap.add_argument("--preview", action="store_true",
                    help="read verifier-'yes' rows directly (inspect before any approval)")
    ap.add_argument("--limit", type=int, default=10)
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
    write_edits(edits)


if __name__ == "__main__":
    main()
