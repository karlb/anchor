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
and excluded from the keep/catch rates (which isolate the prompt's effect).

Since the verdict cache keeps one row per (pair, prompt version), the baseline no longer has
to be a separate backup FILE: --baseline-ver grades the live prompt against an earlier one out
of the same cache, which is the A/B the additive cache exists to make free.

    uv run validate_prompt.py                         # vs data/verify.pre-rec4.sqlite3
    uv run validate_prompt.py --baseline-ver 59d6f472 # vs an earlier prompt, same cache file
    uv run validate_prompt.py --baseline path.sqlite3 # explicit backup file
    uv run validate_prompt.py --keep-min 0.9 --catch-min 0.5
"""

import argparse
import collections
import os
import sqlite3

import config
import verify
from review_queue import CONFIRMED, load_decisions, load_verify_cache

ABSENT = "(out-of-scope)"            # gold row not present in the new cache


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

    Returns (n, present, counts) where counts maps verdict (or ABSENT) -> n."""
    keys = [k for k, r in gold.items() if keep(r["status"])]
    counts = collections.Counter(new.get(k, ABSENT) for k in keys)
    present = sum(n for v, n in counts.items() if v != ABSENT)
    return len(keys), present, counts


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
    ap.add_argument("--keep-min", type=float, default=0.90,
                    help="min fraction of confirmed-correct that must stay 'yes'")
    ap.add_argument("--catch-min", type=float, default=0.50,
                    help="min fraction of human-rejected that must flip to non-'yes'")
    args = ap.parse_args()

    gold = load_decisions()
    new = load_cache(config.VERIFY)
    base = (load_cache(config.VERIFY, ver=args.baseline_ver) if args.baseline_ver
            else load_cache(args.baseline, ver=None))
    base_name = (f"{config.VERIFY} @ prompt {args.baseline_ver}" if args.baseline_ver
                 else args.baseline)
    if not gold:
        raise SystemExit(f"no decisions at {config.DECISIONS}")
    if not new:
        raise SystemExit(f"no verdicts at {config.VERIFY} — rebuild the cache first")

    n_def = sum(1 for r in gold.values() if r["status"] == "deferred")
    defnote = f"; {n_def} deferred (held, excluded from scoring)" if n_def else ""
    print(f"decision log : {config.DECISIONS}  ({len(gold)} gold rows, "
          f"all verifier-'yes' under the old prompt{defnote})")
    print(f"new cache    : {config.VERIFY} @ prompt {verify.PROMPT_VER}  "
          f"({len(new)} verdicts; model: {models(config.VERIFY)})")
    if base:
        print(f"baseline     : {base_name}  ({len(base)} verdicts)")

    # confirmed-correct should STAY 'yes' (recall); rejected should FLIP (the target)
    n_ok, ok_present, ok = group(gold, new, lambda s: s in CONFIRMED)
    kept = ok.get("yes", 0)
    keep_rate = kept / ok_present if ok_present else 0.0
    n_bad, bad_present, bad = group(gold, new, lambda s: s == "rejected")
    caught = bad_present - bad.get("yes", 0)
    catch_rate = caught / bad_present if bad_present else 0.0

    print("\nGOLD SET — new prompt vs human truth")
    print(f"  confirmed-correct (n={n_ok})  should STAY 'yes':\n    {fmt(ok)}")
    print(f"    kept {kept}/{ok_present} = {100 * keep_rate:.1f}%"
          + (f"   [{ok[ABSENT]} now out-of-scope]" if ok.get(ABSENT) else ""))
    print(f"  human-rejected    (n={n_bad})  should FLIP to non-'yes':\n    {fmt(bad)}")
    print(f"    caught {caught}/{bad_present} = {100 * catch_rate:.1f}%"
          + (f"   [{bad[ABSENT]} now out-of-scope]" if bad.get(ABSENT) else ""))

    if base:
        was_yes = [k for k, v in base.items() if v == "yes"]
        flipped = sum(1 for k in was_yes if new.get(k, ABSENT) != "yes")
        print("\nWHOLE-CACHE CHANGE (vs baseline)")
        print(f"  old 'yes' -> non-'yes': {flipped}/{len(was_yes)} = "
              f"{100 * flipped / len(was_yes):.1f}%   (the stricter prompt's net rejections)")
        if flipped == 0:
            print("  NOTE: nothing flipped — has the cache actually been rebuilt with the new prompt?")

    ok_keep = keep_rate >= args.keep_min
    ok_catch = catch_rate >= args.catch_min
    print(f"\nCRITERION  keep >= {100 * args.keep_min:.0f}% · catch >= {100 * args.catch_min:.0f}%")
    print(f"  VERDICT: {'PASS' if ok_keep and ok_catch else 'FAIL'}"
          + ("" if ok_keep else f"  (keep {100 * keep_rate:.1f}% — prompt too strict, hurts recall)")
          + ("" if ok_catch else f"  (catch {100 * catch_rate:.1f}% — prompt misses known false positives)"))


if __name__ == "__main__":
    main()
