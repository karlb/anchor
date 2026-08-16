#!/usr/bin/env python3
"""Did a verifier-prompt change improve on the human-gold decisions? — no LLM cost.

Every row in the decision log was a verifier-'yes' candidate that a human then
judged (config.DECISIONS), so the log is a free, already-paid gold set:
  * approved/written  -> the verifier was RIGHT to say 'yes'  (should STAY 'yes')
  * rejected          -> the verifier was WRONG (a false positive the prompt let
                         through; a better prompt should FLIP it to non-'yes')

After rebuilding the verdict cache with a new prompt, this joins the fresh
verdicts (config.VERIFY) onto those gold rows and reports, with zero extra LLM
calls, whether the new prompt keeps the good ones and catches the bad ones. If a
baseline cache (the pre-change backup) is given, it also reports how many old
'yes' verdicts the new prompt now rejects across the whole cache — the magnitude
of the change.

A row absent from the new cache is OUT-OF-SCOPE (a structural-gate change, e.g.
QW-A's scope tightening), not a prompt rejection, so it is reported separately
and excluded from the keep/catch rates (which isolate the prompt's effect). So
are error/parse_error rows: a failed batch is not a verdict, and counting one as
a caught false positive could flip FAIL to PASS after an outage.

CATCH IS SCOPED TO THE WRITE LANE. The criterion is deliberately tolerant — it
passes broader/narrower/lateral/demonym-ethnic pairs through as 'yes' and leaves
the call to the scope hints and the auto-defer gate, which is why a human rejects
them at review and why they are never written. Counting those as verifier misses
scored a design decision as a failure and made this report contradict
`review_queue.py status` about the same prompt on the same data. The gated rate is
therefore: of the rejections the verifier either caught or would have WRITTEN
(EQUAL / convention-keep), how many did it catch? Triaged rejections are reported
on their own line.

Since the verdict cache keeps one row per (pair, prompt version), the baseline no longer has
to be a separate backup FILE: --baseline-ver grades the live prompt against an earlier one out
of the same cache, which is the A/B the additive cache exists to make free.

    uv run validate_prompt.py                         # vs data/verify.pre-rec4.sqlite3
    uv run validate_prompt.py --baseline-ver 59d6f472 # vs an earlier prompt, same cache file
    uv run validate_prompt.py --baseline path.sqlite3 # explicit backup file
    uv run validate_prompt.py --keep-min 0.9 --catch-min 0.5
    uv run validate_prompt.py --decisions notes/runs/senseless_sample.csv   # a measured lane

--decisions grades against a different log of the same shape. A lane whose precision was
measured by adjudicating a sample must keep those adjudications OUT of config.DECISIONS —
write.py writes what is 'approved' there — but they are exactly the gold a prompt change
should be graded on, so the instrument reads whichever file it is pointed at.
"""

import argparse
import collections
import os
import sqlite3

import config
import verify
from review_queue import (CONFIRMED, REL_ACTION, demonym_context, live_rel,
                          load_decisions, load_verify_cache)

ABSENT = "(out-of-scope)"            # gold row not present in the new cache
FAILED = ("error", "parse_error")    # a failed batch — infrastructure, not a verdict


def load_cache(path, ver=verify.PROMPT_VER):
    """{(target_lid, anchor_qid): verdict} from a verify.sqlite3, or {} if missing.

    ver=None reads every row regardless of prompt version — right for an old single-prompt
    backup file, wrong for the live cache, which now holds several versions at once."""
    return {k: v[0] for k, v in load_verify_cache(path or "", ver=ver).items()}


def models(path):
    if not path or not os.path.exists(path):
        return ""
    conn = sqlite3.connect(path)
    ms = [m for (m,) in conn.execute(
        "SELECT model FROM verdict GROUP BY model ORDER BY count(*) DESC")]
    conn.close()
    return ", ".join(ms)


def group(gold, new, keep):
    """Tabulate new-cache verdicts for the gold rows where keep(status) holds.

    Returns (n, gradable, counts) where counts maps verdict (or ABSENT) -> n. `gradable`
    excludes ABSENT (a structural-gate change dropped the row — not a prompt decision) and
    error/parse_error (a failed batch — not a decision at all). Counting failures as verdicts
    let an outage move the rates in both directions: every error scored as a caught false
    positive, so a cache rebuilt during one could flip FAIL to PASS at the --catch-min check,
    while the keep side was symmetrically penalised for the same rows."""
    keys = [k for k, r in gold.items() if keep(r["status"])]
    counts = collections.Counter(new.get(k, ABSENT) for k in keys)
    gradable = sum(n for v, n in counts.items() if v != ABSENT and v not in FAILED)
    return len(keys), gradable, counts


