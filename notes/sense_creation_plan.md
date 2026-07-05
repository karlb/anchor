# Senseless lexemes: sense-creation plan (phase 2)

*Extend the P5137 pipeline to targets with **zero** senses: create the sense (CC0 gloss)
and attach the P5137 in one edit. Plan drafted 2026-07-02; companion to
`p5137_anchor_propagation.md`. **Amended 2026-07-05** by `scope_roadmap.md`: review and
write must be lexeme-centric, not pair-wise — 135 senseless lexemes have 2–4 candidate
items, and independent per-pair sense creation risks duplicate senses the live guard
can't catch. Group all candidate items per lexeme in the review lane; one edit per
lexeme in `write.py`.*

## Why this is the main prize

The pilot writes only monosemous targets (add P5137 to the one existing sense). But the
candidate stream is dominated by **senseless** lexemes — the ones whose first meaning we
would create:

| Tier | Candidates | of which senseless |
|---|---|---|
| All clean | 34,821 | 31,260 (90%) |
| High tier minus gate B (clean ∩ A ∩ D ∩ scope) | 7,400 | 6,679 |

Gate B (monosemous) is meaningless here — the senseless tier is **clean ∩ A (≥2 anchors)
∩ D (concrete, in-scope anchor) ∩ n_senses = 0**. Grounding a senseless lexeme is the
method's founding motivation ("fixes the senseless-lexeme problem and creates the
translation link") and the highest-value edit: it gives the lexeme its first meaning.

## What already exists vs. what blocks the stream

Exists:
- `verify.py --senseless-only` (correctly refuses gate B; use `--gate AD`).
- `write.py --include-senseless` builds the sense-creation payload: new sense with
  gloss = anchor's CC0 label (own language, en fallback), P5137 claim + P887/P813 ref
  inside it, **skip when no CC0 label exists** (the `or lemma` degenerate fallback is
  already removed).
- Live-write guard: skip if any live sense already carries this P5137 (crash-replay safe).
- Early signal: the original 48-candidate senseless sample verified 36 yes / 9 no / 3
  uncertain, with correct rejections (`Schützin`→"alcoholic mixed drink").

Blocks:
1. ~~`gates.high_tier_where()` hard-codes `target_n_senses = 1`~~ **— DONE 2026-07-05.**
   Parameterized `high_tier_where(c, it, n_senses=1)`; the senseless tier passes `n_senses=0`.
   One definition, consumed by review_queue/export_review/write, so the tiers can't drift.
2. ~~Gloss policy implemented~~ **— DONE 2026-07-05.** `review_queue.anchor_gloss(it, qid, lang)`
   is the single source: description-primary, label fallback, skip if neither, plus an en gloss
   for legibility. The review UI (`--senseless` lane) previews the *exact* text write.py will
   write — both call this one function.
3. ~~Gloss language-key bug~~ **— DONE 2026-07-05.** `anchor_gloss` returns `{gloss_lang: text}`
   keyed by the language the text is actually in, and `build_edit()` writes each under its own
   key — an en fallback is now keyed `en`, never as the target language.
4. No fresh-stream precision number (and none can come from held-out data — senseless
   targets have no P5137 to hide, *by construction*). **Still open — this is the next step**
   (measurement staging below): the lane and write path are ready; what's unmeasured is the
   stream's precision. Verify a sample → human-review → gate before scaling.

## Risk model — how this stream differs from monosemous

- **The gloss·tolerant safety net is absent.** The current verifier's homograph catches
  (sheet/hov) come from reading the target's own gloss; senseless targets have none, so
  the verifier runs in exactly the lemma-only mode that missed all 4 genuine homographs
  on gold. Compensations: the anchor-side signal is untouched (label+description), the
  demonym/ethnic scope-hint nudge is deterministic (no gloss needed), and `item_lang`
  multi-language labels give the reviewer (and optionally the +multi verifier) a
  cross-language consistency check.
