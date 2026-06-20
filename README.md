# P5137 anchor-propagation (de/en/sv pilot)

Generate reliable **P5137 ("item for this sense")** candidates for Wikidata lexemes by
propagating an *existing* anchor concept across WikDict sense-grounded translation clusters.
The LLM only does a constrained yes/no, verified against the anchor Q-item's **own CC0**
label/description. No DBnary gloss text is ever read or written. See the full method and
license rationale in `notes/p5137_anchor_propagation.md` and the plan in
`~/.claude/plans/i-want-to-improve-twinkling-puppy.md`.

Input data (the ready-built wikdict-gen `dictionaries/*.sqlite3`) is read-only and referenced
via `config.py` (override its location with `$WIKDICT_GEN`); all generated output lands in
`data/` (git-ignored, regenerable).

## Pipeline

```
infer.sqlite3 ──build_clusters.py──▶ clusters.sqlite3 ─┐
                                                       ├─match.py──▶ candidates.sqlite3
latest-lexemes.json.gz ─load_lexemes.py──▶ wikidata.sqlite3 ─┘
```

1. **`build_clusters.py`** — sense-grounded clusters from `dictionaries/infer.sqlite3`.
   Groups translation edges by the structural key `(lexentry, sense_num)` (never the gloss),
   which removes ~30% of naive lemma triples that would otherwise stitch different senses
   together (e.g. de *Bank* = bench vs. financial).
2. **`load_lexemes.py`** — streams the Wikidata lexeme dump, keeps de/en/sv lexemes and each
   sense's P5137 target. Glosses are deliberately dropped (no gloss column exists).
3. **`match.py`** — matches cluster members to noun lexemes (Q1084), finds the single dominant
   anchor Q per cluster, and emits one candidate per `(target lexeme, anchor Q)` with a
   `support_clusters` confidence count. Abstains on conflicting anchors and never overrides an
   existing P5137 (cross-pivot conflict).

## Run

Managed with [uv](https://docs.astral.sh/uv/) — `uv run` provisions the interpreter on first
use (the scripts are stdlib-only, no dependencies).

```bash
# 1. clusters (local; ~2 min over the 49 GB infer.sqlite3)
uv run build_clusters.py

# 2. Wikidata lexemes — download the weekly dump first:
#    https://dumps.wikimedia.org/wikidatawiki/entities/latest-lexemes.json.gz  (~562 MiB)
uv run load_lexemes.py --dump path/to/latest-lexemes.json.gz

# 3. candidates
uv run match.py
```

## Logic test (no dump needed)

```bash
uv run load_lexemes.py --dump test/fixture_lexemes.jsonl \
    --out data/wikidata_fixture.sqlite3
uv run match.py --wikidata data/wikidata_fixture.sqlite3
```

The fixture models the *Bank* case: `bench@en` anchored to Q123 propagates to `Bank@de` and
`bänk@sv`, while financial `bank` only attaches to the financial cluster — demonstrating
sense-specific candidates.

## Not yet built

Q-item label/description fetch (CC0) · LLM yes/no verifier · held-out precision harness
(stratified by P5137 provenance) · review-queue UI · `wbeditentity` writer with the mandatory
`based on heuristic (P887)` reference.
