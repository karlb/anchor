#!/usr/bin/env python3
"""Per-relation calibration of the human decision log — the instrument that says when
(if ever) a verifier rating class is safe to auto-decide, and surfaces the exact pairs
an auto-rule would get WRONG.

It DECIDES NOTHING and writes nothing. It replays config.DECISIONS, buckets each decided
pair by the verifier's relation token (EQUAL/BROADER/NARROWER/LATERAL/…, with the
demonym/gender convention split out exactly as review_queue.scope_hint does it), maps
each bucket to the action scope_hint would suggest (keep / defer / reject), and prints how
the human actually ruled plus an ADVISORY readiness flag against the gates below. Then it
lists the CONTRADICTIONS — decided pairs where the human verdict disagrees with the
verifier rating in the direction the live gate would get wrong — so they can be eyeballed.

    uv run calibrate.py            # print the table + contradictions

Gates (user call 2026-06-27 — avoid wrong writes "by all cost"). Denominator is always
the bucket's human-decided pairs (approved + rejected + deferred):
  • auto-approve (keep-rated; WRITES data — the high two-part bar):
        >=98% approve-or-defer  (<=2% rejected — the never-write-wrong floor)  AND
        >=80% a full approve    (so we don't auto-write a bucket humans only ever defer).
  • auto-hide reject (reject-rated; drops verifier-'no' from the queue so humans don't see it):
        >=80% human-rejected.
  • auto-defer (defer-rated; reversible — nothing is written, the low bar):
        >=85% not written (reject+defer).
Every gate also needs at least MIN_SAMPLES decided pairs.

'written' folds into the approve count (write.py only writes approved pairs). 'deferred'
is a hold (nothing written): it counts TOWARD the safety floor (a defer is not a wrong
write) but AGAINST the approve floor (a defer is not a confirmation).
"""

import collections

import review_queue as rq
from review_queue import rel_class, REL_ACTION as ACTION   # single-sourced taxonomy + auto-defer gate

# The auto-approve / auto-hide floors below are calibrate-only (the instrument). The auto-defer gate
# (MIN_SAMPLES, NOWRITE_AUTO) and the relation taxonomy (rel_class, ACTION) are imported from
# review_queue — the live pending-filter there enforces the SAME gate, so they can't drift apart.
MIN_SAMPLES = rq.GATE_MIN_SAMPLES
NOWRITE_AUTO = rq.AUTO_DEFER_NOWRITE   # (reject+defer)/decided >= this -> auto-defer (reversible)
SAFE_FLOOR = 0.98      # (approve+defer)/decided >= this  -> <=2% human-rejected (auto-approve)
APPROVE_FLOOR = 0.80   # approve/decided >= this          -> bulk are real approves (auto-approve)
REJECT_FLOOR = 0.80    # reject/decided >= this           -> safe to auto-hide a reject-rated class


def readiness(cls, appr, rej, defer):
    """ADVISORY flag for a bucket: which gate (if any) it has cleared, or why not."""
    action = ACTION.get(cls)
    decided = appr + rej + defer
    if action is None:
        return "manual (not a current-prompt rating)"
    if decided < MIN_SAMPLES:
        return f"hold (need {MIN_SAMPLES - decided} more)"
    if action == "keep":
        safe = (appr + defer) / decided        # <=2% rejected
        approve = appr / decided               # bulk are real approves
        if safe >= SAFE_FLOOR and approve >= APPROVE_FLOOR:
            return f"→ AUTO-APPROVE (safe {safe:.0%}, appr {approve:.0%})"
        why = []
        if safe < SAFE_FLOOR:
            why.append(f"safe {safe:.0%}<98 ({rej} rej)")
        if approve < APPROVE_FLOOR:
            why.append(f"appr {approve:.0%}<80")
        return "manual — " + ", ".join(why)
    if action == "reject":
        rrate = rej / decided
        return (f"→ AUTO-HIDE ({rrate:.0%} reject)" if rrate >= REJECT_FLOOR
                else f"manual — {rrate:.0%} reject <80")
    nowrite = (rej + defer) / decided          # action == "defer" (reversible)
    return (f"→ auto-defer ({nowrite:.0%} not written)" if nowrite >= NOWRITE_AUTO
            else "manual (mixed)")


def _fmt(d):
    """One contradiction line: lang lemma → anchor, the human verdict, and the reason."""
    reason = " ".join((d.get("reason") or "").split())
    if len(reason) > 64:
        reason = reason[:63] + "…"
    return (f"    {(d.get('target_lang') or '??'):<3} {(d.get('target_lemma') or '?'):<16}"
            f" → {(d.get('anchor_qid') or '?'):<9} {(d.get('status') or ''):<9} {reason}")


def main():
    decisions = rq.load_decisions()   # last event per (lid, anchor) wins
    buckets = collections.defaultdict(collections.Counter)
    rows_by_cls = collections.defaultdict(list)
    for d in decisions.values():
        st = (d.get("status") or "").strip()
        if st not in ("approved", "rejected", "deferred", "written"):
            continue
        cls = rel_class(d.get("reason"))
        buckets[cls][st] += 1
        rows_by_cls[cls].append(d)

    print(f"decision log: {rq.DECISIONS}")
    print(f"{'class':<20}{'gate':>8}{'n':>5}{'appr':>6}{'rej':>5}{'defer':>6}   readiness")
    for cls, c in sorted(buckets.items(), key=lambda kv: -sum(kv[1].values())):
        appr = c["approved"] + c["written"]   # written == approved-then-written
        rej, defer = c["rejected"], c["deferred"]
        n = appr + rej + defer
        gate = ACTION.get(cls, "—")
        print(f"{cls:<20}{gate:>8}{n:>5}{appr:>6}{rej:>5}{defer:>6}   "
              f"{readiness(cls, appr, rej, defer)}")

    # ---- contradictions: the pairs a live gate would get WRONG ------------------
    # Surface exactly the band the gates tolerate: a keep-rated pair the human REJECTED
    # is a wrong write auto-approve would make; a reject-rated pair the human KEPT is a
    # good link auto-hide would silently drop. Listed so they can be eyeballed, never
    # bulk-trusted. (Legacy/pre-REL pairs aren't a current-prompt rating, so excluded.)
    keep_rej = [d for cls, rows in rows_by_cls.items() if ACTION.get(cls) == "keep"
                for d in rows if (d.get("status") or "").strip() == "rejected"]
    reject_kept = [d for cls, rows in rows_by_cls.items() if ACTION.get(cls) == "reject"
                   for d in rows if (d.get("status") or "").strip() != "rejected"]
    by_lemma = lambda d: (d.get("target_lang") or "", d.get("target_lemma") or "")

    print("\nContradictions the gates would tolerate:")
    print(f"  keep-rated but human-REJECTED — auto-approve would WRONGLY WRITE ({len(keep_rej)}):")
    for d in sorted(keep_rej, key=by_lemma):
        print(_fmt(d))
    if not keep_rej:
        print("    (none)")
    print(f"  reject-rated but human-KEPT — auto-hide would WRONGLY DROP ({len(reject_kept)}):")
    for d in sorted(reject_kept, key=by_lemma):
        print(_fmt(d))
    if not reject_kept:
        print("    (none reviewed yet — surface a sample of verifier-'no' pairs to measure this gate)")


if __name__ == "__main__":
    main()