- **Wrong-first-sense hazard.** "Senseless" ≠ "monosemous in reality". If the cluster
  sense is a polysemous word's *minor* meaning, we create S1 = the minor sense; later
  editors may pile dominant-meaning data onto it. Mitigation: the gloss makes the sense
  self-describing, and every pre-BRFA edit is human-approved *including the gloss text*.
- **Precision is an assumption until measured.** The monosemous 95.7% (180/188) is
  inherited, not demonstrated; the 48-sample yes-rate (75%) sits between the held-out
  85% and the fresh-monosemous 51%, so the stream's difficulty is genuinely unknown.
  The staging below makes measuring it the first deliverable.

## Relationship to MachtSinn (why we keep our own pipeline)

MachtSinn (Nudin/makesense, running on Toolforge; `gates.py` already borrows its
false-positive lists) does the *same edit* we're about to: on human confirm it creates a
sense whose gloss is the **item description** and attaches P5137 to the item — no BRFA,
because it's human-confirm. So the tempting question is "just feed MachtSinn instead of
building our own write path." Two facts decide against collapsing to that:

- **Its candidate generation is a strict subset of ours.** MachtSinn matches by
  `lemma == item label`, single language, via SPARQL. Anchor's candidates come from the
  cross-language WikDict cluster + LLM verification + scope-hint/demonym nudges — exactly
  the matches where the lemma is *not* any item's label (`Kugel`→sphere reached via the
  cluster, not a "Kugel"-labeled item). Restricting to what MachtSinn can find throws
  away the high-value slice and keeps only the cognate cases it already surfaces itself.
- **It has no external-candidate ingestion.** It only reviews its own SPARQL output, so
  "create input for MachtSinn" means a fork/PR to add a curated-candidate source — more
  work than our review lane (`review_queue.py`/`export_review.py`/`decisions.csv`), which
  already renders the LLM verdict, scope hints, and gloss its UI wouldn't show.

So the candidate stream stays ours; we **borrow MachtSinn's edit shape and trust
precedent, not its infrastructure** — see the gloss policy (below) and the BRFA framing
(staging step 5). The one thing MachtSinn genuinely bounds: it doesn't scale (every match
is a manual click). Our bet is that LLM verification earns the right to bot-scale the
6,679; that bet is what the sample-precision gate (steps 1–3) tests, and if it fails the
honest fallback is running these as MachtSinn-style human confirms, not a home-grown bot.

## The gloss policy (decided: align with MachtSinn)

Constraints: CC0-only (Wikidata label/description of the anchor Q — DBnary text is never
read), never degenerate (no gloss == headword), keyed by the language of the text.

**Decision — description-primary, matching MachtSinn's community-accepted shape:** write
the anchor's **description** as the gloss; fall back to the **label** only when no
description exists in that language; **skip** the candidate if neither is usable. This
adopts the exact gloss MachtSinn already ships (item description), which resolves the
design question toward an established precedent and removes the reviewer-pushback risk of
a novel gloss shape. Options considered and rejected: **(a) label-only** fails on cognates
(`Kugel` glossed "Kugel"); **(c) label-else-description** writes a bare one-word synonym
where MachtSinn writes a definition-shaped gloss — worse, and diverges from precedent for
no gain. Description's one weakness (sometimes encyclopedic-disambiguator prose, not a
lexical gloss) is mitigated because it's what MachtSinn already ships *and* every gloss is
human-approved before scale.

Optionally also write the **en** label/description as an additional `en`-keyed gloss
(senses take multiple gloss languages) so every created sense is legible to
non-target-language patrollers. Fold the chosen policy into the talk-page post, citing
MachtSinn as precedent — bot-created glosses are the part reviewers will push on.

## Code changes (small, in order) — **all DONE 2026-07-05**

1. ~~**gates.py** — parameterize `high_tier_where(c, it, n_senses=1)`.~~ Done.
2. **verify.py** — no logic change; run `--gate AD --senseless-only` on the sample
   (below). Decide separately whether this stream justifies +multi/GUIDED (see open
   decisions). *(Nothing to change; it already refuses gate B for senseless.)*
