"""Shared paths for the P5137 anchor-propagation pipeline.

Input data lives in the wikdict-gen repo (read-only); output goes into this
project's ``data/`` dir. Override the wikdict-gen location with $WIKDICT_GEN.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
WIKDICT_GEN = os.environ.get("WIKDICT_GEN", "/home/karl/code/github/wikdict-gen")

# inputs (ready-built wikdict-gen dictionaries — treat as read-only)
INFER = os.path.join(WIKDICT_GEN, "dictionaries", "infer.sqlite3")
PROCESSED = os.path.join(WIKDICT_GEN, "dictionaries", "processed", "%s.sqlite3")

# outputs (this project)
DATA = os.path.join(HERE, "data")
CLUSTERS = os.path.join(DATA, "clusters.sqlite3")
WIKIDATA = os.path.join(DATA, "wikidata.sqlite3")
CANDIDATES = os.path.join(DATA, "candidates.sqlite3")
DUMP = os.path.join(DATA, "latest-lexemes.json.gz")
PRECISION = os.path.join(DATA, "precision.sqlite3")
ITEMS = os.path.join(DATA, "wd_items.sqlite3")  # cached CC0 labels/descriptions + P31/P279
VERIFY = os.path.join(DATA, "verify.sqlite3")    # LLM yes/no verdicts
