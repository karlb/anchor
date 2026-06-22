"""Shared paths for the P5137 anchor-propagation pipeline.

All data — both the copied wikdict-gen inputs and our generated outputs — lives
in this project's ``data/`` dir, so other jobs rebuilding the wikdict-gen
dictionaries can't interfere mid-run. ``WIKDICT_GEN`` records only the original
source the inputs were copied from (see ``make sync-inputs`` note in README).
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
WIKDICT_GEN = os.environ.get("WIKDICT_GEN", "/home/karl/code/github/wikdict-gen")

DATA = os.path.join(HERE, "data")

# inputs — local copies of the ready-built wikdict-gen dictionaries (read-only)
INFER = os.path.join(DATA, "infer.sqlite3")
PROCESSED = os.path.join(DATA, "%s.sqlite3")
CLUSTERS = os.path.join(DATA, "clusters.sqlite3")
WIKIDATA = os.path.join(DATA, "wikidata.sqlite3")
CANDIDATES = os.path.join(DATA, "candidates.sqlite3")
DUMP = os.path.join(DATA, "latest-lexemes.json.gz")
ITEMS = os.path.join(DATA, "wd_items.sqlite3")  # cached CC0 labels/descriptions + P31/P279
VERIFY = os.path.join(DATA, "verify.sqlite3")    # LLM yes/no verdicts

# The human review decisions — the ONE irreplaceable artifact (every other DB is
# regenerable from the dump + infer.sqlite3). Kept as a committed, append-only
# CSV log OUTSIDE data/ so it is git-tracked and diffable. This file is the single
# source of truth for approvals/rejections/writes; there is no review SQLite DB.
DECISIONS = os.path.join(HERE, "notes", "runs", "decisions.csv")
