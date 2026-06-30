# P5137 anchor-propagation (de/en/sv/fr/no pilot)

Generate reliable **P5137 ("item for this sense")** candidates for Wikidata lexemes by
propagating an *existing* anchor concept across WikDict sense-grounded translation clusters.
The LLM only does a constrained yes/no, verified against the anchor Q-item's **own CC0**
label/description plus the target lexeme's **own CC0 Wikidata sense gloss** (which pins *which*
sense is meant — e.g. *sheet* = paper vs. the nautical line). No DBnary gloss text (CC-BY-SA) is
ever read or written. See the full method and
license rationale in `notes/p5137_anchor_propagation.md` and the plan in
`~/.claude/plans/i-want-to-improve-twinkling-puppy.md`.

All data lives in `data/` (git-ignored). That includes **local copies** of the wikdict-gen
inputs — `infer.sqlite3` and `de/en/sv/fr/no.sqlite3` — kept here so other jobs rebuilding the
wikdict-gen dictionaries can't interfere mid-run. To refresh them from source:

```bash
src=$WIKDICT_GEN/dictionaries   # default: /home/karl/code/github/wikdict-gen/dictionaries
for f in infer processed/de processed/en processed/sv processed/fr processed/no; do
  cp "$src/$f.sqlite3" "data/$(basename $f).sqlite3.part" && \
  mv "data/$(basename $f).sqlite3.part" "data/$(basename $f).sqlite3"  # atomic
done
```

Norwegian: WikDict's `no` dictionary is sourced from the Bokmål Wiktionary, so the dump's
Norwegian Bokmål lexemes (language item Q25167, lemma tag `nb`) are mapped to `no` in
`load_lexemes.py` to join the clusters; Nynorsk (`nn`) has no matching WikDict dictionary
and is excluded.

## Pipeline

```
infer.sqlite3 ──build_clusters.py──▶ clusters.sqlite3 ─┐
                                                       ├─match.py──▶ candidates.sqlite3 ─┬─ precision.py / ablation.py (held-out eval)
latest-lexemes.json.gz ─load_lexemes.py──▶ wikidata.sqlite3 ─┘                          ├─ fetch_items.py ─▶ wd_items.sqlite3 (CC0 labels)
                                                                                        ├─ verify.py ─▶ verify.sqlite3 (LLM yes/no)
                                                                                        ├─ measure_lift.py (verifier-lift eval)
                                                                                        ├─ export_review.py ─▶ data/review.html
                                                                                        ├─ review_queue.py ─▶ notes/runs/decisions.csv (human approve/reject log)
                                                                                        ├─ gender.py ─▶ gender.sqlite3 (LLM semantic gender → P10339; phase 2, not in v1 write path)
                                                                                        └─ write.py ─▶ wbeditentity: P5137 only (gated on approval)
```

**Build the candidate stream:**
1. **`build_clusters.py`** — sense-grounded clusters from `dictionaries/infer.sqlite3`.
   Groups translation edges by the structural key `(lexentry, sense_num)` (never the gloss),
   which removes ~30% of naive lemma triples that would otherwise stitch different senses
   together (e.g. de *Bank* = bench vs. financial).
