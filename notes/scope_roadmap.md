# Scope limitations & roadmap: toward per-lexeme sense-inventory alignment

*Assessment written 2026-07-05. Companion to `sense_creation_plan.md` (phase 2) and
`p5137_anchor_propagation.md`. Numbers below are from `data/candidates.sqlite3` at the
current thresholds (high tier minus gate B = clean ∩ A ∩ D ∩ scope, any n_senses).*

## The three limitations

1. ~~**Cluster evidence uses only the pilot languages.**~~ **Lifted 2026-07-05**, in two
   steps, both keyed on `config.REVIEW_LANGS` (the pilot is now a review-axis concept only —
   `load_lexemes.py` loads review langs in full + all other WikDict langs anchor-only, and a
   target filter in `match.py` keeps candidates review-only):
   - **All-language clusters** (`build_clusters.py`, no pilot edge filter). On its own a
     *strict superset*: clean candidates 42,440 → 52,306, monosemous verify pool 1,179 →
     1,720 (+46%), senseless create-lane 7,220 → 9,944, 4,107 pairs promoted to gate-A.
   - **All-language anchors** (any WikDict language's existing P5137 can anchor a cluster).
     This is NOT additive — non-review anchors mostly act as a cross-language *agreement
     check*: clean 52,306 → 53,240 but with churn (9,407 new / 8,473 lost), and the
     monosemous verify pool drops 1,720 → **1,448** (430 lost, 158 new) as disagreements
     become `conflicting_anchors` abstentions. Sometimes a real precision catch (Habicht →
     Q846664 "hawk" is dropped because Italian *astore* pins Q25353 "goshawk", the narrower
     truth), sometimes over-conservative (vixen→fox broken by female-animal cross-talk).
     **Net original→now: monosemous pool 1,179 → 1,448 (+23%).** Kept deliberately (user
     call 2026-07-05) for the precision signal. Caveat: 27 already-decided pairs (9 written,
     18 approved) now fall to abstain — they stay writable (write.py reads the decision log,
     not the candidate stream) but the pipeline now disagrees with them → recheck candidates.
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
2. ~~**All-language cluster build**~~ **— done 2026-07-05**, plus all-language anchors (see
   limitation 1 above). Still to do on the richer evidence: re-size gate A/support
   thresholds (support now runs much higher — common concepts accrue 50–100+ clusters via
   non-pilot pivots); re-verify the current 1,448-candidate monosemous High tier (the pool
   both grew from clusters and was re-shaped by anchor disagreements, so cached verdicts no
   longer cover it 1:1); and triage the 27 already-decided pairs the anchor cross-check now
   contradicts (9 written, 18 approved) via `recheck` rows.
3. **Match-and-extend** for polysemous and multi-item lexemes: run the existing
   gloss·tolerant verifier per (item, sense) pair — item-to-sense-gloss matching is
   the judgment it already performs (the homograph catches prove it); items matching
   no sense go to the create lane. Gate on phase-2's measured precision; the edit
   class ("append senses to curated lexemes") needs its own talk-page framing.