3. ~~**review_queue.py / export_review.py** — a `--senseless` lane.~~ Done.
   `review_queue.py {serve,status} --senseless` and `export_review.py --senseless` use the
   parameterized gate; each row shows **"will CREATE sense — gloss: '…'"** (exact policy text)
   and, for lexemes with 2–4 candidate items, a ⚠ **multi-candidate** flag with same-lexeme
   rows sorted adjacent (lexeme-centric review — approve/prune the inventory as a set).
   Existing hints apply (the polysemy-collapse hint is repurposed to the wrong-first-sense
   warning; the demonym→ethnic nudge is inactive without a target gloss — a known gap). Live
   pool today: **258** verified-`yes` senseless candidates pending; **8,888** in the senseless
   tier overall (grew from 6,679 after the all-language rebuild).
4. ~~**write.py** — gloss keying fix + description-primary policy + guard tightening.~~ Done.
   Plus **lexeme-centric writes**: all approved items for one lexeme are grouped into ONE
   `wbeditentity` creating one sense each (`--limit` is lexeme-aware so an inventory is never
   split — a partial write would be permanently blocked by the tightened guard). The guard now
   skips any create edit whose live lexeme has **any** sense ("no longer senseless"), subsuming
   the old per-P5137 check.

## Measurement & staging (cost-gated)

1. **Verify a sample first, not the pool.** ~250 of the 6,679 (random), current
   tolerant prompt, sonnet. Record yes-rate + actual token cost; extrapolate the full
   ~6.7k run and get sign-off before running it (>100k-token job).
2. **Human-review the sample's "yes" rows** in the senseless lane (~150–190 expected).
   This yields the stream's first real precision number with a Wilson CI — the same
   measurement design that produced 180/188 on the monosemous stream.
3. **Gate to proceed:** reviewed precision comparable to the monosemous stream
   (CI overlapping ~95%). If it lands clearly lower (plausible — no gloss net), do NOT
   scale; options then are +multi/GUIDED re-verify of the sample (measured on gold to
   help exactly when target glosses are absent) or restricting to a tighter slice
   (e.g. support ≥3) before re-measuring.
4. **~10 pre-BRFA live edits**, hand-approved, in their **own EditGroups batch** (easy
   one-click revert of the sense-creation class separately from statement-adds).
   Inspect on-wiki rendering of the created senses (gloss display, Ordia).
5. **Disclose, then scale.** Talk-page post update + BRFA must state explicitly that
   this phase *creates senses* (a bigger edit class than adding a statement), with the
   gloss policy and the measured sample precision. **Frame these as MachtSinn-class
   edits:** MachtSinn already creates senses (gloss = item description) + P5137 with no
   BRFA because it's human-confirm, and our pre-BRFA edits are human-approved too — so
   the edit class is *established*, and the only novel thing we ask the community to
   accept is **LLM verification substituting for one human confirm, spot-checked**. That
   rides a precedent instead of pitching "bot creates senses" cold. Frame the trial so it
   measures senseless-write precision — the assumption becomes the trial's deliverable.
   Full verify + queue of the 6,679 only after that and after the cost sign-off.

## Open decisions (Karl)

- ~~**Gloss policy**~~: **DECIDED** — description-primary (label fallback, skip if
  neither), matching MachtSinn's community-accepted shape + optional en extra gloss.
  See the gloss-policy section.
- **Scale fallback if precision lands low**: if the sample gate (steps 1–3) shows
  precision clearly below the monosemous stream, the fallback is *not* a home-grown bot —
  it's running these as MachtSinn-style human confirms. Name this in the talk-page post.
- **+multi/GUIDED for this stream**: on gold it strictly wins and its marginal value is
  highest exactly when the target has no gloss — but it needs `item_lang` for all
  senseless anchors + the guided prompt (cache invalidation). Cost call; the sample
  measurement (step 3) tells us whether it's needed.
- **Scale sign-off**: full-pool verify (~6.7k candidates) after the sample cost
  extrapolation.