def catch_split(gold, cache, ctx):
    """Split the human-rejected gold rows by what the live prompt does with each.

    Counter over caught | missed | triaged | failed | ABSENT.

    Not every human rejection is a verifier miss, and treating them alike made this report
    contradict `review_queue.py status` about the same prompt on the same data. The criterion
    is deliberately TOLERANT: it passes BROADER / NARROWER / LATERAL / demonym-ethnic pairs
    through as 'yes' and leaves the call to the scope hints and the auto-defer gate — which is
    why a human rejects them at review and why they are never written. Scoring those as misses
    grades the verifier on a decision it does not make.

    So the gated rate is the WRITE-LANE one: of the rejections the verifier either caught or
    would have written (EQUAL / convention-keep), how many did it catch? That is the same lens
    `review_queue.status` reports verifier-keep precision through, so the two now agree by
    construction — its write-lane misses are exactly this function's `missed`."""
    out = collections.Counter()
    for k, d in gold.items():
        if (d.get("status") or "").strip() != "rejected":
            continue
        live = cache.get(k)
        if live is None:
            out[ABSENT] += 1
        elif live[0] in FAILED:
            out["failed"] += 1
        elif live[0] != "yes":
            out["caught"] += 1
        elif REL_ACTION.get(live_rel(k, d.get("reason"), cache, ctx)) in ("defer", "reject"):
            out["triaged"] += 1
        else:
            # Everything else is write-lane: an explicit keep (EQUAL / convention), but ALSO an
            # unclassifiable reason — rel_class returns '(legacy, pre-REL)' for those and they
            # never auto-defer, so they reach the queue as ordinary keep candidates. Defaulting
            # the unknown case to 'missed' keeps the excuse narrow: only a relation the triage
            # layer demonstrably acts on is excluded from the rate.
            out["missed"] += 1
    return out


