#!/usr/bin/env python3
"""Write P5137 sense statements to Wikidata — gated on human approval.

Reads ONLY rows the human approved in review_queue.py (status='approved') and,
for each, builds a `wbeditentity` payload adding P5137 → anchor Q to a sense,
with a MANDATORY reference (never bare): based on heuristic (P887) + retrieved
(P813). The CC-BY-SA DBnary gloss is never written.

    uv run write.py --dry-run                 # print exact payloads, submit nothing
    uv run write.py --dry-run --preview       # preview verifier-'yes' rows (no approval needed)
    uv run write.py --write --limit 10        # submit 10 approved edits (needs credentials)

Safety: --dry-run is the default. Live submit needs --write AND WIKI_USER/WIKI_PASS
env vars (a bot password on your operator account) AND WikibaseIntegrator installed
(imported lazily, so dry-run has no dependency). Pilot writes MONOSEMOUS targets only
(add to the existing sense); senseless targets need a gloss policy (see --include-senseless).
"""

import argparse
import datetime
import json
import os
import sqlite3

import config

# P887 "based on heuristic" value: the purpose-made item "inferred from translation"
# (label/description/alias as specified; created 2026-06 on the operator account).
# NOTE: still add `instance of (P31) -> heuristic (Q201413)` to this item, or P887's
# value-type constraint will flag it.
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
        gloss = anchor_label(qid, lang) or lemma
        sense = {"add": "", "glosses": {lang: {"language": lang, "value": gloss}},
                 "claims": {"P5137": [claim]}}
        return {"senses": [sense]}, f"CREATE sense (gloss seeded from CC0 label: {gloss!r})"
    return None, "skipped: senseless (needs gloss policy; use --include-senseless)"


def load_rows(args):
    db = sqlite3.connect(config.DATA + "/review.sqlite3")
    if args.preview:
        db.execute("ATTACH DATABASE ? AS cn", [config.CANDIDATES])
        db.execute("ATTACH DATABASE ? AS vf", [config.VERIFY])
        db.execute("ATTACH DATABASE ? AS it", [config.ITEMS])
        rows = db.execute(
            """SELECT c.target_lid,c.anchor_qid,c.target_lemma,c.target_lang,c.target_n_senses
               FROM cn.candidate c JOIN vf.verdict v
                 ON v.target_lid=c.target_lid AND v.anchor_qid=c.anchor_qid
               WHERE c.homograph_n_lid=1 AND NOT c.cross_pivot_conflict
                 AND c.support_clusters>=2 AND c.target_n_senses=1 AND v.verdict='yes'
                 AND EXISTS(SELECT 1 FROM it.item_type t WHERE t.qid=c.anchor_qid AND t.prop='P31')
               ORDER BY c.support_clusters DESC LIMIT ?""", [args.limit]).fetchall()
    else:
        rows = db.execute(
            "SELECT target_lid,anchor_qid,target_lemma,target_lang,target_n_senses "
            "FROM review WHERE status='approved' ORDER BY target_lang, target_lemma "
            "LIMIT ?", [args.limit]).fetchall()
    db.close()
    return rows


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

    # --- live submission (lazy import keeps dry-run dependency-free) ---
    user, pw = os.environ.get("WIKI_USER"), os.environ.get("WIKI_PASS")
    if not (user and pw):
        raise SystemExit("--write needs WIKI_USER / WIKI_PASS (bot password) env vars")
    if HEURISTIC_ITEM.startswith("Q-TODO"):
        raise SystemExit("refusing to write: pin HEURISTIC_ITEM to a real P887 value first")
    raise SystemExit("live submission via WikibaseIntegrator not wired yet — "
                     "intentionally left to the human operator step")


if __name__ == "__main__":
    main()
