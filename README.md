# P5137 anchor-propagation (de/en/sv/fr/no/ru pilot)

Generate reliable **P5137 ("item for this sense")** candidates for Wikidata lexemes by
propagating an *existing* anchor concept across WikDict sense-grounded translation clusters.
The LLM only does a constrained yes/no, verified against the anchor Q-item's **own CC0**
label/description plus the target lexeme's **own CC0 Wikidata sense gloss** (which pins *which*
sense is meant — e.g. *sheet* = paper vs. the nautical line). No DBnary gloss text (CC-BY-SA) is
ever read or written. See the original method and license rationale in
`notes/p5137_anchor_propagation.md`, the generalisation to per-sense assignment in
`notes/sense_assignment_findings.md`, and a critical read of the whole pipeline in
`notes/redo.md`.

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
latest-lexemes.json.gz ─load_lexemes.py──▶ wikidata.sqlite3 ─┘                          ├─ fetch_items.py / fetch_anchor_langs.py ─▶ wd_items.sqlite3 (CC0 labels)
                                                                                        ├─ verify.py ─▶ verify.sqlite3 (LLM yes/no, per lexeme)
                                                                                        ├─ assign.py ─▶ assign.sqlite3 (LLM: WHICH sense — poly lane)
                                                                                        ├─ measure_lift.py / calibrate.py (eval + gate readiness)
                                                                                        ├─ export_review.py ─▶ data/review.html
                                                                                        ├─ review_queue.py ─▶ notes/runs/decisions.csv (human approve/reject log)
                                                                                        ├─ game_export.py / import_crowd.py (crowd votes ⇄ the same log)
                                                                                        ├─ gender.py ─▶ gender.sqlite3 (LLM semantic gender → P10339; phase 2, not in v1 write path)
                                                                                        └─ write.py ─▶ wbeditentity: P5137 only (gated on approval)
