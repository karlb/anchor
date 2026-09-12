Read the whole pipeline. Here's the picture, then the design.

*(Fact-checked 2026-08-15 against the code and live DBs; corrections applied inline. Re-checked 2026-08-16 after the polysemous lane landed and the multi-candidate sheet was retired — changed numbers updated, new deltas marked inline in italics.)*

## What's actually there

**7,039 lines across 26 scripts, 52 GB in `data/`, producing 450 written statements** — all approved by one person clicking buttons.

The proportions tell the story:

|                                                    | LOC    | share |
| -------------------------------------------------- | ------ | ----- |
| Measurement harnesses (10 scripts)                 | ~2,180 | 31%   |
| `review_queue.py` alone                            | 1,144  | 16%   |
| Core pipeline (clusters→match→verify→assign→write) | ~1,960 | 28%   |
| Crowd game + gender phase-2                        | 883    | 13%   |

*(2026-08-16: harnesses fell from 37% to 31% — `build_multi_gold.py` was deleted, the first harness ever retired. The core row grew by `assign.py` and the polysemous write path. Same 26 scripts: one out, one in.)*

And `config.REVIEW_LANGS` has six languages; `decisions.csv` has rows for four. `no` and `ru` have never produced a single reviewed decision, but they're in every gate, every prompt map, every fetch.

## What I'd build from scratch

**1. One database, not seven.** The pipeline is one row per `(target_lid, anchor_qid)` that accumulates columns: evidence → verdict → decision → revid. Instead there are six SQLite files ATTACHed under aliases, which is why `gates.py` exists at all — nine SQL-fragment composers (plus the `high_tier_where` combiner) taking `cand_alias`/`it_alias`/`wd_alias` parameters, because five consumers each need the same WHERE clause. With one table the High tier is `WHERE homograph_n_lid=1 AND support>=2 AND anchor_in_scope` plus one per-action admissibility term — a free sense for MATCH, zero senses for CREATE_FIRST. (Not `n_free_senses>0` alone: that drops the clean gate — 220 rows, 9.9%, wrongly in — and structurally excludes the senseless lane, ~88% of the pool; the exact regression `gates._check` exists to catch.) `gates.py` disappears.

**2. Filter before expanding, and `build_clusters.py` disappears too.** It extracts every translation edge in `infer.sqlite3` into a 1.3 GB DB — 697,744 clusters, 5.4M members. Only 151,863 clusters ever get an anchor and only 1.2M members ever match a lexeme. **78% of the build is discarded downstream.** A cluster with no P5137-bearing member can never produce anything, and you know that set after `load_lexemes.py` (160k anchored lexemes). Flip the order: load lexemes → anchored lemma set → pull only clusters touching one. That's a WHERE clause inside `match.py`, and `clusters.sqlite3` goes from 1.3 GB to tens of MB.

**3. Make the LLM return the fields you branch on.** This is the deepest one. The verifier returns `{"verdict":"yes","reason":"NARROWER: female form of dancer"}` and then six heuristics substring-scrape that ≤10-word prose field and the CC0 descriptions: `_CONVENTION`, `_DEMONYM_GLOSS`, `_ETHNIC_ANCHOR`, `is_ethnic_demonym`, `scope_kind`, `rel_class`. A model paraphrase silently flips keep→defer (verified live: "female form of dancer" → keep, "woman-specific form of dancer" → defer). You already caught this (`notes/simplify/convention-tag-in-verifier-reply.md`) and deferred the fix to the next prompt revision.

From scratch the reply carries explicit fields — `{relation, is_gendered_form, is_demonym, anchor_is_ethnic_group}` — and the whole scope-triage section collapses to a dict lookup.