2. **`load_lexemes.py`** — streams the Wikidata lexeme dump, keeps de/en/sv/fr/no lexemes, each
   sense's P5137 target, and each sense's **own CC0 Wikidata gloss** (`sense_gloss` table;
   DBnary's CC-BY-SA glosses are never touched).
3. **`match.py`** — matches cluster members to noun lexemes (Q1084), finds the single dominant
   anchor Q per cluster, and emits one candidate per `(target lexeme, anchor Q)` with a
   `support_clusters` confidence count. Abstains on conflicting anchors and never overrides an
   existing P5137 (cross-pivot conflict).

**Evaluate, verify, review, write:**
4. **`precision.py`** / **`ablation.py`** — held-out leave-one-out precision and structural-gate
   ablation (the A/B/D gates; see notes for the de-bias).
5. **`fetch_items.py`** — polite Wikidata Action API client; caches anchor Q **CC0**
   labels/descriptions + P31/P279 into `wd_items.sqlite3`.
6. **`verify.py`** — LLM yes/no verifier over `claude -p` (CC0 anchor label/description + the
   target lexeme's own CC0 gloss; tolerant criterion — broader/narrower count as yes, only a
   genuinely different concept is 'no'). `--gate ABD` runs the High tier. Verdicts are cached in
   `data/verify.sqlite3` (`--force` re-verifies after a prompt change); `--csv` exports them.
7. **`measure_lift.py`** — measures the verifier's precision lift on held-out ground truth.
8. **`export_review.py`** / **`review_queue.py`** — browsable HTML + a stdlib approve/reject/defer UI
   (the authorization gate). Decisions are an append-only, git-committed log at
   `notes/runs/decisions.csv` (the single source of truth — there is no review DB; "pending" is
   computed as the verifier-'yes' pool minus what the log covers).
9. **`gender.py`** *(phase 2 — not wired into the v1 write path)* — LLM semantic-gender tagger
   (CC0: bare lemma only) over approved candidates; supplies the **P10339** value for the
   two-part gendered-lexeme convention (P5137 → the gender-neutral concept, the word's sex
   recorded separately). A gendered form denotes the *same* concept as its neutral counterpart
   (an EQUAL link, valid and constraint-clean without the qualifier), so v1 writes the P5137 link
   alone and leaves P10339 as an additive phase-2 enrichment — not a v1 precondition. Cached in
   `gender.sqlite3`.
10. **`write.py`** — builds the `wbeditentity` payload (**P5137** + mandatory P887/P813 reference),
   reads only approved rows from that log, appends a `written` event with the revid, and defaults
   to `--dry-run`.

**QA tooling:** **`audit_decisions.py`** surfaces human/verifier disagreements and runs an optional
CC0 gloss-contradiction audit (`--gloss`); **`validate_prompt.py`** checks a verifier-prompt change
against the human-gold decisions (keep% / catch%) with no extra LLM cost.

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

# 4. evaluate + fetch CC0 anchor items (needs www.wikidata.org)
uv run precision.py
uv run fetch_items.py

# 5. verify the High tier (clean ∩ ≥2 anchors ∩ monosemous ∩ concrete) and measure lift
uv run verify.py --gate ABD --all --model sonnet
uv run measure_lift.py --gate ABD --sample 350 --model sonnet

# 6. review, then write  (decisions logged to notes/runs/decisions.csv — source of truth)
uv run export_review.py                 # data/review.html
uv run review_queue.py serve            # http://localhost:8731  approve/reject/defer
uv run review_queue.py status           # pending (computed) / approved / deferred / rejected / written
uv run write.py --dry-run               # print payloads for approved rows (submits nothing)
# gender.py (P10339, two-part gendered-lexeme convention) is phase 2 — not part of the v1 write path

# 7. go live (assisted edits on your account; no autoconfirmed needed for statement-adds)
#    create a Special:BotPasswords credential, then:
set -x WIKI_USER 'You@botname'; set -x WIKI_PASS '...'
uv run write.py --write --limit 10      # submit approved rows via the Action API
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

## Status (2026-06, de/en/sv)

**French and Norwegian (Bokmål) were just added** to the pilot (`load_lexemes.py` +
`build_clusters.py`); the headline numbers below are still the measured de/en/sv results.
fr/no candidates are generated by the local stages (clusters → match) but have not yet been
verified/reviewed, and the eval tooling (`precision.py`/`ablation.py`/`measure_lift.py`,
`problematic_cases.csv`) remains scoped to the de/en/sv gold.

Pipeline validated end-to-end on the real dump. Headline results (full detail in
`notes/p5137_anchor_propagation.md`):

- **34,821** clean candidates; **High tier** (clean ∩ ≥2 anchors ∩ concrete) = **7,400**
  (~6,700 grounding a currently-senseless lexeme).
- **Structural precision** caps ~81% honestly (held-out, de-biased); the LLM verifier is required.
- **Verifier lift** on the High tier (held-out, strict-Q truth): pre-gloss lemma-only ~86% →
  **95.9%** (pooled n=470); the current **gloss·tolerant** verifier ~86% → **93.8% at 100% recall**
  (n=350) — the small dip is tolerance accepting broader/narrower near-synonyms that exact-Q truth
  scores as misses. On the **human-reviewed gold set** it keeps **98.3%** and catches every genuine
  homograph (*sheet*=paper, *hov*=court) the lemma-only verifier missed.
- Verifying all **669** High-tier *candidates* with the gloss·tolerant verifier: **470 yes / 198 no
  / 1 uncertain** (~70% / 30%); the rejections include true homograph catches the lemma-only verifier
  waved through (e.g. *sheet*→nautical line, *hov*→hoof).

## What's left before the BRFA

- **P887 heuristic item** "inferred from translation" — created as **Q140308675** and pinned in
  `write.py` (done). ~11 pre-BRFA assisted edits already written (see `notes/runs/decisions.csv`).
- **Senseless gloss policy** — the pilot writes monosemous targets only; sense-creation
  (`--include-senseless`) needs a CC0 gloss decision first.
- **Talk-page post** (method + license rationale) → **BRFA** (a few more pre-BRFA edits, then a 50–250 trial).
