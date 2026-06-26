#!/usr/bin/env python3
"""Score each verifier approach against the locked 53-case gold (notes/problematic_cases.csv).

The gold gives, per case, the single best output (proposed_correct) plus an ACCEPTABLE-OUTPUT
SET parsed from the {..} in the rationale (defer is a valid triage action, never an error).
Each arm's predicted action is already a column in the CSV (base/descs/alllangs/guided).

Metrics per arm:
  in-set    prediction is in the acceptable set                     (higher better — gets it right)
  false-keep predicted 'keep' but 'keep' not acceptable             (worst error: would write a bad link)
  false-drop predicted 'reject' but 'reject' not acceptable         (drops a good/uncertain link)
  defers    predicted 'defer'                                       (review burden — fine if needed)
"""
import csv
import re

ARMS = [("base", "base"), ("+descs", "descs"), ("+alllangs", "alllangs"), ("guided", "guided")]
rows = list(csv.DictReader(open("notes/problematic_cases.csv")))


def acceptable(r):
    m = re.search(r"\{([^}]*)\}", r["rationale"])
    if m:
        return {x.strip() for x in m.group(1).split(",") if x.strip()}
    return {r["proposed_correct"]}


print(f"scoring {len(rows)} gold cases\n")
print(f"{'arm':<12}{'in-set':>9}{'false-keep':>12}{'false-drop':>12}{'defers':>9}")
for name, col in ARMS:
    inset = fk = fd = defers = 0
    for r in rows:
        acc = acceptable(r)
        p = r[col]
        inset += p in acc
        fk += (p == "keep" and "keep" not in acc)
        fd += (p == "reject" and "reject" not in acc)
        defers += (p == "defer")
    n = len(rows)
    print(f"{name:<12}{inset/n:>8.0%}{fk:>12}{fd:>12}{defers:>9}")

# per-arm detail of the dangerous errors (false-keep / false-drop)
print("\n--- dangerous errors by arm ---")
for name, col in ARMS:
    bad = []
    for r in rows:
        acc = acceptable(r)
        p = r[col]
        if p == "keep" and "keep" not in acc:
            bad.append(f"FALSE-KEEP {r['lang']}:{r['lemma']} (gold {r['proposed_correct']})")
        if p == "reject" and "reject" not in acc:
            bad.append(f"FALSE-DROP {r['lang']}:{r['lemma']} (gold {r['proposed_correct']})")
    print(f"\n{name}: {len(bad)} dangerous")
    for b in bad:
        print(f"    {b}")
