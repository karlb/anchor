#!/usr/bin/env python3
"""Score each verifier approach against the locked 53-case gold (config.GOLD).

Arm actions are recomputed LIVE from the raw (verdict, reason) ab cache via the current
ab.predicted_action -> review_queue.scope_kind, so this always reflects the current nudge
logic (not the snapshot columns in the CSV). Sample votes are majority-voted; a TIE -> defer
(a split verifier belongs in human review). The gold gives each case a single best output
plus an ACCEPTABLE-OUTPUT SET parsed from the {..} in its rationale (defer is a valid triage
action, never an error).

Cases whose gold is itself 'defer' (proposed_correct == 'defer') are excluded from the
false-keep / false-drop tallies: we defer because a human can't reliably judge the pair, so
we can't say the verifier's keep or reject is wrong (in-set still credits landing on defer).

Metrics per arm:
  in-set     prediction in the acceptable set                    (higher better)
  false-keep predicted 'keep' but 'keep' not acceptable, non-defer gold   (worst: writes a bad link)
  false-drop predicted 'reject' but 'reject' not acceptable, non-defer gold  (drops a good link)
  defers     predicted 'defer'                                    (review burden — fine if needed)
"""
import collections
import csv
import re

import ab_concept as ab
import config
import review_queue as rq

ARMS = {"base": "base", "+multi": "multi", "+nudge": "nudge"}   # cache arm name -> short column name
COLS = ("base", "multi", "nudge")


def acceptable(r):
    m = re.search(r"\{([^}]*)\}", r["rationale"])
    return {x.strip() for x in m.group(1).split(",")} if m else {r["proposed_correct"]}


def main():
    gold = {(r["lid"], r["qid"]): r for r in csv.DictReader(open(config.GOLD))}

    # gloss + de/en/sv anchor description per gold pair, for the demonym→ethnic nudge —
    # the same batched context the live queue and calibrate feed the classifier.
    ctx = rq.demonym_context([(lid, qid, gold[(lid, qid)]["lang"]) for lid, qid in gold])

    # vote from the DEDUPED cache (last event per (arm, sample, lid, qid) wins, matching
    # ab_concept.load_cache) — a resumed run re-appends whole batches to the jsonl, and
    # counting raw lines double-counts those pairs, enough to flip a majority
    votes = collections.defaultdict(collections.Counter)
    for (arm, _s, lid, qid), (verdict, reason) in ab.load_cache().items():
        k = (lid, qid)
        if k in gold and arm in ARMS:
            gloss, _glang, adesc = ctx.get(k, (None, None, None))
            act = ab.predicted_action(verdict, reason, gloss, adesc)
            votes[(ARMS[arm], k)][act] += 1
    pred = {ak: ab.vote_action(c) for ak, c in votes.items()}   # sample tie -> defer

    # A gold case is only scored for an arm once that arm has cached verdicts for it (so a
    # half-run cache doesn't silently score uncovered cases as wrong). Coverage is reported.
    print(f"scoring {len(gold)} gold cases (live from cache; sample-tie -> defer)\n")
    print(f"{'arm':<10}{'in-set':>9}{'false-keep':>12}{'false-drop':>12}{'defers':>9}{'covered':>9}")
    bad = {col: [] for col in COLS}
    for col in COLS:
        inset = fk = fd = defers = n = 0
        for k, r in gold.items():
            if (col, k) not in pred:
                continue                       # arm not yet verified for this case
            n += 1
            acc, p = acceptable(r), pred[(col, k)]
            gold_defer = r["proposed_correct"] == "defer"   # human can't judge -> keep/reject can't be "wrong"
            inset += p in acc
            defers += (p == "defer")
            if not gold_defer and p == "keep" and "keep" not in acc:
                fk += 1
                bad[col].append(f"FALSE-KEEP {r['lang']}:{r['lemma']} (gold {r['proposed_correct']})")
            if not gold_defer and p == "reject" and "reject" not in acc:
                fd += 1
                bad[col].append(f"FALSE-DROP {r['lang']}:{r['lemma']} (gold {r['proposed_correct']})")
        rate = f"{inset / n:.0%}" if n else "-"
        print(f"{col:<10}{rate:>9}{fk:>12}{fd:>12}{defers:>9}{n:>6}/{len(gold)}")

    print("\n--- dangerous errors by arm (false-keep / false-drop) ---")
    for col in COLS:
        print(f"\n{col}: {len(bad[col])} dangerous")
        for b in bad[col]:
            print(f"    {b}")


if __name__ == "__main__":
    main()
