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
    uv run write.py --write --bot --limit 50  # bot task: named first senses, no approval (BOT_USER)

Safety: --dry-run is the default. Live submit needs --write AND WIKI_USER/WIKI_PASS
env vars (a Special:BotPasswords credential on your operator account). Submission goes
through the MediaWiki Action API (wbeditentity) using only the standard library — no
extra dependency, and it posts the exact JSON shown in --dry-run. Edits are NOT
bot-flagged (assisted editing on the operator account), except --bot runs. Three write shapes, each behind its
own flag: MONOSEMOUS by default (add to the lexeme's one sense), senseless with
--include-senseless (create the first sense, needs a gloss policy), and POLYSEMOUS with
--include-poly (add to the ONE existing sense the human approved). The poly shape is the same
EDIT CLASS as the monosemous one — a P5137 on an existing sense — and differs only in which
sense, which is why it needs no new permission; what it needs is the sid, read from the
decision log's target_sid and never from the live verify cache, so that re-running a changed
prompt can never move an approved statement onto a different sense.

Before each live edit the live lexeme is re-fetched: a monosemous target must still have
exactly one sense, and that sense must NOT already carry a P5137 (never override existing
data); a polysemous target's approved sense must still EXIST and must likewise carry no
P5137; a sense-creation target must still be senseless (zero live senses) — if any sense
appeared since the dump (a prior run, or real senses) we skip and leave it for manual review,
since a semantically-duplicate sense is a hazard the P5137-only check can't see. The one
exception is a `create` decision (poly lane, assigner abstained, human confirmed the meaning
is missing): that lexeme HAS senses by construction, so its guard is only that no live sense
already carries this anchor — the human saw the whole inventory before choosing create.

Senseless (--include-senseless) writes are lexeme-centric: all approved items for one lexeme
are grouped into a single edit creating one sense each, so a multi-item lexeme's inventory is
written as one human-approved unit. The gloss is the anchor's CC0 DESCRIPTION — target language
first, else any other language that has one, never a label — keyed by the language of the text.
See review_queue.anchor_gloss for why a foreign description beats a same-language name.
"""

import argparse
import collections
import datetime
import http.cookiejar
import itertools
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
from review_queue import (action_of, anchor_gloss, append_decision, create_evidence,
                          is_auto_deferred, load_decisions, now, pending_view, pool)

SUMMARY = ("Add [[Property:P5137]] (item for this sense) inferred from translation, "
           "ref [[Q140308675]] — LLM-verified, human-approved assisted edit")
# --bot: the approved unreviewed task (named first senses). Its own account, so assisted runs
# can never edit as the bot and the bot can never write outside its task.
BOT_SUMMARY = ("Create first sense with [[Property:P5137]] (item for this sense): item label "
               "equals lemma, inferred from translation, ref [[Q140308675]], LLM-verified; "
               "bot task [[Wikidata:Requests for permissions/Bot/{bot}]]")
BOT_LANGS = ("de", "en", "sv", "ru")
TOOL = "CB"  # EditGroups "custom bot" id (self-service, no registration); batch id is per-run

# P887 "based on heuristic" value: the purpose-made item "inferred from translation"
# (created 2026-06 on the operator account; P31 -> heuristic Q201413, constraint-clean).
HEURISTIC_ITEM = "Q140308675"


def entity_snak(prop, qid):
    return {"snaktype": "value", "property": prop,
            "datavalue": {"type": "wikibase-entityid", "value": {"id": qid}}}


def sense_anchor(sense):
    """The anchor qid a built sense dict carries (exactly one P5137 claim per sense)."""
    return sense["claims"]["P5137"][0]["mainsnak"]["datavalue"]["value"]["id"]


def edit_kind(senses):
    """"create" | "poly" | "mono" — which write shape a built lexeme edit is.

    Read off the payload rather than carried alongside it, so it cannot disagree with what
    would actually be submitted. A created sense has {"add": ...}; a MATCH on a polysemous
    target carries the REAL approved S-id; a monosemous MATCH carries the placeholder
    build_edit writes, because its S-id is only known once the live lexeme is re-fetched."""
    if "add" in senses[0]:
        return "create"
    return "mono" if senses[0].get("id", "").startswith("<") else "poly"


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


# ---- duplicate-sense preflight (senseless multi-item groups only) -----------
# A senseless lexeme with 2-4 approved anchors becomes ONE edit creating a sense each. Nothing
# below the human checks that two of those anchors don't denote the SAME sense — and if they do,
# we don't merely mislink, we CREATE a redundant sense pair on a lexeme that had none. Wikidata's
# lexeme data is already full of those: in the assign_eval `absent` arm, deleting the linked sense
# still left a near-equivalent survivor in at least 51 of 200 polysemous lexemes (>=25%), which is
# the pile this lane could add to. Structural priors do not see it (only 1 of the 319 same-lexeme
# pairs co-occurs on any other lexeme's sense) and neither does gloss overlap (it scored the
# odontology near-duplicate at 0.06), so it takes a read of the two glosses.
#
# Deliberately LAZY: it runs at edit-build time on multi-sense CREATE groups only, so its cost
# scales with real usage rather than with the 283 lexemes that merely *could* become groups —
# 11,371 senseless candidates are gated but only 394 verified, and not one of the 283 currently
# has two verifier-'yes' anchors. Verdicts are cached in config.VERIFY, so a repeated --dry-run
# is free. There is no override flag, matching the monosemous "competing anchors" skip directly
# above the call: a flagged group is a human call, resolved by rejecting one of the anchors.
DUP_MODEL = "sonnet"
DUP_SYSTEM = (
    "You are checking a proposed dictionary entry for REDUNDANT senses before it is written.\n"
    "You get one word and several numbered concepts. Each concept would become its own new "
    "sense of that word, glossed with the text shown. Two concepts are REDUNDANT when they "
    "denote the same sense of the word — a reader would not accept them as two separate "
    "senses, only as two wordings of one.\n"
    "Judge the MEANING, not the wording: different languages, different length and different "
    "phrasing are all normal ('a science dealing with the teeth' and 'the science of "
    "dentistry' are ONE sense). Genuinely distinct senses stay distinct even when related — "
    "an institution and its building, a material and the tissue it comes from, a taxon and a "
    "member of it are all TWO senses.\n"
    "For each concept i report same_as = the SMALLEST number that denotes the same sense as i "
    "(same_as = i when no earlier concept means what i means). Concept 1 always has "
    "same_as = 1.\n"
    'Reply ONLY with a JSON array: [{"i":1,"same_as":1,"reason":"<=10 words"}, ...] — one '
    "object per concept, no prose."
)
DUP_PROMPT_VER = __import__("hashlib").sha1(DUP_SYSTEM.encode()).hexdigest()[:8]


def _dup_cached(vf, lid, qids):
    vf.execute("CREATE TABLE IF NOT EXISTS sense_dup(lid TEXT, qids TEXT, prompt_ver TEXT, "
               "same_as TEXT, reason TEXT, PRIMARY KEY (lid, qids, prompt_ver)) WITHOUT ROWID")
    r = vf.execute("SELECT same_as, reason FROM sense_dup WHERE lid=? AND qids=? AND prompt_ver=?",
                   [lid, ",".join(qids), DUP_PROMPT_VER]).fetchone()
    return r


def duplicate_groups(lid, lang, lemma, senses, it):
    """[[qid, qid, ...], ...] anchors in this CREATE group that denote the same sense; [] if all
    distinct; None if the check failed (already reported). Compares the exact gloss text the
    edit would write, so what is judged is what would be published."""
    import llm                                    # only this path needs the LLM
    qids = [sense_anchor(s) for s in senses]
    vf = sqlite3.connect(config.VERIFY)
    hit = _dup_cached(vf, lid, qids)
    if hit:
        same_as, _reason = hit
        same = [int(x) for x in same_as.split(",")]
    else:
        lines = []
        for n, s in enumerate(senses, 1):
            gl = "; ".join(f"[{g}] {v['value']}" for g, v in sorted(s["glosses"].items()))
            lab = (it.execute("SELECT label FROM item_lang WHERE qid=? AND label IS NOT NULL "
                              "ORDER BY lang<>?, lang<>'en' LIMIT 1",
                              [sense_anchor(s), lang]).fetchone() or [""])[0]
            lines.append(f'{n}. concept {sense_anchor(s)} "{lab}" — gloss to write: {gl}')
        prompt = (f'word: language={config.LANGNAME.get(lang, lang)} noun="{lemma}"\n'
                  "concepts:\n" + "\n".join(lines))
        try:
            ans = llm.batch_json(DUP_SYSTEM, prompt, DUP_MODEL, len(senses),
                                 {"same_as": None, "reason": ""})
        except Exception as e:  # noqa: BLE001 — an unreachable model must not silently pass a group
            print(f"# SKIP Lexeme:{lid} — duplicate-sense preflight failed ({e}); "
                  "not writing a multi-sense inventory unchecked")
            vf.close()
            return None
        same = []
        for n, a in enumerate(ans, 1):
            try:
                v = int(a.get("same_as"))
            except (TypeError, ValueError):
                v = n                              # unparseable -> treat as its own sense
            same.append(v if 1 <= v <= n else n)
        vf.execute("INSERT OR REPLACE INTO sense_dup VALUES (?,?,?,?,?)",
                   [lid, ",".join(qids), DUP_PROMPT_VER, ",".join(map(str, same)),
                    "; ".join(str(a.get("reason", ""))[:60] for a in ans)])
        vf.commit()
    vf.close()
    by = {}
    for n, root in enumerate(same, 1):
        by.setdefault(root, []).append(qids[n - 1])
    return [g for g in by.values() if len(g) > 1]


def build_edit(row, today, include_senseless, it, include_poly=False):
    """Return (wbeditentity_data, note) for one approved candidate.

    Monosemous: add the claim to the lexeme's single existing sense (its S-id is
    resolved against the live lexeme at write time). Senseless: create a sense whose
    gloss is the anchor's CC0 description (target language first, else any language that
    has one; never a label, skip if none), keyed by the language the text is in, plus an
    en gloss for legibility (review_queue.anchor_gloss, the SAME text the review UI
    previewed).
    `it` is the shared items-DB connection (opened once in main)."""
    lid, qid, lemma, lang, n_senses, target_sid, create = row
    claim = build_statement(qid, today)

    if create and not include_senseless:
        return None, (f"skipped: create a new sense on a {n_senses}-sense lexeme "
                      "(sense creation; use --include-senseless)")
    if create:
        n_senses = 0      # take the sense-creation path below; the live guard knows better
    if n_senses == 1:
        # The placeholder is what marks this shape for write_edits: a monosemous target's
        # S-id is not known until the live lexeme is re-fetched, whereas a poly target's IS
        # known — it is the sense the human approved.
        sense = {"id": f"<{lid}-S? resolved live>", "claims": {"P5137": [claim]}}
        return {"senses": [sense]}, "add to existing sense"
    if n_senses >= 2 and include_poly and target_sid:
        # MATCH: add the claim to the ONE existing sense the human approved. The sid comes
        # from the decision log, never from the live verify cache — a prompt change must not
        # be able to move an approved statement onto a different sense.
        return ({"senses": [{"id": target_sid, "claims": {"P5137": [claim]}}]},
                f"add to approved sense {target_sid}")
    if n_senses >= 2 and include_poly:
        return None, "skipped: polysemous, approved without a target_sid (pre-migration row?)"
    if n_senses >= 2:
        # MATCH on a polysemous target: the action is "write to ONE of the existing senses",
        # and which one is not decidable here — it is the per-sense assignment, and it has to
        # be the sid the human actually approved. Until that is threaded through, refuse.
        # This branch used to fall into the sense-CREATION path below, because that path
        # gated on the --include-senseless FLAG rather than on the sense count: a polysemous
        # approved row would have created a brand-new sense on a lexeme that already has
        # some — the `create_add` action the design records and refuses to write. Unreachable
        # before the poly review lane existed; reachable the moment it does.
        return None, (f"skipped: polysemous target ({n_senses} senses); "
                      "use --include-poly to write the approved sense")
    if include_senseless:
        glosses = anchor_gloss(it, qid, lang, lemma)  # descriptions only, never the headword
        if not glosses:  # no CC0 description in ANY language -> nothing that defines the sense
            return None, "skipped: senseless, no CC0 anchor description in any language"
        sense = {"add": "",
                 "glosses": {g: {"language": g, "value": t} for g, t in glosses.items()},
                 "claims": {"P5137": [claim]}}
        # Report the language the gloss is ACTUALLY in, not the candidate's. Since glosses are
        # descriptions-only, 1,154 of 8,876 senseless edits are legitimately glossed from
        # another language, and tagging those gloss[de] in the on-wiki edit summary misstates
        # what was written. Same primary-pick as the review render, so preview and summary agree.
        primary_lang = lang if lang in glosses else next(iter(glosses))
        primary = glosses[primary_lang]
        extra = " +en" if "en" in glosses and primary_lang != "en" else ""
        return {"senses": [sense]}, f"CREATE sense (gloss[{primary_lang}]{extra}: {primary!r})"
    return None, "skipped: senseless (needs gloss policy; use --include-senseless)"


def load_bot_rows():
    """The bot task's rows: undecided senseless pool rows the verifier kept (no auto-defer, no
    triage action) whose item's own label or alias is the lemma — the population the trials
    sampled (named150, ru100). Pairs a human decided stay with the assisted runs."""
    _d, und, ctx, ad, _v = pending_view("senseless")
    it, wd = sqlite3.connect(config.ITEMS), sqlite3.connect(config.WIKIDATA)
    return sorted(r for r in und if r[3] in BOT_LANGS
                  and not is_auto_deferred((r[0], r[1]), r[6], ctx, ad)
                  and not action_of((r[0], r[1]), r[6], ctx)
                  and create_evidence(it, wd, r[0], r[1], r[3], r[2])[0] == "named")


def load_rows(args):
    """(lid, anchor_qid, lemma, lang, n_senses) rows to build edits from.

    --preview reuses the review queue's verifier-'yes' High-tier pool directly
    (inspect before any approval). Otherwise read the rows the human approved from
    the decision log (the single source of truth); 'approved' is the current
    replayed status, so anything later written/rejected is naturally excluded."""
    if args.preview:
        # senseless preview when --include-senseless, else the monosemous write stream
        lane = ("senseless" if args.include_senseless
                else "poly" if args.include_poly else "mono")
        # poly: the verifier's chosen sense rides on the pool row (NULL/none → no target_sid,
        # build_edit skips it as approved-without-a-sense would be)
        rows = [(*r[:5], (r[-1] if lane == "poly" and (r[-1] or "").startswith("L") else ""),
                 False) for r in pool(lane=lane)]
    else:
        # `create` rows are the poly lane's create_add decisions: a new sense on a lexeme
        # that already has some. build_edit routes them by the flag, never by n_senses.
        rows = [(d["target_lid"], d["anchor_qid"], d["target_lemma"], d["target_lang"],
                 int(d["target_n_senses"]), (d.get("target_sid") or "").strip(),
                 d["status"] == "create")
                for d in load_decisions().values() if d["status"] in ("approved", "create")]
    # Sorted so one lexeme's rows are contiguous (same lid ⇒ same lang+lemma), which is what
    # lets main() group and cap by lexeme in a single pass. --limit is NOT applied here: the
    # approved pool holds both lanes, and a plain run drops every senseless row at build time,
    # so capping candidates would silently under-deliver edits (a --limit 20 that built none).
    rows.sort(key=lambda r: (r[3], r[2], r[0], r[1]))
    return rows


# ---- Action API submission (stdlib only) -----------------------------------

MAXLAG_RETRIES = 8  # back-off attempts on maxlag / 429 / 503 before giving up


def _api(opener, params, post=False):
    """One Action API call, honouring maxlag back-off (API:Etiquette).

    A maxlag rejection (JSON `error.code=maxlag`) or a 429 happens *before* the
    edit is applied, so retrying a POST is safe. A 503 or a bare network failure
    on a POST is NOT retried — an intermediary can return either after the edit
    already landed, and we must not risk a duplicate."""
    params = {**params, "format": "json", "formatversion": "2"}
    backoff = 5.0
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
            # lag persists for minutes; sleeping just the reported lag gave up after ~30 s
            time.sleep(min(max(float(data["error"].get("lag") or 0), backoff), 60))
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


def submit(opener, csrf, lid, data, summary, extra=None):
    # No "bot" key. API booleans are presence-checked, so *any* value — "0" included —
    # marks the edit as a bot edit; omitting the key is the only way to say false. Inert
    # today because the account lacks the `bot` right, but the BRFA that grants it would
    # silently hide every edit from Recent Changes, which is the last place this pilot
    # wants to disappear from. Re-add as "bot": "1" only as a deliberate call.
    #                   "bot": "0",
    res = _api(opener, {"action": "wbeditentity", "id": lid, "data": json.dumps(data),
                        "summary": summary, "token": csrf, "maxlag": "5", **(extra or {})},
               post=True)
    if "error" in res:
        raise RuntimeError(res["error"].get("info", str(res["error"])))
    return res.get("entity", {}).get("lastrevid")


def write_edits(edits, max_per_min, bot=False, rows=()):
    """Resolve each sense live, safety-check, submit, record the revid in the queue.

    Submissions are paced to at most `max_per_min` edits/minute (0 = no cap), the
    per-minute rate limit the bot policy requires editing tools to support."""
    pre = "BOT" if bot else "WIKI"
    user, pw = os.environ.get(f"{pre}_USER"), os.environ.get(f"{pre}_PASS")
    if not (user and pw):
        raise SystemExit(f"--write needs {pre}_USER / {pre}_PASS (Special:BotPasswords) env vars")
    account = user.split("@")[0]
    if bot != ("bot" in account.lower()):
        raise SystemExit(f"{pre}_USER is {account!r}: --bot runs need the bot account, "
                         "assisted runs the operator account")
    if HEURISTIC_ITEM.startswith("Q-TODO"):
        raise SystemExit("refusing to write: pin HEURISTIC_ITEM to a real P887 value first")
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf = login(opener, user, pw)
    extra = {"assert": "user"}
    summary_base = SUMMARY
    if bot:
        # bot=1 on every edit (policy); assert=bot only once the flag is granted, since the
        # trial run before it would fail the assert
        rights = _api(opener, {"action": "query", "meta": "userinfo", "uiprop": "rights"})
        extra = {"bot": "1", "assert": "bot" if "bot" in rights["query"]["userinfo"]
                 .get("rights", []) else "user"}
        summary_base = BOT_SUMMARY.format(bot=account)
    info = {(r[0], r[1]): r for r in rows}
    decisions = load_decisions()  # to carry the audited fields onto the 'written' event
    # One EditGroups batch per run: every edit shares this id, so the whole run is one
    # auditable, one-click-revertible group on editgroups.toolforge.org. Padded to 12 hex
    # digits to stay inside EditGroups' 4–32 lowercase-hex range for any random value.
    batch_id = "%012x" % random.randrange(2**48)
    summary = f"{summary_base} ([[:toolforge:editgroups/b/{TOOL}/{batch_id}|details]])"
    group_url = f"https://editgroups.toolforge.org/b/{TOOL}/{batch_id}/"
    interval = 60.0 / max_per_min if max_per_min > 0 else 0.0
    print(f"editgroups batch {TOOL}/{batch_id} → {group_url}")
    print(f"throttle: {'≤%d edits/min' % max_per_min if interval else 'none'}")
    written = 0
    last_edit = 0.0  # monotonic time of the last submitted edit, for pacing
    for lid, data in edits:
        senses = data["senses"]
        # One lexeme's edit is either monosemous (exactly one sense, {"id": ...} — main() drops
        # the multi-anchor case) or sense-creation (one or more {"add": ...} senses grouped
        # lexeme-wise). Every sense carries its anchor.
        kind = edit_kind(senses)
        is_create = kind == "create"
        anchors = [sense_anchor(s) for s in senses]
        try:
            if kind == "poly":
                # MATCH a SPECIFIC existing sense. The dump can be stale in three ways that
                # each make the approved statement wrong to write, so check all three against
                # the live lexeme: the sense may be gone, it may already carry this very
                # anchor (a prior run that died before logging), or it may have acquired some
                # OTHER P5137 since — which we never override.
                live = {s["id"]: s for s in live_senses(opener, lid)}
                stale = False
                for sense in senses:
                    sid, anchor = sense["id"], sense_anchor(sense)
                    if sid not in live:
                        print(f"  skip {lid}: approved sense {sid} no longer exists")
                        stale = True
                        break
                    p5137 = p5137_targets(live[sid])
                    if anchor in p5137:
                        print(f"  skip {lid}: {sid} already has P5137 → {anchor}")
                        stale = True
                        break
                    if p5137:
                        print(f"  skip {lid}: {sid} already has P5137 {p5137} (never override)")
                        stale = True
                        break
                if stale:
                    continue
            elif not is_create:  # monosemous: resolve the live sense id + guard
                anchor = anchors[0]
                sid, p5137 = resolve_sense(opener, lid)
                if anchor in p5137:
                    print(f"  skip {lid}: already has P5137 → {anchor}"); continue
                if p5137:
                    print(f"  skip {lid}: sense already has P5137 {p5137} (never override)")
                    continue
                senses[0]["id"] = sid
            elif any(decisions.get((lid, a), {}).get("status") == "create" for a in anchors):
                # create_add: the human chose "create" over the inventory the dump showed.
                # Skip if the anchor already sits on a live sense (a prior run that died
                # before logging, or another editor), or if the inventory changed since — a
                # new sense may already mean this, without a P5137 to show it.
                live = live_senses(opener, lid)
                carried = {a for a in anchors for s in live if a in p5137_targets(s)}
                if carried:
                    print(f"  skip {lid}: a live sense already has P5137 → {sorted(carried)}")
                    continue
                n_seen = {decisions[(lid, a)]["target_n_senses"] for a in anchors
                          if decisions.get((lid, a), {}).get("status") == "create"}
                if n_seen != {str(len(live))}:
                    print(f"  skip {lid}: {len(live)} live senses, reviewed against "
                          f"{'/'.join(sorted(n_seen))}; re-review")
                    continue
            else:
                # sense-creation: only ever CREATE a first sense on a still-senseless lexeme.
                # If the live lexeme has ANY sense now (a prior run created it and died before
                # logging, OR real senses appeared since the dump), skip — adding one risks a
                # semantic duplicate the P5137-only check can't see. Manual reconcile instead.
                live = live_senses(opener, lid)
                if live:
                    print(f"  skip {lid}: no longer senseless ({len(live)} live sense(s)) — "
                          f"manual review (semantic-duplicate hazard) → {anchors}")
                    continue
            # Crowd-approved rows credit their approvers in the public edit summary:
            # import_crowd.py sets reviewer = "crowd:<names>", used verbatim here —
            # never parsed back, so any characters hub usernames allow are fine.
            crowd = sorted({rev[len("crowd:"):] for a in anchors
                            if (rev := decisions.get((lid, a), {}).get("reviewer", ""))
                            .startswith("crowd:")})
            if interval and last_edit:  # pace only between actual submissions
                wait = interval - (time.monotonic() - last_edit)
                if wait > 0:
                    time.sleep(wait)
            revid = submit(opener, csrf, lid, data, summary
                           + (f", approved by {', '.join(crowd)} via the distributed game"
                              if crowd else ""), extra)
            last_edit = time.monotonic()
        except Exception as e:  # noqa: BLE001 — one bad edit shouldn't abort the batch
            print(f"  FAIL {lid} → {anchors}: {e}"); continue
        # One submit = one revid, but the decision log is keyed (lid, qid): log each anchor.
        for anchor in anchors:
            d = decisions.get((lid, anchor), {})
            r = info.get((lid, anchor))
            if bot and r:  # no human decision behind a bot row: log the pool row itself
                d = {"target_lemma": r[2], "target_lang": r[3], "target_n_senses": str(r[4]),
                     "verdict": "yes", "reason": r[6], "model": r[7]}
            append_decision({
                "ts": now(), "target_lid": lid, "anchor_qid": anchor,
                "target_lemma": d.get("target_lemma", ""), "target_lang": d.get("target_lang", ""),
                "target_n_senses": d.get("target_n_senses", ""), "verdict": d.get("verdict", ""),
                "reason": d.get("reason", ""), "model": d.get("model", ""),
                "status": "written",
                "reviewer": f"{'bot' if bot else 'write.py'} b/{TOOL}/{batch_id}",
                "revid": str(revid),
            })
            print(f"  wrote {lid} → {anchor}  (rev {revid})")
        written += 1
        print(f"    https://www.wikidata.org/wiki/Lexeme:{lid}")
        print(f"    https://www.wikidata.org/wiki/Special:Diff/{revid}")
    print(f"\n{written}/{len(edits)} lexeme edit(s) submitted.")
    if written:
        print(f"editgroups batch → {group_url}")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="print payloads, submit nothing (default)")
    g.add_argument("--write", action="store_true", help="actually submit (needs credentials)")
    ap.add_argument("--preview", action="store_true",
                    help="read verifier-'yes' rows directly (inspect before any approval)")
    ap.add_argument("--limit", type=int, default=10,
                    help="cap on lexeme EDITS built (not candidates read); live-state skips at "
                         "submit time can still land fewer")
    ap.add_argument("--max-edits-per-min", type=int, default=30,
                    help="cap submission rate (0 = no cap); default 30")
    ap.add_argument("--include-poly", action="store_true",
                    help="also build MATCH edits for polysemous targets, adding P5137 to the "
                         "ONE existing sense the human approved (decisions.csv target_sid). "
                         "Same edit class as the monosemous stream — only the sense differs")
    ap.add_argument("--include-senseless", action="store_true",
                    help="also build sense-creation edits (gloss = the anchor's CC0 "
                         "description, any language, never a label; one edit per lexeme)")
    ap.add_argument("--bot", action="store_true",
                    help="the bot task: create first senses for the verifier-kept named "
                         f"senseless pool ({'/'.join(BOT_LANGS)}), no human approval; "
                         "edits as BOT_USER/BOT_PASS, bot-flagged")
    ap.add_argument("--pairs", help="only these pairs: a file of 'lid qid' lines "
                    "(e.g. notes/runs/named150.txt), so one batch is one reviewed set")
    args = ap.parse_args()
    if args.bot:
        args.include_senseless = True  # the bot task is sense creation only
    if args.preview and args.include_senseless and args.include_poly:
        ap.error("--preview shows ONE lane's pool; --include-senseless and --include-poly "
                 "select different ones. (Without --preview the flags are additive, because "
                 "the approved log holds every lane at once.)")

    today = datetime.date.today().isoformat()
    bot_rows = load_bot_rows() if args.bot else []
    rows = ([(*r[:5], "", False) for r in bot_rows] if args.bot else load_rows(args))
    rows.sort(key=lambda r: (r[3], r[2], r[0], r[1]))
    if args.pairs:
        want = {tuple(l.split()[:2]) for l in open(args.pairs) if l.strip() and l[0] != "#"}
        rows = [r for r in rows if (r[0], r[1]) in want]
    src = ("bot-task (named, verifier-kept)" if args.bot
           else "verifier-'yes' (preview)" if args.preview else "approved")
    print(f"{len(rows)} {src} candidate(s), building ≤{args.limit} lexeme edit(s); "
          f"mode={'WRITE' if args.write else 'dry-run'}; "
          f"reference=P887({HEURISTIC_ITEM})+P813({today})\n")

    # Build one sense per candidate row, GROUPED by lexeme (lexeme-centric, per
    # sense_creation_plan): a senseless lexeme with several approved items becomes ONE wbeditentity creating
    # 2–4 senses, so near-synonym duplicates are caught in one human-approved inventory rather
    # than written blind pair-by-pair. Monosemous lexemes appear once, so grouping is a no-op there.
    # --limit counts EDITS BUILT, not candidates read: a run's cost is the edits it submits, and
    # the rows that build nothing (senseless without --include-senseless, competing anchors) are
    # exactly the ones a cap must not spend itself on.
    edits = []
    it = sqlite3.connect(config.ITEMS)  # shared by the senseless gloss lookups
    for lid, group in itertools.groupby(rows, key=lambda r: r[0]):
        if len(edits) >= args.limit:
            break
        senses = []
        last = None
        for row in group:
            # Any row of the group carries the group's lemma/lang (same lid ⇒ same both),
            # which the duplicate-sense preflight below needs to phrase its question.
            last = row
            data, note = build_edit(row, today, args.include_senseless, it, args.include_poly)
            print(f"# {row[3]} {row[2]!r}  Lexeme:{lid} → {row[1]}   [{note}]")
            if data is not None:
                senses.extend(data["senses"])
        if not senses:
            continue
        # A poly lexeme can carry a MATCH (approved sense) and a CREATE (abstained, human
        # confirmed) in one group. They are different write shapes with different live guards,
        # so they go out as two edits; edit_kind reads senses[0] and must never see a mix.
        created = [s for s in senses if "add" in s]
        groups = ([[s for s in senses if "add" not in s], created]
                  if created and len(created) < len(senses) else [senses])
        for senses in groups:
            # A monosemous lexeme has exactly ONE live sense, so several approved anchors for it don't
            # form an inventory — they all pile onto that one sense. Grouping them can't be written
            # (there is a single live sense id to resolve, and the extra copies would go out carrying
            # the unresolved placeholder), and writing several P5137 values on one sense would assert
            # that the sense denotes each of them — exactly the pivot ambiguity gate A exists to keep
            # out. Competing anchors are a human call: skip the lexeme, approve one and reject the rest.
            kind = edit_kind(senses)
            if kind == "mono" and len(senses) > 1:
                print(f"# SKIP Lexeme:{lid} — {len(senses)} approved anchors "
                      f"{[sense_anchor(s) for s in senses]} compete for one monosemous sense; "
                      f"keep exactly one approved")
                continue
            if kind == "poly" and len(senses) > 1:
                # Several approved anchors on one polysemous lexeme are FINE — that is the normal
                # shape, one per sense, written as a single edit. What is not fine is two anchors
                # approved onto the SAME sense: that asserts the sense denotes both, the pivot
                # ambiguity gate A exists to keep out. Same human resolution as the monosemous
                # case, but scoped to the colliding sense rather than the whole lexeme.
                by_sid = collections.Counter(s["id"] for s in senses)
                clash = {sid: n for sid, n in by_sid.items() if n > 1}
                if clash:
                    print(f"# SKIP Lexeme:{lid} — {sorted(clash)} each have "
                          f"{max(clash.values())} approved anchors competing for one sense; "
                          f"keep exactly one approved per sense")
                    continue
            # Creating several senses at once is the one edit shape that can INVENT a redundant
            # sense pair, so it is the one shape that gets read before it goes out.
            if len(senses) > 1 and kind == "create":
                dups = duplicate_groups(lid, last[3], last[2], senses, it)
                if dups is None:
                    continue
                if dups:
                    print(f"# SKIP Lexeme:{lid} — duplicate-sense preflight: "
                          + "; ".join("+".join(g) + " denote the same sense" for g in dups)
                          + "; creating these as separate senses would add a redundant pair — "
                            "reject all but one of each group, then re-run")
                    continue
            edits.append((lid, {"senses": senses}))

    for lid, data in edits:
        n = len(data["senses"])
        if n > 1:
            print(f"# Lexeme:{lid} — {n} senses grouped into one lexeme edit (lexeme-centric)")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print()

    if not args.write:
        print(f"dry-run: {len(edits)} lexeme edit(s) built, none submitted.")
        if HEURISTIC_ITEM.startswith("Q-TODO"):
            print("NOTE: P887 heuristic item is a placeholder — pin HEURISTIC_ITEM before writing.")
        return

    if args.preview:
        raise SystemExit("refusing to --write in --preview mode: write only human-approved rows")
    write_edits(edits, args.max_edits_per_min, args.bot, bot_rows)


if __name__ == "__main__":
    main()
