#!/usr/bin/env python3
"""Import crowd votes from the game's /export and promote consensus into decisions.csv.

The return path of the crowd-review boundary (notes/crowd_review_plan.md §7): the tile
spooler serves the feed game_export.py ships; players vote yes/no/skip; this script
pulls the vote log, archives it append-only (notes/runs/crowd_decisions.csv — raw
history, kept separate from the authorizing log), scores every voter on the trap
tiles, and appends an ordinary 'approved' event to notes/runs/decisions.csv for each
pair the trusted crowd confirmed — write.py then writes it on its next manual run,
crediting the approvers by name in the public edit summary (via the reviewer field).

Voter tiers from the trap record: 'spammer' votes are ignored entirely; a pair
promotes on two 'normal' yes-votes or one 'reliable' one (PROMOTE_WEIGHT below) with
zero non-spammer 'no' votes; skips never count and never block. Crowd-REFUTED and
contested pairs are REPORT-ONLY: a crowd 'no' has signalled family-level defects
(endlessness→Q205 swept in three quality-noun siblings), so the local queue stays the
appellate court and nothing is ever auto-rejected here.

    uv run import_crowd.py                        # pull, archive, score, promote, report
    uv run import_crowd.py --dry-run              # identical report; writes nothing
    uv run import_crowd.py --from-file x.json     # replay a saved /export (dev/offline)
"""

import argparse
import collections
import csv
import gzip
import json
import os
import re
import urllib.request

import config
import game_export as gx
import review_queue as rq
from fetch_items import USER_AGENT

EXPORT_URL = "https://anchor-game.karl.berlin/export"
# Raw-vote archive (plan §7 columns). Append-only and git-tracked like decisions.csv:
# the service overwrites a repeat click in place, so this file is the only place the
# full click history survives.
CROWD_LOG = os.path.join(config.HERE, "notes", "runs", "crowd_decisions.csv")
VOTE_COLS = ["ts", "sid", "target_lid", "anchor_qid", "user", "decision",
             "feed_version", "stale", "is_gold"]

# ---- voter tiers ---------------------------------------------------------------
# Graded per trap SIDE because their evidential strength differs: approved traps are
# keep-class only (near-unambiguous — an error there means not-reading; this is the
# no-spam detector, since a "no"-spammer aces every rejected trap), while rejected
# traps span judgment-call classes (LATERAL/BROADER/...), so they are scored
# leniently and only guard against yes-spam. Skips on traps never count. Manual
# overrides win over the trap record (spammer override first); anyone else —
# including voters with too little trap data — is 'normal'.
RELIABLE_APPROVED_MIN = 2                       # ≥2 approved-trap votes, ALL correct…
RELIABLE_REJECTED_MIN = 2                       # …AND ≥2 rejected-trap votes, ≥1 correct
SPAM_APPROVED_MIN, SPAM_APPROVED_ACC = 2, 0.50  # <50% on ≥2 approved traps → spammer
SPAM_REJECTED_MIN, SPAM_REJECTED_ACC = 3, 0.34  # <34% on ≥3 rejected traps → spammer
RELIABLE_USERS = ()                             # e.g. ("Niobian",)
SPAMMER_USERS = ()
PROMOTE_WEIGHT = {"reliable": 2, "normal": 1}   # promote at yes-weight ≥ 2, zero 'no'


def inline_refs(node, defs):
    """Resolve '#/$defs/x' $ref pointers and drop $defs, so the vote contract can be
    checked by game_export.schema_check unchanged — it hard-fails on unknown keywords
    by design, and $ref/$defs are the only ones the vote schema adds."""
    if isinstance(node, dict):
        if "$ref" in node:
            return inline_refs(defs[node["$ref"].rsplit("/", 1)[1]], defs)
        return {k: inline_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [inline_refs(v, defs) for v in node]
    return node


def vote_schema():
    path = os.path.join(config.GAME_REPO, "schema", "vote_export.schema.json")
    try:
        with open(path, encoding="utf-8") as f:
            schema = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"vote contract not found: {path} — clone the service repo "
                         "beside this one or set ANCHOR_GAME_REPO")
    return inline_refs(schema, schema.get("$defs", {}))