```

`gates.py` is the shared definition of the tiers every one of those consumers filters on — one
`high_tier_where()` per lane, so the streams can't drift apart.

**Build the candidate stream:**
1. **`build_clusters.py`** — sense-grounded clusters from `dictionaries/infer.sqlite3`, built
   from **all ~26 WikDict languages** (not just the pilot): richer evidence — more support per
   candidate, and non-pilot pivots (e.g. es *banco*) bridge pilot members no pilot pivot links.
   Groups translation edges by the structural key `(lexentry, sense_num)` (never the gloss),
   which removes ~30% of naive lemma triples that would otherwise stitch different senses
   together (e.g. de *Bank* = bench vs. financial).
2. **`load_lexemes.py`** — streams the Wikidata lexeme dump. Keeps **review-language**
   (`config.REVIEW_LANGS` = de/en/sv/fr/no/ru) lexemes in full (they can be write targets), and
   **every other WikDict language anchor-only** (P5137-bearing lexemes, so any language can
   anchor a cluster). Extracts each sense's P5137 target and its **own CC0 Wikidata gloss**
   (`sense_gloss` table; DBnary's CC-BY-SA glosses are never touched). The 26-language
   `LANG_QID` map is derived from the dump (each language's actual lexeme-tagging QID).
3. **`match.py`** — matches cluster members to noun lexemes (Q1084), finds the single dominant
   anchor Q per cluster, and emits one candidate per `(target lexeme, anchor Q)` with a
   `support_clusters` confidence count. **Targets are restricted to `config.REVIEW_LANGS`** —
   the one place the pilot limit is applied to the output (anchors may be any loaded language).
   Abstains on conflicting anchors and never overrides an existing P5137 (cross-pivot conflict).

**Evaluate, verify, review, write:**
4. **`precision.py`** / **`ablation.py`** — held-out leave-one-out precision and structural-gate
   ablation (the A/B/D gates; see notes for the de-bias).
5. **`fetch_items.py`** / **`fetch_anchor_langs.py`** — polite Wikidata Action API clients; cache
   anchor Q **CC0** labels/descriptions + P31/P279 into `wd_items.sqlite3`. The all-language
   `item_lang` cache is the verifier's only concept source, so a stale fetch fails loud.
6. **`verify.py`** — LLM yes/no verifier over `claude -p` (CC0 anchor label/description + the
   target lexeme's own CC0 gloss; tolerant criterion — broader/narrower count as yes, only a
   genuinely different concept is 'no'). Verdicts are cached in `data/verify.sqlite3` (`--force`
   re-verifies after a prompt change); `--csv` exports them.
7. **`assign.py`** — the per-*sense* question, for targets that have several senses: which of
   this lexeme's senses does the anchor belong to, or none? Runs downstream of `verify.py`;
   an abstention is strong evidence the sense is missing from Wikidata altogether.
8. **`measure_lift.py`** / **`calibrate.py`** — verifier precision lift on held-out truth, and
   per-relation human-verdict precision against the pinned auto-approve/auto-defer gates.
9. **`export_review.py`** / **`review_queue.py`** — browsable HTML + a stdlib approve/reject/defer UI
   (the authorization gate). Decisions are an append-only, git-committed log at
   `notes/runs/decisions.csv` (the single source of truth — there is no review DB; "pending" is
   computed as the verifier-'yes' pool minus what the log covers).
10. **`game_export.py`** / **`import_crowd.py`** — the same pool as pre-rendered tiles for the
   Wikidata Distributed Game, and the return path: crowd votes are scored per voter, promoted
   into the same decision log, and never write on their own (`notes/crowd_review_plan.md`).
11. **`gender.py`** *(phase 2 — not wired into the v1 write path)* — LLM semantic-gender tagger
   (CC0: bare lemma only) over approved candidates; supplies the **P10339** value for the
   two-part gendered-lexeme convention (P5137 → the gender-neutral concept, the word's sex
   recorded separately). A gendered form denotes the *same* concept as its neutral counterpart
   (an EQUAL link, valid and constraint-clean without the qualifier), so v1 writes the P5137 link
   alone and leaves P10339 as an additive phase-2 enrichment — not a v1 precondition. Cached in
   `gender.sqlite3`.
12. **`write.py`** — builds the `wbeditentity` payload (**P5137** + mandatory P887/P813 reference),
   reads only approved rows from that log, appends a `written` event with the revid, and defaults
   to `--dry-run`.

### Three lanes

Every stage above is parameterised by how many senses the *target* lexeme has, because that
decides what the edit does. `gates.py` asserts that the lanes partition the pool:

| lane | target | the edit | flag |
|---|---|---|---|
| **mono** | exactly one sense | add P5137 to that sense | *(default)* |
| **poly** | several senses | add P5137 to the *assigned* sense (`assign.py`) | `--include-poly` |
| **senseless** | no senses at all | create a first sense (CC0 gloss) + P5137 | `--include-senseless` |
| mono or poly, no recorded sense fits | the verifier saw the inventory and answered `none` (`MISSING` class) | reviewer clicks **+ create sense** → status `create` → a new sense (CC0 gloss) + P5137 | `--include-senseless` |

The lanes are deliberately kept separate: a lane's measured precision must not be restated over
a population nobody has reviewed. `review_queue.py`, `export_review.py`, `calibrate.py`,
`game_export.py` and `write.py` all take the same lane selector.

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
uv run fetch_anchor_langs.py            # the all-language concept cache verify.py reads

# 5. verify the High tier (clean ∩ ≥2 anchors ∩ concrete ∩ an admissible sense to write into)
uv run gates.py                         # assert the lanes are non-empty and partition the pool
uv run verify.py --gate ABD --all --model sonnet          # mono lane
uv run verify.py --gate AD --senseless-only --all         # senseless lane
uv run verify.py --gate AD --poly-only --all              # poly lane, then:
uv run assign.py --all                  # ...which sense of the target does the anchor mean?

# 6. review, then write  (decisions logged to notes/runs/decisions.csv — source of truth)
uv run export_review.py                 # data/review.html
uv run review_queue.py serve            # http://localhost:8731  approve/reject/defer
uv run review_queue.py status           # pending (computed) / approved / deferred / rejected / written
#   ...both take --senseless / --poly to switch lanes
uv run calibrate.py                     # per-relation precision vs the pinned automation gates
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

## Status

Deliberately numbers-free — every count below moves, and the live ones come from the tools:

```bash
uv run review_queue.py status [--senseless|--poly]   # pending / approved / written per lane
uv run calibrate.py                                  # per-relation precision vs the gates
uv run gates.py                                      # lane sizes + the partition assertion
```

Where things stand:

- **The pipeline is validated end-to-end** on the real dump, and the **monosemous lane writes
  live** — every edit so far is an assisted edit, individually approved by a human against the
  anchor's CC0 description. Measured precision on that write stream is reported by
  `review_queue.py status`; the method-level measurements are in
  `notes/p5137_anchor_propagation.md`.
- **Structure alone caps around 81%** (held-out, de-biased), so the LLM verifier is load-bearing,
  and the human gate behind it is what the precision claim actually rests on.
- **The senseless and polysemous lanes are built but have never written.** Both have approved or
  assigned rows waiting; sense creation is the larger edit class and the one that needs the
  community conversation.
- **The crowd game is live** (`notes/crowd_review_plan.md`); crowd consensus is promoted into the
  same decision log under a `crowd:*` reviewer and still writes only through `write.py`.
- **Automation is gated, not on.** `calibrate.py` measures per-relation human-verdict precision
  against pinned thresholds; auto-*defer* is enforced for classes humans almost never write,
  auto-*approve* is not enabled for any class.

## What's next

The gating question is **whether sense creation is accurate enough to propose at all**, so the
work is measurement before conversation. Reasoning in `notes/sense_assignment_findings.md` §4,
`notes/sense_creation_plan.md` (measurement staging) and `notes/redo.md`.

1. ~~**Measure the senseless lane's fresh-stream precision.**~~ **DONE** —
   `notes/senseless_lane_findings.md`. A 240-pair random sample of the lane, adjudicated blind.
   The lane **misses the gate** (its write-lane precision is clearly below the monosemous
   stream's, non-overlapping CIs), so do not scale and do not write from it yet. What the errors
   turned out to be is the useful part: none of the write-lane losses is a wrong sense, the
   feared gloss-less homograph flood did not appear, higher support does *not* help, and two
   thirds of the losses are deterministic — a triage inversion and bad gloss selection, not a
   weak model.
2. ~~**The prompt + cache revision**~~ **cache half DONE** (`(pair, prompt_ver)`, both rows kept,
   read-side filters landed, `validate_prompt --baseline-ver`). The **structured-reply half**
   (`notes/redo.md` #3 — the verifier returns the fields the code branches on instead of prose the
   code substring-scrapes) is next, and (1) turned it from a tidiness argument into a correctness
   one: the scraped `_CONVENTION` list makes the senseless lane *keep* exactly the demonym pairs
   the monosemous lane defers.
3. ~~**Refine the lane against what (1) found, then re-measure.**~~ **ONE ROUND DONE** — the
   structured reply landed and the demonym/ethnic-group split now works without a gloss (verified
   8/8 on the decisive pairs), and Russian senses can be glossed in Russian. Write-lane precision
   moved 92.9% → 94.0%, still short of the monosemous stream. The re-measure's real finding is
   methodological: at one sample per pair, the model's relation call moves ~6% of pairs between
   runs, which is the same size as the fix — so **the next iteration needs a multi-sample vote**
   before its headline means anything. Levers still unspent: richer anchor-side evidence, and an
   abstention-style question adapted to senseless targets ("does the anchor correspond to a
   meaning this lemma actually has?"), which discriminated sharply in the polysemous lane. The
   tighter-slice fallback is **refuted** — precision is flat in support. Grading is free: the
   sample's adjudications are a gold set and the cache is additive —
   `validate_prompt.py --decisions notes/runs/senseless_sample.csv --baseline-ver <old>`.
   **2026-09-03: the verifier now sees each lexeme's sense inventory** and answers *which* sense
   (or `none` → a `create` action on mono and poly rows). Prompt `d8769650`, pool re-verified,
   kept on every free grader — `notes/verifier_sense_inventory.md`. Senseless targets still get
   the bare lemma; the abstention-style question above is the unspent lever there.
4. **Disclose sense creation on-wiki, then write.** In that order. Sense creation is already
   announced in the talk-page thread as in-progress with a promise to report back, and the
   public answer given there about homographs does not cover a lane with no gloss — so the post
   comes before the edits, carrying the measured precision and an honest account of what
   replaces the gloss. Approved rows keep: `decisions.csv` is append-only, so a refined approach
   that disagrees with an earlier approval appends a `recheck` row rather than losing it.
5. **Review the mono `create` rows** (`review_queue.py serve`, `/?act=create`) — the first human
   labels on the create action, which the senseless lane and any bot request rest on.
   **Poly is parked** (2026-09-27): ~350 pairs, no proposal depends on it. Kept built and in the
   design — the unit stays `(lexeme, sense | new, item)` for every lane, so poly needs no special
   case when it returns.

**The method has been disclosed on-wiki and the licensing question is answered.**
[[Wikidata talk:Lexicographical data]], posted 2026-06-29 (`notes/talkpage_post.wiki`): two
editors reviewed it, neither objected to the licence boundary or the P887 reference scheme, and
the thread since covered French/English examples, homographs and Russian. Two obligations came
out of it — the public answer about homographs relies on safeguards the newer lanes don't have,
and sense creation has been *announced* to a waiting reviewer but not yet described. Details in
that file's header; don't re-argue the licence case, it was already accepted.

**The BRFA itself is deliberately held** until there is a proposal worth making. Monosemous and
polysemous writes are assisted, human-confirmed edits that need no bot flag, and the monosemous
lane is too small to be worth a task description; the volume — and the licensing conversation —
are both in sense creation, which is not yet mature enough to propose. When it is: scope the
request at the *action* ("create a sense for a translation-attested meaning Wikidata does not
record"), not at "senseless lexemes". The long method + licence write-up for that request is
parked in `notes/talkpage_draft.wiki`.

Already settled, so they don't reappear as blockers: the P887 heuristic item *inferred from
translation* (**Q140308675**) is created and pinned in `write.py`, and the senseless gloss policy
is decided (anchor description first, label fallback, skip if neither — `notes/sense_creation_plan.md`).
