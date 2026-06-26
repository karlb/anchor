#!/usr/bin/env python3
"""Score each verifier approach against the locked 53-case gold (notes/problematic_cases.csv).

Arm actions are recomputed LIVE from the raw (verdict, reason) ab cache via the current
ab.predicted_action -> review_queue.scope_hint, so this always reflects the current nudge
logic (not the snapshot columns in the CSV). Sample votes are majority-voted; a TIE -> defer
(a split verifier belongs in human review). The gold gives each case a single best output
plus an ACCEPTABLE-OUTPUT SET parsed from the {..} in its rationale (defer is a valid triage
action, never an error).

Metrics per arm:
  in-set     prediction in the acceptable set                    (higher better)
  false-keep predicted 'keep' but 'keep' not acceptable           (worst: would write a bad link)
  false-drop predicted 'reject' but 'reject' not acceptable       (drops a good/uncertain link)
  defers     predicted 'defer'                                    (review burden — fine if needed)
"""
import collections
import csv
import json
import re

import ab_concept as ab
import config

ARMS = {"base": "base", "+labels+descs": "descs", "+alllangs": "alllangs", "+alllangs-guided": "guided"}
gold = {(r["lid"], r["qid"]): r for r in csv.DictReader(open("notes/problematic_cases.csv"))}


def acceptable(r):
    m = re.search(r"\{([^}]*)\}", r["rationale"])
    return {x.strip() for x in m.group(1).split(",")} if m else {r["proposed_correct"]}


def resolve(counter):
    top = counter.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return "defer"                       # split verifier -> human review
    return top[0][0]


votes = collections.defaultdict(collections.Counter)
with open(config.DATA + "/ab_concept_cache.jsonl") as f:
    for line in f:
        e = json.loads(line)
        k = (e["lid"], e["qid"])
        if k in gold and e["arm"] in ARMS:
            votes[(ARMS[e["arm"]], k)][ab.predicted_action(e["verdict"], e["reason"])] += 1
pred = {ak: resolve(c) for ak, c in votes.items()}

print(f"scoring {len(gold)} gold cases (live from cache; sample-tie -> defer)\n")
print(f"{'arm':<10}{'in-set':>9}{'false-keep':>12}{'false-drop':>12}{'defers':>9}")
for col in ("base", "descs", "alllangs", "guided"):
    inset = fk = fd = defers = 0
    for k, r in gold.items():
        acc = acceptable(r)
        p = pred[(col, k)]
        inset += p in acc
        fk += (p == "keep" and "keep" not in acc)
        fd += (p == "reject" and "reject" not in acc)
        defers += (p == "defer")
    print(f"{col:<10}{inset/len(gold):>8.0%}{fk:>12}{fd:>12}{defers:>9}")

print("\n--- dangerous errors by arm (false-keep / false-drop) ---")
for col in ("base", "descs", "alllangs", "guided"):
    bad = []
    for k, r in gold.items():
        acc, p = acceptable(r), pred[(col, k)]
        if p == "keep" and "keep" not in acc:
            bad.append(f"FALSE-KEEP {r['lang']}:{r['lemma']} (gold {r['proposed_correct']})")
        if p == "reject" and "reject" not in acc:
            bad.append(f"FALSE-DROP {r['lang']}:{r['lemma']} (gold {r['proposed_correct']})")
    print(f"\n{col}: {len(bad)} dangerous")
    for b in bad:
        print(f"    {b}")
