#!/usr/bin/env python3
"""Per-relation calibration of the human decision log — the instrument that tells us
when (if ever) a verifier relation class is safe to auto-decide.

It DECIDES NOTHING and writes nothing. It replays config.DECISIONS, buckets each
decided pair by the verifier's relation token (EQUAL/BROADER/NARROWER/LATERAL, with
the demonym/gender convention split out exactly as review_queue.scope_hint does it),
and prints how the human actually ruled on each bucket plus an ADVISORY readiness
flag. A class earns an auto-rule only once it clears a precision threshold on a
minimum sample count here — so automation follows the data instead of a guess.

    uv run calibrate.py            # print the table

'written' folds into the approve count (write.py only writes approved pairs).
'deferred' is a hold: excluded from the clean-rate denominator (matching
review_queue's precision convention) but shown in full, since auto-defer — being
reversible (nothing is written) — is itself a candidate action with a lower bar.
"""

import collections

import review_queue as rq

# Advisory graduation band: a class is flagged ready only with at least MIN_SAMPLES
# decided pairs AND a rate past an edge. Tunable — these are not load-bearing yet.
MIN_SAMPLES = 30
APPROVE_AUTO = 0.97   # clean-rate >= this -> candidate auto-approve (writes data, high bar)
NOWRITE_AUTO = 0.85   # (reject+defer)/n >= this -> candidate auto-defer (reversible, lower bar)

KNOWN = ("EQUAL", "BROADER", "NARROWER", "LATERAL", "INCOHERENT", "DIFFERENT")


def rel_class(reason):
    """Bucket a decision by its verifier relation, matching scope_hint's split.

    Rows verified before the REL-token convention carry free-text reasons; they
    group under '(legacy, pre-REL)' and don't inform current-prompt calibration."""
    rel, suggestion, _ = rq.scope_hint(reason)
    if rel not in KNOWN:
        return "(legacy, pre-REL)"
    if rel in ("BROADER", "NARROWER") and "convention" in suggestion:
        return rel + "·convention"
    return rel


def readiness(appr, rej, defer):
    decided = appr + rej + defer
    if decided < MIN_SAMPLES:
        return f"hold (need {MIN_SAMPLES - decided} more)"
    clean = appr / (appr + rej) if (appr + rej) else 0.0
    nowrite = (rej + defer) / decided
    if clean >= APPROVE_AUTO:
        return f"→ auto-approve? ({clean:.0%} clean)"
    if nowrite >= NOWRITE_AUTO:
        return f"→ auto-defer? ({nowrite:.0%} not written)"
    return "manual (mixed)"


def main():
    decisions = rq.load_decisions()   # last event per (lid, anchor) wins
    buckets = collections.defaultdict(collections.Counter)
    for d in decisions.values():
        st = (d.get("status") or "").strip()
        if st not in ("approved", "rejected", "deferred", "written"):
            continue
        buckets[rel_class(d.get("reason"))][st] += 1

    print(f"decision log: {rq.DECISIONS}")
    print(f"{'class':<20}{'n':>5}{'appr':>6}{'writ':>6}{'rej':>5}{'defer':>6}"
          f"{'clean':>8}   readiness")
    for cls, c in sorted(buckets.items(), key=lambda kv: -sum(kv[1].values())):
        appr = c["approved"] + c["written"]   # written == approved-then-written
        rej, defer = c["rejected"], c["deferred"]
        n = appr + rej + defer
        clean = appr / (appr + rej) if (appr + rej) else 0.0
        print(f"{cls:<20}{n:>5}{c['approved']:>6}{c['written']:>6}{rej:>5}{defer:>6}"
              f"{clean:>7.0%}   {readiness(appr, rej, defer)}")


if __name__ == "__main__":
    main()