def load_export(url, path=None):
    """The /export payload, contract-checked. Full pull every run: the log is tiny,
    and dedup against the local archive replaces any since-checkpoint state."""
    if path:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req) as r:
            payload = json.loads(r.read().decode())
    try:
        gx.schema_check(payload, vote_schema())
    except ValueError as e:
        raise SystemExit(f"/export violates the vote contract: {e}")
    return payload


def sid_key(sid):
    """(target_lid, anchor_qid) — the decisions.csv key — from 'L…|Q…|v1'."""
    lid, qid, _schema = sid.split("|")
    return lid, qid


def load_feeds(feed_dir=gx.FEED_DIR):
    """({sid: feed row} over every feed_v*.jsonl.gz, newest feed's sid set).

    Later versions win; the union keeps dropped sids resolvable, since their meta
    still has to fill a promotion row. The newest-sids set only feeds the report's
    '(stale)' marker."""
    files = sorted(f for f in (os.listdir(feed_dir) if os.path.isdir(feed_dir) else [])
                   if re.match(r"feed_v\d+\.jsonl\.gz$", f))
    if not files:
        raise SystemExit(f"no feeds in {feed_dir} — nothing to grade or promote against")
    by_sid, newest = {}, set()
    for name in files:
        newest = set()
        with gzip.open(os.path.join(feed_dir, name), "rt", encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                by_sid[row["sid"]] = row
                newest.add(row["sid"])
    return by_sid, newest


def load_archive(path=CROWD_LOG):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def archive_votes(votes, dry, path=CROWD_LOG):
    """Append export rows not yet archived; returns (n_new, combined rows).

    Dedup key (sid, user, ts): a repeat click reappears as the same (sid, user) with
    a new ts, and we deliberately keep BOTH rows — aggregation takes the latest."""
    rows = load_archive(path)
    seen = {(r["sid"], r["user"], r["ts"]) for r in rows}
    fresh = []
    for v in votes:
        if (v["sid"], v["user"], v["ts"]) in seen:
            continue
        lid, qid = sid_key(v["sid"])
        fresh.append({"ts": v["ts"], "sid": v["sid"], "target_lid": lid,
                      "anchor_qid": qid, "user": v["user"], "decision": v["decision"],
                      "feed_version": v["feed_version"] or "",
                      "stale": str(int(v["stale"])), "is_gold": str(int(v["is_gold"]))})
    fresh.sort(key=lambda r: (r["ts"], r["sid"], r["user"]))
    if fresh and not dry:
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=VOTE_COLS)
            if new:
                w.writeheader()
            w.writerows(fresh)
    return len(fresh), rows + fresh


def latest_votes(rows):
    """{(sid, user): row}, max-ts row winning (string compare is safe on the fixed
    'YYYY-MM-DD HH:MM:SS' format)."""
    latest = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        latest[(r["sid"], r["user"])] = r
    return latest


def trap_scores(latest, by_sid):
    """{user: [a_ok, a_n, r_ok, r_n]} from votes on trap tiles, skips not counted.

    Graded against the FEED's gold label, not the exported is_gold flag — that flag
    is export-time state (the service LEFT JOINs the *current* suggestion table), so
    a vote cast while the pair was still live grades here against the eventual human
    verdict: more signal, and promotion has its own already-decided check."""
    scores = collections.defaultdict(lambda: [0, 0, 0, 0])
    for (sid, user), r in latest.items():
        row = by_sid.get(sid)
        gold = row["gold"] if row else None
        if r["is_gold"] == "1" and gold is None:
            print(f"  warn: {sid} exported is_gold but no local feed marks it gold "
                  "(feed files out of date?) — vote not graded")
        if gold is None or r["decision"] == "skip":
            continue
        s = scores[user]
        if gold == "approved":
            s[0] += r["decision"] == "yes"
            s[1] += 1
        else:
            s[2] += r["decision"] == "no"
            s[3] += 1
    return scores


