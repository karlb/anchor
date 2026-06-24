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

A second, LLM-based mode (--gloss) audits CONFIRMED links the other way: it compares
each lexeme's own CC0 Wikidata sense gloss against the anchor and flags clear
contradictions (the sense-mismatch class the lemma-only verifier is blind to, e.g.
'hov' glossed "royal court" linked to "hoof"). Precision-tuned → human re-review.

    uv run audit_decisions.py            # human-readable disagreement report (pure data)
    uv run audit_decisions.py --json     # JSON + CC0 evidence (pipe into a panel)
    uv run audit_decisions.py --gloss    # LLM: flag links whose CC0 gloss contradicts the anchor
"""

import argparse
import collections
import json
import sqlite3
import subprocess

import config
from review_queue import load_decisions

CONFIRMED = ("approved", "written")  # human said the verifier-'yes' was correct
LANGNAME = {"de": "German", "en": "English", "sv": "Swedish"}

# --gloss audit: the verifier sees only the lemma, so it can't tell which sense a
# monosemous lexeme holds. The lexeme's OWN CC0 Wikidata gloss can — when it plainly
# describes a different concept than the anchor (e.g. 'hov' glossed "royal court"
# linked to "hoof"). Tuned for PRECISION: only clear contradictions, because raw gloss
# signal is noisy (terse/wrong-language/synonym glosses) — it routes to human re-review.
GLOSS_SYSTEM = (
    "You are a lexicographer auditing Wikidata P5137 links for SENSE MISMATCHES. Each item gives a "
    "WORD, its LANGUAGE, the word's own Wikidata sense GLOSS, and the linked concept (CC0 label + "
    "description). Answer 'contradiction' ONLY when the gloss clearly describes a GENUINELY "
    "DIFFERENT concept than the linked one (e.g. gloss 'group of people surrounding a leader' "
    "[royal court] vs concept 'hoof' -> contradiction). Wikidata glosses are often terse, in "
    "another language, a bare synonym, or slightly broader/narrower -- those are NOT "
    "contradictions; answer 'ok'. Use 'unsure' only if the gloss is empty or impossible to read. "
    'Reply with ONLY a JSON array, one object per item in order: '
    '[{"i":1,"verdict":"contradiction|ok|unsure","reason":"<=10 words"}].'
)


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


def best_gloss(wd, lid, lang):
    """The lexeme's own CC0 Wikidata sense gloss, target-language preferred."""
    vals = {lg: g for lg, g in wd.execute("SELECT lang, gloss FROM sense_gloss WHERE lid=?", [lid])}
    for code in [lang, "en", "de", "sv"]:
        if vals.get(code):
            return vals[code]
    return next(iter(vals.values()), "")


def run_gloss_batch(rows, model):
    lines = []
    for n, (d, gloss, lab, desc) in enumerate(rows, 1):
        lines.append(
            f'{n}. word="{d["target_lemma"]}" ({LANGNAME.get(d["target_lang"], d["target_lang"])}) '
            f'sense_gloss="{gloss}" | concept {d["anchor_qid"]}: label="{lab}" description="{desc}"')
    out = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--model", model, "--system-prompt", GLOSS_SYSTEM],
        input="Items:\n" + "\n".join(lines), capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:300])
    result = json.loads(out.stdout)["result"].strip()
    if result.startswith("```"):
        result = result.split("\n", 1)[1].rsplit("```", 1)[0]
    by_i = {v.get("i"): v for v in json.loads(result)}
    return [by_i.get(n, {"verdict": "parse_error", "reason": ""}) for n in range(1, len(rows) + 1)]


def gloss_audit(dec, it, wd, model, batch):
    """Flag confirmed (approved/written) links whose lexeme CC0 gloss contradicts the anchor."""
    rows = []
    for (lid, qid), d in dec.items():
        if d["status"] not in CONFIRMED:
            continue
        g = best_gloss(wd, lid, d["target_lang"])
        if not g:
            continue
        lab, desc = cc0(it, qid, d["target_lang"])
        rows.append((d, g, lab, desc))
    flagged = []
    for i in range(0, len(rows), batch):
        b = rows[i:i + batch]
        try:
            verdicts = run_gloss_batch(b, model)
        except Exception as e:  # noqa: BLE001 — one bad batch shouldn't kill the run
            print(f"  batch {i // batch} failed: {e}")
            continue
        for (d, g, lab, desc), v in zip(b, verdicts):
            if v.get("verdict") == "contradiction":
                flagged.append((d, g, lab, desc, v.get("reason", "")))
    return rows, flagged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true",
                    help="emit disagreements with CC0 anchor evidence (for a panel)")
    ap.add_argument("--gloss", action="store_true",
                    help="LLM audit: flag approved/written links whose lexeme CC0 gloss "
                         "contradicts the anchor (precision-tuned; routes to human re-review)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--batch", type=int, default=8)
    args = ap.parse_args()

    dec = load_decisions()
    if not dec:
        raise SystemExit(f"no decisions at {config.DECISIONS}")
    vf = sqlite3.connect(config.VERIFY)
    verdicts = {(a, b): (v, r) for a, b, v, r in
                vf.execute("SELECT target_lid, anchor_qid, verdict, reason FROM verdict")}
    it = sqlite3.connect(config.ITEMS)

    if args.gloss:
        wd = sqlite3.connect(config.WIKIDATA)
        rows, flagged = gloss_audit(dec, it, wd, args.model, args.batch)
        print(f"gloss-contradiction audit: {len(rows)} confirmed links with a CC0 gloss "
              f"(model={args.model})")
        print(f"\n=== FLAGGED for re-review — lexeme gloss contradicts anchor: {len(flagged)} ===")
        for d, g, lab, desc, reason in sorted(flagged, key=lambda x: (x[0]["target_lang"], x[0]["target_lemma"])):
            print(f"  {d['target_lang']} {d['target_lemma']:<16} {d['target_lid']}/{d['anchor_qid']}  "
                  f"human={d['status']}")
            print(f"     gloss : {g}")
            print(f"     anchor: {lab} — {desc}")
            print(f"     why   : {reason}")
        print("\n(precision-tuned: clear contradictions only; human re-review, not auto-action)")
        return

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
