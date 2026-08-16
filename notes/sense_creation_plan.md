# Senseless lexemes: sense-creation plan (phase 2)

*Extend the P5137 pipeline to targets with **zero** senses: create the sense (CC0 gloss)
and attach the P5137 in one edit. Plan drafted 2026-07-02; companion to
`p5137_anchor_propagation.md`.*

> **Status 2026-08-16 — this is the critical path.** The lane is fully built (gates, verify,
> review UI, write path, duplicate-sense preflight) and has approved rows waiting, but it has
> **never written an edit and its precision has never been measured**. It holds the large
> majority of the gated pool, so it is also the only lane where a bot request would be worth
> filing — and the request cannot be written until the measurement below exists. The staging
> section is therefore the live part of this document; the code sections are history.

*Amendment folded in 2026-07-05: review and write are **lexeme-centric, not pair-wise**. Some
senseless lexemes have several candidate items, and independent per-pair sense creation would
create near-duplicate senses that the live same-P5137 guard cannot see. So the review lane groups
all candidate items per lexeme and approval covers the resulting inventory including each gloss;
`write.py` builds one edit per lexeme, and `--limit` is lexeme-aware so an inventory is never
split.*

## Why this is the main prize

The pilot writes only monosemous targets (add P5137 to the one existing sense). But the
candidate stream is **dominated by senseless lexemes** — the ones whose first meaning we would
create. They are the large majority of the gated pool at every tier, and the gap is not close;
the monosemous and polysemous lanes together are a small fraction of it. (Live sizes:
`uv run gates.py`.)

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
- Early signal: the original small senseless sample verified with a solid yes-rate and
  *correct* rejections (`Schützin`→"alcoholic mixed drink").

Blocks:
1. ~~`gates.high_tier_where()` hard-codes `target_n_senses = 1`~~ **— DONE.** Parameterized by
   lane, one definition consumed by every stage, so the tiers can't drift. (It has since become a
   three-way lane selector, with a self-check asserting the lanes partition the pool.)
2. ~~Gloss policy implemented~~ **— DONE.** `review_queue.anchor_gloss(it, qid, lang)`
   is the single source: description-primary, label fallback, skip if neither, plus an en gloss
   for legibility. The review UI (`--senseless` lane) previews the *exact* text write.py will
   write — both call this one function.
3. ~~Gloss language-key bug~~ **— DONE.** `anchor_gloss` returns `{gloss_lang: text}`
   keyed by the language the text is actually in, and `build_edit()` writes each under its own
   key — an en fallback is now keyed `en`, never as the target language.
4. **No fresh-stream precision number — STILL OPEN, and now the project's critical path.**
   None can come from held-out data: senseless targets have no P5137 to hide, *by construction*.
   The lane, the review UI and the write path are all ready; what is unmeasured is whether the
   stream is accurate enough to propose at volume. Verify a sample → human-review the 'yes' rows
   → gate, per the staging section below.

## Risk model — how this stream differs from monosemous

- **The gloss·tolerant safety net is absent.** The current verifier's homograph catches
  (sheet/hov) come from reading the target's own gloss; senseless targets have none, so
  the verifier runs in exactly the lemma-only mode that missed all 4 genuine homographs
  on gold. Compensations: the anchor-side signal is untouched (label+description), the
  demonym/ethnic scope-hint nudge is deterministic (no gloss needed), and `item_lang`
  multi-language labels give the reviewer (and optionally the +multi verifier) a
  cross-language consistency check.
  **This is the one risk the community has already asked about.** On the talk page, the answer
  to "how do you deal with homographs?" was: the LLM sees the sense gloss and the item
  descriptions, plus heuristics "like excluding lexemes that have multiple senses". This lane
  removes *both* of those safeguards — no gloss exists, and the monosemous restriction is
  exactly what the lane lifts. So the disclosure in staging step 5 must say so plainly and say
  what replaces them; quietly shipping a lane that voids a public answer is the one thing that
  would actually damage trust here.
- **Minor sense as S1 — a gloss concern, not a rules one.** "Senseless" ≠ "monosemous in
  reality". If the cluster sense is a polysemous word's *minor* meaning, we create S1 =
  the minor sense. That breaks no requirement (`wikidata-policy/lexeme-data-model.md`:
  sense ids auto-increment and are never renumbered, so rank is a display artifact — not
  worth engineering around). The real exposure is that later editors pile dominant-meaning
  data onto an under-specific gloss. Mitigation: the gloss makes the sense self-describing,
  and every pre-BRFA edit is human-approved *including the gloss text*.
- **Precision is an assumption until measured.** The monosemous lane's reviewed precision is
  *inherited* here, not demonstrated, and the one early sample's yes-rate landed between the
  (easy, enriched) held-out population and the fresh monosemous stream — so this stream's
  difficulty is genuinely unknown, in both directions. The staging below makes measuring it the
  first deliverable.

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
   and, for lexemes with several candidate items, a ⚠ **multi-candidate** flag with same-lexeme
   rows sorted adjacent (lexeme-centric review — approve/prune the inventory as a set).
   Existing hints apply (the polysemy-collapse hint is repurposed to the wrong-first-sense
   warning; the demonym→ethnic nudge is inactive without a target gloss — a known gap).
