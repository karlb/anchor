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

    uv run calibrate.py                   # print the table + contradictions
    uv run calibrate.py --occupancy       # each class split by same-language slot occupancy
    uv run calibrate.py --lane senseless  # calibrate ONE review lane (see --lane below)

The review lanes are calibrated separately, not split into sub-classes: the senseless
lane is small enough that every bucket would read "hold (need N more)" while its monosemous
half carried the table. --lane FILTERS instead, so each run's MIN_SAMPLES means what it says.
The poly lane (n_senses>=2) is newest and has no decided history yet, so it gates nothing and
is reviewed entirely by hand until it has voted — which is the intended behaviour, not a gap.

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

Rows suffixed '·sole-anchor' are the subset of a class whose gate-A support clusters all trace
to one anchoring lexeme (rq.rel_tokens) — nested, so their n is INCLUDED in the parent row's.
They are live: a refinement that clears the auto-defer gate hides its pairs even when the parent
class does not (BROADER, 2026-08-02).

'written' folds into the approve count (write.py only writes approved pairs). 'deferred'
is a hold (nothing written): it counts TOWARD the safety floor (a defer is not a wrong
write) but AGAINST the approve floor (a defer is not a confirmation).
"""

import argparse
import collections

import review_queue as rq

# The auto-approve / auto-hide floors below are calibrate-only (the instrument). The auto-defer gate
# (MIN_SAMPLES, NOWRITE_AUTO), the relation taxonomy (rq.live_rel / rq.rel_action) and the bucket
# keys it reads (rq.rel_tokens) all come from review_queue — the live pending-filter there enforces
# the SAME gate on the SAME buckets, so the instrument and the filter can't drift apart.
MIN_SAMPLES = rq.GATE_MIN_SAMPLES
NOWRITE_AUTO = rq.AUTO_DEFER_NOWRITE   # (reject+defer)/decided >= this -> auto-defer (reversible)
SAFE_FLOOR = 0.98      # (approve+defer)/decided >= this  -> <=2% human-rejected (auto-approve)
APPROVE_FLOOR = 0.80   # approve/decided >= this          -> bulk are real approves (auto-approve)
REJECT_FLOOR = 0.80    # reject/decided >= this           -> safe to auto-hide a reject-rated class


# --occupancy suffix: the anchor's same-language slot is already filled by an exact-label
# match (rq.occupancy_context). A split bucket faces the SAME gate as its parent class, so
# strip the suffix before the ACTION lookup — rq.rel_action does the same for '·sole-anchor',
# which is not a flag here but a live refinement of the gate (rq.rel_tokens).
OCCUPIED = "·occupied"


def action_of(cls):
    return rq.rel_action(cls[:-len(OCCUPIED)] if cls.endswith(OCCUPIED) else cls)


def readiness(cls, appr, rej, defer):
    """ADVISORY flag for a bucket: which gate (if any) it has cleared, or why not."""
    action = action_of(cls)
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
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--occupancy", action="store_true",
                    help="split each class by whether the anchor's same-language slot is already "
                         "filled by an exact-label match (advisory measurement)")
    ap.add_argument("--lane", choices=("senseless", "mono", "poly"),
                    help="calibrate only one review lane: senseless (target_n_senses 0), mono "
                         "(the monosemous write stream, 1) or poly (>=2); default pools all")
    args = ap.parse_args()

    decisions = rq.load_decisions()   # last event per (lid, anchor) wins
    if args.lane:                     # filter, don't sub-class — see the module docstring
        # Lane names are review_queue's, not a private copy: rq.decided_lane reads the
        # count FROZEN in the log row, so a senseless target that has since been written
        # still calibrates the lane it was judged in.
        decisions = {k: d for k, d in decisions.items() if rq.decided_lane(d) == args.lane}
    cache = rq.load_verify_cache()    # the LIVE prompt's relation per pair (supersedes the
    #                                   frozen log reason, so pre-REL approvals bucket too)
    pairs = [(k[0], k[1], d.get("target_lang")) for k, d in decisions.items()]
    ctx = rq.demonym_context(pairs)
    occ = rq.occupancy_context(pairs) if args.occupancy else {}
    buckets = collections.defaultdict(collections.Counter)
    rows_by_cls = collections.defaultdict(list)
    for k, d in decisions.items():
        st = (d.get("status") or "").strip()
        if st not in ("approved", "rejected", "deferred", "written"):
            continue
        cls = rq.live_rel(k, d.get("reason"), cache, ctx)
        if cls is None:               # the live verifier no longer says 'yes' — not write-eligible
            continue
        # A pair counts under its class AND its ·sole-anchor refinement — the same nested keys the
        # live gate reads (rq.rel_tokens), so a refinement row here IS a subset of its parent row.
        for t in rq.rel_tokens(k, cls):
            buckets[t + (OCCUPIED if occ.get(k) else "")][st] += 1
        # Contradictions list PAIRS, so file each one once, under its base class — the nested
        # refinement would otherwise print it twice. show the live rating, not the frozen one.
        rows_by_cls[cls].append({**d, "reason": cache.get(k, (None, d.get("reason")))[1]})

    print(f"decision log: {rq.DECISIONS}"
          + (f"   [LANE: {args.lane} only]" if args.lane else ""))
    if not args.lane:
        print("pooled over all review lanes — the live auto-defer gate is measured per lane; "
              "run --lane senseless|mono|poly to see the set actually enforced")
    if args.occupancy:
        print(f"{OCCUPIED} = the anchor's same-language slot is already filled by an exact-label "
              "match (advisory split; nothing live keys on it)")
    print(f"{rq.SOLE_ANCHOR} = the SUBSET of its parent class (not a separate bucket — the n's "
          "overlap) whose gate-A clusters all trace to ONE anchoring lexeme")
    width = 20 + len(rq.SOLE_ANCHOR) + len(OCCUPIED) * args.occupancy
    print(f"{'class':<{width}}{'gate':>8}{'n':>5}{'appr':>6}{'rej':>5}{'defer':>6}   readiness")
    for cls, c in sorted(buckets.items(), key=lambda kv: -sum(kv[1].values())):
        appr = c["approved"] + c["written"]   # written == approved-then-written
        rej, defer = c["rejected"], c["deferred"]
        n = appr + rej + defer
        gate = action_of(cls) or "—"
        print(f"{cls:<{width}}{gate:>8}{n:>5}{appr:>6}{rej:>5}{defer:>6}   "
              f"{readiness(cls, appr, rej, defer)}")

    # ---- contradictions: the pairs a live gate would get WRONG ------------------
    # Surface exactly the band the gates tolerate: a keep-rated pair the human REJECTED
    # is a wrong write auto-approve would make; a reject-rated pair the human KEPT is a
    # good link auto-hide would silently drop. Listed so they can be eyeballed, never
    # bulk-trusted. (Bucketed by the live cache relation, so pre-REL pairs are included.)
    keep_rej = [d for cls, rows in rows_by_cls.items() if action_of(cls) == "keep"
                for d in rows if (d.get("status") or "").strip() == "rejected"]
    reject_kept = [d for cls, rows in rows_by_cls.items() if action_of(cls) == "reject"
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