def classify(scores, users):
    """{user: 'spammer' | 'reliable' | 'normal'}."""
    tiers = {}
    for u in users:
        a_ok, a_n, r_ok, r_n = scores.get(u, (0, 0, 0, 0))
        if u in SPAMMER_USERS:
            t = "spammer"
        elif u in RELIABLE_USERS:
            t = "reliable"
        elif a_n >= SPAM_APPROVED_MIN and a_ok / a_n < SPAM_APPROVED_ACC:
            t = "spammer"
        elif r_n >= SPAM_REJECTED_MIN and r_ok / r_n < SPAM_REJECTED_ACC:
            t = "spammer"
        elif (a_n >= RELIABLE_APPROVED_MIN and a_ok == a_n
              and r_n >= RELIABLE_REJECTED_MIN and r_ok >= 1):
            t = "reliable"
        else:
            t = "normal"
        tiers[u] = t
    return tiers


def aggregate(latest, tiers, decisions, by_sid):
    """Bucket every voted live pair; returns (promoted, refuted, contested, pending,
    n_decided, uncovered_sids). promoted/refuted items are (sid, [users]).

    A vote whose sid has dropped out of the newest feed still counts while the pair
    is undecided: sids drop out almost always BECAUSE the pair got decided (caught by
    the decisions replay right here), and the sid pins the exact tile the voter saw."""
    by_pair = collections.defaultdict(list)
    for (sid, user), r in latest.items():
        by_pair[sid].append((user, r["decision"]))
    promoted, refuted, contested, uncovered = [], [], [], []
    pending = decided = 0
    for sid, votes in sorted(by_pair.items()):
        row = by_sid.get(sid)
        if row is None:
            uncovered.append(sid)   # no feed carries it — never promote blind
            continue
        if row["gold"] is not None:
            continue                # trap: measured in trap_scores, never promoted
        if sid_key(sid) in decisions:
            decided += 1            # any status blocks — idempotency, and pairs
            continue                # Karl decided after the feed shipped
        votes = [(u, d) for u, d in votes if tiers[u] != "spammer" and d != "skip"]
        yes = sorted(u for u, d in votes if d == "yes")
        no = sorted(u for u, d in votes if d == "no")
        if yes and no:
            contested.append((sid, yes, no))
        elif sum(PROMOTE_WEIGHT[tiers[u]] for u in yes) >= 2:
            promoted.append((sid, yes))
        elif sum(PROMOTE_WEIGHT[tiers[u]] for u in no) >= 2:
            refuted.append((sid, no))
        elif votes:
            pending += 1
    return promoted, refuted, contested, pending, decided, uncovered


def promote(promoted, by_sid, dry):
    """One ordinary 'approved' decisions.csv event per crowd-promoted pair — write.py
    picks it up on its next manual run. reviewer = 'crowd:' + the yes-voters joined
    with ', ': write.py strips the prefix and puts the names verbatim into the edit
    summary, never parsing them back — so any characters hub usernames allow are fine
    (csv quoting handles the field)."""
    for sid, yes in promoted:
        lid, qid = sid_key(sid)
        m = by_sid[sid]["meta"]
        if not dry:
            rq.append_decision({
                "ts": rq.now(), "target_lid": lid, "anchor_qid": qid,
                "target_lemma": m["lemma"], "target_lang": m["lang"],
                "target_n_senses": m["n_senses"], "verdict": m["verdict"],
                "reason": m["reason"], "model": m["model"],
                "status": "approved", "reviewer": "crowd:" + ", ".join(yes),
                "revid": "",
            })


