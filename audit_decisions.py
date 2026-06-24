#!/usr/bin/env python3
"""Surface human decisions that disagree with the current verifier — the audit.

The decision log (config.DECISIONS) is the human truth; the verify cache
(config.VERIFY) is the latest LLM verdict. Where they disagree, the human call is
*suspect* and worth a second look:
  * suspected false accept : human approved/written, verifier now != 'yes'
  * suspected false reject : human rejected,         verifier now == 'yes'

Deferred (held) rows are listed separately as a re-review aid, not flagged as
errors. This is pure data — no LLM cost. With --json it emits each disagreement
with the anchor's own CC0 label/description (never a DBnary gloss), ready to feed
an independent adjudication panel.

    uv run audit_decisions.py            # human-readable report
    uv run audit_decisions.py --json     # JSON + CC0 evidence (pipe into a panel)
"""

import argparse
import collections
import json
import sqlite3

import config
from review_queue import load_decisions

CONFIRMED = ("approved", "written")  # human said the verifier-'yes' was correct
LANGNAME = {"de": "German", "en": "English", "sv": "Swedish"}


def cc0(it, qid, lang):
    """The anchor's own CC0 label/description (target-language preferred). No DBnary."""
    row = it.execute(
        "SELECT label_de,label_en,label_sv,desc_de,desc_en,desc_sv FROM item "
        "WHERE qid=? AND NOT missing", [qid]).fetchone()
    if not row:
        return "(none)", "(none)"
    lde, len_, lsv, dde, den, dsv = row
    labels = {"de": lde, "en": len_, "sv": lsv}
    descs = {"de": dde, "en": den, "sv": dsv}
    lab = [labels.get(lang), len_] if lang != "en" else [len_]
    lab = " / ".join(dict.fromkeys(x for x in lab if x)) or "(no label)"
    desc = descs.get(lang) or den or dde or dsv or "(no description)"
    return lab, desc


def disagreements(dec, verdicts):
    """(accepts, rejects): decided rows where the current verifier disagrees."""
    acc, rej = [], []
    for (lid, qid), d in dec.items():
        cv = verdicts.get((lid, qid))
        if cv is None:
            continue
        verdict, reason = cv
        if d["status"] in CONFIRMED and verdict != "yes":
            acc.append((d, verdict, reason))
        elif d["status"] == "rejected" and verdict == "yes":
            rej.append((d, verdict, reason))
    return acc, rej


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true",
                    help="emit disagreements with CC0 anchor evidence (for a panel)")
    args = ap.parse_args()

    dec = load_decisions()
    if not dec:
        raise SystemExit(f"no decisions at {config.DECISIONS}")
    vf = sqlite3.connect(config.VERIFY)
    verdicts = {(a, b): (v, r) for a, b, v, r in
                vf.execute("SELECT target_lid, anchor_qid, verdict, reason FROM verdict")}
    it = sqlite3.connect(config.ITEMS)
    acc, rej = disagreements(dec, verdicts)

    if args.json:
        out = []
        for kind, items in (("suspected_false_accept", acc), ("suspected_false_reject", rej)):
            for d, verdict, reason in items:
                lab, desc = cc0(it, d["anchor_qid"], d["target_lang"])
                out.append({
                    "kind": kind, "lid": d["target_lid"], "qid": d["anchor_qid"],
                    "lemma": d["target_lemma"],
                    "lang": LANGNAME.get(d["target_lang"], d["target_lang"]),
                    "human_status": d["status"], "verifier_now": verdict,
                    "verifier_reason": reason, "anchor_label": lab, "anchor_desc": desc,
                })
        out.sort(key=lambda x: (x["kind"], x["lang"], x["lemma"]))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    st = collections.Counter(d["status"] for d in dec.values())
    tally = " · ".join(f"{k} {st[k]}" for k in
                       ("approved", "written", "rejected", "deferred") if st.get(k))
    print(f"decision log : {config.DECISIONS}  ({len(dec)} decisions: {tally})")
    print(f"verify cache : {config.VERIFY}  ({len(verdicts)} verdicts)")

    def show(title, items):
        print(f"\n=== {title}: {len(items)} ===")
        for d, verdict, reason in sorted(items, key=lambda x: (x[0]["target_lang"], x[0]["target_lemma"])):
            print(f"  {d['target_lang']} {d['target_lemma']:<18} {d['target_lid']}/{d['anchor_qid']}  "
                  f"human={d['status']}  verifier_now={verdict}  :: {reason}")

    show("SUSPECTED FALSE ACCEPTS (human approved/written, verifier now != yes)", acc)
    show("SUSPECTED FALSE REJECTS (human rejected, verifier now == yes)", rej)

    deferred = [(d, verdicts.get((d["target_lid"], d["anchor_qid"]), ("(absent)", ""))[0])
                for d in dec.values() if d["status"] == "deferred"]
    if deferred:
        print(f"\n=== DEFERRED / held (re-review aid; verifier's current verdict): {len(deferred)} ===")
        for d, verdict in sorted(deferred, key=lambda x: (x[0]["target_lang"], x[0]["target_lemma"])):
            print(f"  {d['target_lang']} {d['target_lemma']:<18} {d['target_lid']}/{d['anchor_qid']}  "
                  f"verifier_now={verdict}")


if __name__ == "__main__":
    main()
