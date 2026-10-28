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

import config
import llm
import verify
from review_queue import CONFIRMED, load_decisions, load_verify_cache

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


def cc0(qid, lang, il_map):
    """The anchor's CC0 concept exactly as the live verifier formats it — the audit must
    show the SAME evidence the graded verdict was based on, via the one shared formatter."""
    _q, lab, desc = verify.fmt_concept((None, lang, None, qid, 0),
                                       langs=config.CONCEPT_LANGS_VERIFY, il_map=il_map)
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


def run_gloss_batch(rows, model):
    lines = []
    for n, (d, gloss, lab, desc) in enumerate(rows, 1):
        lines.append(
            f'{n}. word="{d["target_lemma"]}" ({config.LANGNAME.get(d["target_lang"], d["target_lang"])}) '
            f'sense_gloss="{gloss}" | concept {d["anchor_qid"]}: label="{lab}" description="{desc}"')
    return llm.batch_json(GLOSS_SYSTEM, "Items:\n" + "\n".join(lines), model, len(rows),
                          {"verdict": "parse_error", "reason": ""})


def gloss_audit(dec, model, batch, il_map):
    """Flag confirmed (approved/written) links whose lexeme CC0 gloss contradicts the anchor."""
    gmap = verify.load_glosses({lid for (lid, _), d in dec.items() if d["status"] in CONFIRMED})
    rows = []
    for (lid, qid), d in dec.items():
        if d["status"] not in CONFIRMED:
            continue
        g = verify.pick_gloss(gmap, lid, d["target_lang"])
        if not g:
            continue
        lab, desc = cc0(qid, d["target_lang"], il_map)
        rows.append((d, g, lab, desc))
    flagged, audited = [], 0
    for i in range(0, len(rows), batch):
        b = rows[i:i + batch]
        try:
            verdicts = run_gloss_batch(b, model)
        except Exception as e:  # noqa: BLE001 — one bad batch shouldn't kill the run
            print(f"  batch {i // batch} failed: {e} — {len(b)} link(s) NOT audited")
            continue
        for (d, g, lab, desc), v in zip(b, verdicts):
            if v.get("verdict") in ("error", "parse_error"):
                continue        # a failed item is not a clean bill of health
            audited += 1
            if v.get("verdict") == "contradiction":
                flagged.append((d, g, lab, desc, v.get("reason", "")))
    # Report what was actually judged, not what was queued: a dropped batch used to vanish into
    # a summary that still claimed every row had been audited, so an outage read as "nothing
    # contradicts" — the most reassuring possible way to fail.
    return rows, flagged, audited


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
    verdicts = load_verify_cache()
    il_map = verify.load_item_lang({qid for _, qid in dec})

    if args.gloss:
        rows, flagged, audited = gloss_audit(dec, args.model, args.batch, il_map)
        print(f"gloss-contradiction audit: {audited} of {len(rows)} confirmed links with a CC0 "
              f"gloss audited (model={args.model})"
              + (f"  ⚠ {len(rows) - audited} NOT audited — failed batches, re-run to cover them"
                 if audited < len(rows) else ""))
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
                lab, desc = cc0(d["anchor_qid"], d["target_lang"], il_map)
                out.append({
                    "kind": kind, "lid": d["target_lid"], "qid": d["anchor_qid"],
                    "lemma": d["target_lemma"],
                    "lang": config.LANGNAME.get(d["target_lang"], d["target_lang"]),
                    "human_status": d["status"], "verifier_now": verdict,
                    "verifier_reason": reason, "anchor_label": lab, "anchor_desc": desc,
                })
        out.sort(key=lambda x: (x["kind"], x["lang"], x["lemma"]))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    st = collections.Counter(d["status"] for d in dec.values())
    tally = " · ".join(f"{k} {st[k]}" for k in
                       ("approved", "written", "bot_written", "rejected", "deferred") if st.get(k))
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
