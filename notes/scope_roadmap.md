# Scope limitations & roadmap: toward per-lexeme sense-inventory alignment

*Assessment written 2026-07-05. Companion to `sense_creation_plan.md` (phase 2) and
`p5137_anchor_propagation.md`. Numbers below are from `data/candidates.sqlite3` at the
current thresholds (high tier minus gate B = clean ∩ A ∩ D ∩ scope, any n_senses).*

## The three limitations

1. ~~**Cluster evidence uses only the pilot languages.**~~ **Lifted 2026-07-05.** Clusters
   now build from all ~26 WikDict languages (`build_clusters.py`); the pilot limit moved to
   the review axis only (`config.REVIEW_LANGS`, enforced by `load_lexemes.py` + a target
   filter in `match.py`, with anchors left language-agnostic). Measured effect on the same
   dump: clean candidates 42,440 → 52,306 (a strict superset — zero coverage lost), the
   monosemous verify pool 1,179 → 1,720 (+46%), the senseless create-lane 7,220 → 9,944
   (+38%), and 4,107 shared pairs promoted from support=1 to gate-A (support≥2). Still open:
   **anchors from a non-review language** — a cluster whose only P5137-bearing member is,
   say, Italian can't yet supply an anchor because `load_lexemes.py` loads only review-lang
   lexemes; add that language's Q-id there to realise the extra "distinct items per lexeme".
2. **We defer items that aren't the recorded sense.** 150 of 639 decisions in
   `runs/decisions.csv` are deferred (23%) — minor-sense/homograph, BROADER/NARROWER,
   LATERAL, demonym→ethnic.
3. **We only write monosemous targets** (gate B), with `sense_creation_plan.md`
   extending to senseless ones.

The proposed end-state that addresses 2+3 together: per lexeme, gather the full set of
candidate items, **match** some to existing senses, **create** senses for the rest
("match-and-extend"). That is the right target architecture — the cluster data is
already per-source-sense, so it's what the data structurally wants. But it should not
replace the phase-2 plan, for the reasons below.

## Sizing: match-and-extend is a ~2% slice today

High-tier-minus-B candidate pairs by target sense count:

| target_n_senses | pairs |
|---|---|
| 0 (senseless) | 7,220 |
| 1 (monosemous, current stream) | 1,179 |
| 2+ (polysemous) | **135** |

Distinct candidate items per senseless lexeme: 6,938 have exactly one; 124 have two,
10 have three, 1 has four → **135 of ~7,073 (~2%) are multi-item**. Polysemous
lexemes: 111 single-item, 11 multi-item.

So match-and-extend as a new capability touches ~270 lexemes today, while single-item
first-sense creation — exactly what `sense_creation_plan.md` covers — is ~98% of the
senseless volume. The phase-2 plan is where the mass currently is.

## Limitations 1 and 3 are coupled

Items-per-lexeme is low *because* the cluster evidence comes from few languages.
All-language WikDict input raises both support counts and distinct items per lexeme —
it is the prerequisite that turns match-and-extend from a 2% case into the main
pipeline. It is also the cheapest limitation to lift: input-side only (more translation
edges, same licensing shape, no new edit class, no community question), and it
strengthens the existing streams (more candidates clear gate A; support becomes a
stronger signal). Sequence it **before** the inventory work. Cost caveat: the cluster
rebuild is local SQL, but the enlarged verify pool is a token-cost decision — size it
before running.

## What match-and-extend rescues from the defer pile — and what it doesn't

Rescued: minor-sense/homograph cases (the item is a true meaning of the word, just not
the recorded sense) and NARROWER cases where the lemma genuinely has the narrower
reading. **Not** rescued: BROADER and LATERAL scope mismatches (the item still isn't
EQUAL to any sense we could honestly create) and demonym→ethnic (community-unresolved,
not inventory-limited — see `p5137-demonym-ethnonym` notes). Limitation 2 shrinks but
does not vanish.

## What the bigger step costs (why it's phase 3, not phase 2)

- **New error class: fine-grained WSD.** Matching an item to one of several existing
  senses is exactly the task class the talk-page draft argues makes the human gate
  load-bearing. The 119-case gold covers none of it; precision there is unmeasured.
- **New error class: partition design.** Creating multiple senses means deciding a
  sense inventory; near-synonym items → duplicate/overlapping senses.
- **Licensing surface grows.** A one-sense-per-cluster inventory on a polysemous word
  comes much closer to republishing DBnary's editorial sense segmentation
  (`sense_num`) — flagged in `brainstorm.md` as the weakest point of the "pure facts"
  story. Single first-sense creation reproduces no partition; a derived inventory
  arguably does. A defense exists (partition re-derived through Wikidata's own item
  space; clusters merge onto one item or drop out), but it must be argued, not assumed.
- **No precedent.** MachtSinn covers "first sense on a senseless lexeme,
  human-confirmed". "Bot appends senses to lexemes humans already curated" has no
  precedent — a much harder BRFA sell. Senseless-first is also the polite sequencing.

## Amendment to phase 2, effective now: lexeme-centric review/write

The per-lexeme framing exposes a latent hazard in `sense_creation_plan.md` as written:
it is pair-wise, but 135 senseless lexemes have 2–4 candidate items, and per-pair
`write.py --include-senseless` would create 2–4 senses on one lexeme independently —
near-synonym items become duplicate senses, and the live guard (same-P5137 check only)
won't catch it. Therefore: the senseless review lane groups **all candidate items per
lexeme**, and approval covers the resulting inventory (including each gloss), not each
pair blind; `write.py` builds one edit per lexeme. Cheap, needed for correctness
regardless of scope, and it is the first concrete increment of the target architecture.

## Recommended sequence

1. **Phase 2 as planned** (`sense_creation_plan.md`: sample → measure → gate → ~10
   hand-approved edits → disclose), amended to lexeme-centric review/write.
2. ~~**All-language cluster build**~~ **— done 2026-07-05** (input-side widening; see
   limitation 1 above). Still to do on the richer evidence: re-size gate A/support
   thresholds (support now runs much higher — common concepts accrue 50–100+ clusters via
   non-pilot pivots), and cost sign-off for the +46% verify pool before re-verifying.
3. **Match-and-extend** for polysemous and multi-item lexemes: run the existing
   gloss·tolerant verifier per (item, sense) pair — item-to-sense-gloss matching is
   the judgment it already performs (the homograph catches prove it); items matching
   no sense go to the create lane. Gate on phase-2's measured precision; the edit
   class ("append senses to curated lexemes") needs its own talk-page framing.