**4. Version the cache instead of invalidating it.** *(2026-08-16: the cache half is DONE — the
`verdict` PK is now `(pair, prompt_ver)`, all rows kept, every consumer filters on a version, and
`validate_prompt --baseline-ver` grades two prompts out of the one file. Deliberately split from
#3 rather than folded as this section advises: folding assumed a re-verify was being paid anyway,
which stops being true once the additive cache lands FIRST — then #4 costs nothing and #3's
re-verify becomes an A/B instead of a wipe. The structured-reply half of #3 landed the same day, and on 2026-09-03 the reply
grew a `sense` field — the verifier now picks the sense from the lexeme's inventory. `stale-prompt-verdicts-in-queue` is closed by the same change.)* `PROMPT_VER = sha1(SYSTEM)` wipes every cached verdict on any wording change (mechanically: orphans them until re-verify, then `INSERT OR REPLACE` destroys the old row — same paid full re-verify either way, ~3,220 pairs today — up from 2,670 once the polysemous lane was verified, so the cost of this deferral grows every time a lane opens). That line is the stated blocker on one of the three `notes/simplify/` findings (convention-tag; the other two wait on an export dependency and a `match.py` rebuild) and two `notes/review/` ones (stale-prompt-verdicts, gloss-ladder-fork). Key the cache on `(pair, prompt_ver)` and *keep both rows* — then a prompt change is an A/B by construction rather than a paid re-verify plus a gold revalidation, and the "deferred until the next prompt revision that happens anyway" pattern stops existing. `prompt_ver` is already a per-row column; the catch is the PK is `(lid, qid)` WITHOUT ROWID, so this is a small table rebuild plus prompt_ver-aware consumers (`review_queue.pool`, `load_verify_cache`), not a policy toggle.

**5. One eval, one gold.** Ten harnesses exist because each new question got its own script that froze its own arm — and they now constrain live code backwards: the fossil de/en/sv `item` columns can't be dropped because a frozen base arm still grades on them. One `eval.py`, one gold CSV, one `score(config) → keep%/catch%`. The 119 curated cases are the real asset; the scaffolding around them isn't. *(2026-08-16: was eleven harnesses and two gold sets. `build_multi_gold.py` and `multi_candidate_gold.csv` are deleted — the sheet asked one-best-of-N of a population reaching below gates A and D, and competition turns out to be 2% of the live review queue and one human tie-break in 694 decided lexemes. `problematic_cases.csv` is now the only gold set, so this section is one step done rather than a proposal. But the count fell by REMOVAL, not consolidation: `assign_eval.py` was added the day before with its own frozen arms and its own DB — the same one-question-one-script pattern this section is about, still running.)*

**6. Don't hand-roll the review UI.** 1,005 lines of stdlib HTTP server, HTML rendering, five advisory heuristics, and ~250 lines of auto-defer enforcement — whose job is to *hide rows a human would reject anyway*. Decisions are already an append-only CSV. A generated static page plus editing the CSV gets most of it. The auto-defer gate in particular is a lot of machinery (`rel_of`/`rel_tokens`/`live_rel`/`auto_defer_classes`/`occupancy_context`/`demonym_context`) buying a shorter queue for one reviewer.

**7. Close one lane before opening the next.** Currently open simultaneously: 6 languages, monosemous + senseless + polysemous lanes, a crowd game, a P10339 gender phase. The BRFA — the thing that unlocks writing at volume — is still not filed. *(2026-08-16: the polysemous lane is no longer "measured-but-unbuilt" — it has gates, verification, per-sense assignment, a review UI and a write path behind `--include-poly`, 549 pairs over 454 lexemes. That is a third lane opened while the first is still unclosed and the BRFA still unfiled, which is precisely what this section argues against. Recorded rather than argued away.)* From scratch: Swedish only (sv is 339 of 757 decided pairs, 495 counting en too; that's where evidence is thickest), monosemous only, 250 edits, file the BRFA, *then* widen. Every language added pre-BRFA multiplies the cost of any policy change the BRFA review forces on you.

**Estimate: ~1,200 lines, one SQLite file, ~2 GB.**

## What I'd keep unchanged

The hard-won parts are real and a rewrite should port them verbatim:

- **Grouping on `(lexentry, sense_num)`, never the gloss** — the actual idea, removes ~30% of naive triples
- **The POS-agreement filter** — `en cancel → Q192298 "ring finger"` with support=13 from one Spanish noun is a genuine bug class, and gate A *amplifies* it
- **The never-override guard + live free-sense recheck at write time**
- **Append-only `decisions.csv` as sole source of truth** — you already identified it as the one irreplaceable artifact
- **The tolerant verifier criterion** (surface for human review, never auto-drop)
- **The CC0/CC-BY-SA discipline** — a genuine legal constraint, not overhead

## The honest caveat

I don't think you should rewrite. Three of these are reachable incrementally, and #3 and #4 unblock each other: fold the structured reply and the additive cache into one prompt revision. You're already paying the re-verify cost for that revision, it's the documented trigger in the convention-tag simplify note and the two review findings blocked the same way, and it permanently removes that class of deferral for good. That's the one move I'd make.