def fmt(counts):
    order = ["yes", "no", "uncertain", "parse_error", "error", ABSENT]
    parts = [f"{v} {counts[v]}" for v in order if counts.get(v)]
    parts += [f"{v} {n}" for v, n in counts.items() if v not in order]
    return " · ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default=os.path.join(config.DATA, "verify.pre-rec4.sqlite3"),
                    help="pre-change cache backup file for the whole-cache flip count")
    ap.add_argument("--baseline-ver",
                    help="compare against an earlier PROMPT_VER inside the live cache instead "
                         "of a backup file (the cache keeps every version)")
    ap.add_argument("--decisions", default=config.DECISIONS,
                    help="grade against a different decision log — same columns, not the live "
                         "one. A lane measured by adjudicating a sample gets its own file (it "
                         "must not authorize writes), and this makes that file a gold set for "
                         "the next prompt at zero LLM cost.")
    ap.add_argument("--keep-min", type=float, default=0.90,
                    help="min fraction of confirmed-correct that must stay 'yes'")
    ap.add_argument("--catch-min", type=float, default=0.50,
                    help="min WRITE-LANE catch: of the human-rejected pairs the verifier either "
                         "caught or would have written, the fraction it must catch")
    args = ap.parse_args()

    gold = load_decisions(args.decisions)
    live = load_verify_cache(config.VERIFY)     # (verdict, reason) — live_rel needs the reason
    new = {k: v[0] for k, v in live.items()}
    base = (load_cache(config.VERIFY, ver=args.baseline_ver) if args.baseline_ver
            else load_cache(args.baseline, ver=None))
    base_name = (f"{config.VERIFY} @ prompt {args.baseline_ver}" if args.baseline_ver
                 else args.baseline)
    if not gold:
        raise SystemExit(f"no decisions at {args.decisions}")
    if not new:
        raise SystemExit(f"no verdicts at {config.VERIFY} — rebuild the cache first")

    n_def = sum(1 for r in gold.values() if r["status"] == "deferred")
    defnote = f"; {n_def} deferred (held, excluded from scoring)" if n_def else ""
    print(f"decision log : {args.decisions}  ({len(gold)} gold rows, "
          f"all verifier-'yes' under the old prompt{defnote})")
    print(f"new cache    : {config.VERIFY} @ prompt {verify.PROMPT_VER}  "
          f"({len(new)} verdicts; model: {models(config.VERIFY)})")
    if base:
        print(f"baseline     : {base_name}  ({len(base)} verdicts)")

    # confirmed-correct should STAY 'yes' (recall); rejected should FLIP (the target)
    n_ok, ok_gradable, ok = group(gold, new, lambda s: s in CONFIRMED)
    kept = ok.get("yes", 0)
    keep_rate = kept / ok_gradable if ok_gradable else 0.0
    n_bad, _bad_gradable, bad = group(gold, new, lambda s: s == "rejected")
    rej = {k: d for k, d in gold.items() if (d.get("status") or "").strip() == "rejected"}
    split = catch_split(gold, live,
                        demonym_context([(k[0], k[1], d.get("target_lang"))
                                         for k, d in rej.items()]))
    caught, missed = split["caught"], split["missed"]
    catch_den = caught + missed
    catch_rate = caught / catch_den if catch_den else 0.0

    def failnote(n):
        return f"   [{n} error rows excluded — infrastructure, not verdicts]" if n else ""

    print("\nGOLD SET — new prompt vs human truth")
    print(f"  confirmed-correct (n={n_ok})  should STAY 'yes':\n    {fmt(ok)}")
    print(f"    kept {kept}/{ok_gradable} = {100 * keep_rate:.1f}%"
          + (f"   [{ok[ABSENT]} now out-of-scope]" if ok.get(ABSENT) else "")
          + failnote(sum(ok.get(v, 0) for v in FAILED)))
    print(f"  human-rejected    (n={n_bad})  should FLIP to non-'yes':\n    {fmt(bad)}")
    print(f"    write-lane catch {caught}/{catch_den} = {100 * catch_rate:.1f}%"
          + (f"   [{split[ABSENT]} now out-of-scope]" if split.get(ABSENT) else "")
          + failnote(split.get("failed", 0)))
    if split["triaged"]:
        print(f"    + {split['triaged']} triaged, not counted: broader/narrower/lateral/"
              "demonym-ethnic rejections the tolerant criterion passes BY DESIGN — stopped by "
              "the scope hints and the auto-defer gate, never written, so not verifier misses")

    if base:
        was_yes = [k for k, v in base.items() if v == "yes"]
        graded = [k for k in was_yes if k in new and new[k] not in FAILED]
        flipped = sum(1 for k in graded if new[k] != "yes")
        print("\nWHOLE-CACHE CHANGE (vs baseline)")
        print(f"  old 'yes' -> non-'yes': {flipped}/{len(graded)} = "
              f"{100 * flipped / len(graded):.1f}%   (the stricter prompt's net rejections)"
              if graded else "  nothing gradable in common with the baseline")
        # Rows the baseline rated 'yes' that the new cache doesn't grade are STRUCTURAL removals
        # (a gate change) or failed batches — counting them as prompt rejections contradicted the
        # keep/catch sections above, which exclude both.
        if len(was_yes) != len(graded):
            print(f"  ({len(was_yes) - len(graded)} baseline 'yes' rows absent from the new cache "
                  "or failed — structural/infrastructure, excluded)")
        if graded and flipped == 0:
            print("  NOTE: nothing flipped — has the cache actually been rebuilt with the new prompt?")

    ok_keep = keep_rate >= args.keep_min
    ok_catch = catch_rate >= args.catch_min
    print(f"\nCRITERION  keep >= {100 * args.keep_min:.0f}% · "
          f"write-lane catch >= {100 * args.catch_min:.0f}%")
    # An empty denominator is NOT a failure. After a prompt change only the re-verified pairs
    # carry a current-version verdict, so a gold row that hasn't been re-verified is out-of-scope
    # on both sides — and `0/0 = 0.0%` used to print "FAIL (catch 0.0% — prompt misses known false
    # positives)" about a prompt that had not been shown a single one of them. Same class of bug
    # as counting a failed batch as a verdict: no evidence must never read as bad evidence.
    if not ok_gradable or not catch_den:
        missing = "confirmed-correct" if not ok_gradable else "human-rejected"
        print(f"  VERDICT: NOT GRADABLE — no {missing} gold row has a verdict under this prompt "
              f"(they are all out-of-scope, not failing). Re-verify the pool at this prompt, or "
              f"point --decisions at a log whose pairs have been.")
        return
    print(f"  VERDICT: {'PASS' if ok_keep and ok_catch else 'FAIL'}"
          + ("" if ok_keep else f"  (keep {100 * keep_rate:.1f}% — prompt too strict, hurts recall)")
          + ("" if ok_catch else f"  (catch {100 * catch_rate:.1f}% — prompt misses known false positives)"))


if __name__ == "__main__":
    main()