4. ~~**write.py** — gloss keying fix + description-primary policy + guard tightening.~~ Done.
   Plus **lexeme-centric writes**: all approved items for one lexeme are grouped into ONE
   `wbeditentity` creating one sense each (`--limit` is lexeme-aware so an inventory is never
   split — a partial write would be permanently blocked by the tightened guard). The guard now
   skips any create edit whose live lexeme has **any** sense ("no longer senseless"), subsuming
   the old per-P5137 check.

## Measurement & staging (cost-gated)

1. **Verify a sample first, not the pool.** A few hundred random candidates on the current
   prompt. Record the yes-rate *and* the actual token cost, extrapolate the full-pool run, and
   get sign-off before spending it.
2. **Human-review the sample's "yes" rows** in the senseless lane. This yields the stream's
   first real precision number with a Wilson CI — the same measurement design that produced the
   monosemous stream's reviewed precision, so the two are directly comparable.
3. **Gate to proceed:** reviewed precision comparable to the monosemous stream (overlapping
   CIs). If it lands clearly lower — plausible, since this lane has no gloss net — do NOT scale.
   Options then: re-verify the sample with richer anchor evidence (it helps most exactly when
   the target has no gloss), restrict to a tighter slice (higher support) and re-measure, or
   fall back to human-confirm-per-edit rather than a bot.
4. **A first batch of live edits**, hand-approved, in its **own EditGroups batch** (so the
   sense-creation class can be reverted separately from statement-adds). Inspect the on-wiki
   rendering of the created senses (gloss display, Ordia). Some rows are already approved and
   waiting; this step needs no permission and can run before or alongside (1).
5. **Disclose the new edit class, then scale.** The 2026-06-29 talk-page post covered the
   monosemous lane only, and this lane is already **promised, not described**: Infovarius was
   told on 2026-08-13 that a senseless version was in the works and would be announced when
   ready. So step 5 has a waiting audience and a specific shape — report back in that thread
   with the gloss policy, the measured sample precision, and an explicit answer to the
   homograph question this lane re-opens (see the risk model). Infovarius also offered to review
   Russian results, which matters because Russian lexemes are overwhelmingly senseless: this
   lane is what turns that offer into usable review capacity for a language Karl can't read. **Frame it as MachtSinn-class:**
   MachtSinn already creates senses (gloss = item description) + P5137 with no BRFA because it
   is human-confirm, and our pre-BRFA edits are human-approved too — so the edit class is
   *established*, and the only novel thing we ask the community to accept is **LLM verification
   substituting for one human confirm, spot-checked**. That rides a precedent instead of
   pitching "bot creates senses" cold. Scope any eventual request at the **action** — "create a
   sense for a translation-attested meaning Wikidata does not record" — so it doesn't have to be
   refiled when the same action is applied to a lexeme that already has senses (`create_add`).

## Open decisions (Karl)

- ~~**Gloss policy**~~: **DECIDED** — description-primary (label fallback, skip if
  neither), matching MachtSinn's community-accepted shape + optional en extra gloss.
  See the gloss-policy section.
- **Scale fallback if precision lands low**: if the sample gate (steps 1–3) shows
  precision clearly below the monosemous stream, the fallback is *not* a home-grown bot —
  it's running these as MachtSinn-style human confirms. Name this in the disclosure.
- **Richer anchor evidence for this stream**: it wins on gold and its marginal value is highest
  exactly when the target has no gloss, but it costs a prompt revision (cache invalidation).
  The sample measurement tells us whether it's needed — don't pay for it blind.
- **Scale sign-off**: full-pool verify only after the sample's cost extrapolation.

## Beyond the first sense: `create_add`, and why it is a harder sell

Creating a sense on a lexeme that **already has senses** is the same action applied to a
different target, and it is not hypothetical — the polysemous lane's abstentions are exactly
this case, and an abstention is strong evidence that the sense really is missing. `write.py`
refuses it today. Two arguments (carried over from the retired scope roadmap) say why it should
stay behind the senseless lane rather than ship with it:

- **The licensing surface grows.** Creating one first sense reproduces no partition. Deriving a
  *sense inventory* for a polysemous word comes much closer to republishing DBnary's editorial
  sense segmentation — the weakest point in the "we only use facts" story. A defence exists (the
  partition is re-derived through Wikidata's own item space; clusters merge onto one item or drop
  out) but it has to be *argued*, not assumed.
- **There is no precedent.** MachtSinn establishes "first sense on a senseless lexeme,
  human-confirmed". "A bot appends senses to lexemes humans already curated" has none, and is a
  much harder request. Senseless-first is also the polite sequencing.