def pair_line(sid, by_sid, newest):
    m = by_sid[sid]["meta"]
    lid, qid = sid_key(sid)
    stale = "" if sid in newest else "  (stale)"
    return f"  {m['lang']}  {m['lemma']:<20} {lid} → {qid}{stale}"


def report(payload, n_new, latest, scores, tiers, agg, by_sid, newest, dry):
    promoted, refuted, contested, pending, decided, uncovered = agg
    promoted_note = "would promote (dry-run)" if dry else "promoted"
    counts = collections.defaultdict(collections.Counter)
    for (sid, user), r in latest.items():
        counts[user][r["decision"]] += 1
    print(f"pulled {len(payload['votes'])} votes (as_of {payload['as_of']}, "
          f"feed {payload['feed_version']}) · {n_new} new → {os.path.relpath(CROWD_LOG, config.HERE)}"
          + (" (dry-run: not written)" if dry and n_new else ""))
    print("users (traps graded from the feed gold; skips not counted):")
    for u in sorted(counts, key=lambda u: (tiers[u], u)):
        a_ok, a_n, r_ok, r_n = scores.get(u, (0, 0, 0, 0))
        c = counts[u]
        mark = "  [override]" if u in RELIABLE_USERS or u in SPAMMER_USERS else ""
        print(f"  {u:<20} {tiers[u]:<9} {sum(c.values()):>3} votes "
              f"({c['yes']}y/{c['no']}n/{c['skip']}s)   "
              f"approved-traps {a_ok}/{a_n} · rejected-traps {r_ok}/{r_n}{mark}")
    n_spam = sum(sum(c.values()) for u, c in counts.items() if tiers[u] == "spammer")
    if n_spam:
        print(f"ignored {n_spam} vote(s) from "
              f"{sum(t == 'spammer' for t in tiers.values())} spammer(s)")
    for sid in uncovered:
        print(f"  warn: vote(s) on {sid} — no local feed carries it, not counted")
    print(f"{promoted_note} {len(promoted)} → {os.path.relpath(rq.DECISIONS, config.HERE)} "
          "(status=approved; written on the next write.py run):")
    for sid, yes in promoted:
        print(pair_line(sid, by_sid, newest) + f"   by {', '.join(yes)}")
    if refuted:
        print(f"crowd-refuted {len(refuted)} — REPORT-ONLY, adjudicate locally:")
        for sid, no in refuted:
            print(pair_line(sid, by_sid, newest) + f"   no: {', '.join(no)}")
            print(f"      verifier: {by_sid[sid]['meta']['reason']}")
    if contested:
        print(f"contested {len(contested)} (mixed yes/no among non-spammers):")
        for sid, yes, no in contested:
            print(pair_line(sid, by_sid, newest)
                  + f"   yes: {', '.join(yes)} / no: {', '.join(no)}")
            print(f"      verifier: {by_sid[sid]['meta']['reason']}")
    print(f"pending: {pending} voted pair(s) below threshold · "
          f"{decided} already-decided pair(s) excluded")


def main():
    ap = argparse.ArgumentParser(description="import crowd votes; see module docstring")
    ap.add_argument("--url", default=EXPORT_URL)
    ap.add_argument("--from-file", metavar="PATH",
                    help="read a saved /export JSON instead of pulling")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only — archive and append nothing")
    args = ap.parse_args()

    payload = load_export(args.url, args.from_file)
    by_sid, newest = load_feeds()
    n_new, rows = archive_votes(payload["votes"], args.dry_run)
    latest = latest_votes(rows)
    scores = trap_scores(latest, by_sid)
    tiers = classify(scores, sorted({u for _, u in latest}))
    agg = aggregate(latest, tiers, rq.load_decisions(), by_sid)
    promote(agg[0], by_sid, args.dry_run)
    report(payload, n_new, latest, scores, tiers, agg, by_sid, newest, args.dry_run)


if __name__ == "__main__":
    main()
