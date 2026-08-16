# Senseless lane: first fresh-stream precision measurement

*Measured 2026-08-16. Numbers here are FROZEN to that run and are the record, not a status
page — for live counts run `review_queue.py status --senseless` and `gates.py`.*

The senseless lane (`target_n_senses = 0`: create the lexeme's first sense, gloss it from the
anchor's CC0 description, attach P5137) holds the large majority of the gated pool and has
never written an edit. `sense_creation_plan.md` block 4 called its missing precision number the
project's critical path. This is that number.

## Design

- **Population.** The senseless High tier — clean ∩ A (≥2 anchors) ∩ D (concrete, in-scope
  anchor) ∩ n_senses = 0. 11,371 pairs.
- **Sample.** 240 pairs, `verify.py --gate AD --senseless-only --sample 240 --seed s1`. Drawn by
  hashing (seed, lid, qid), *not* by taking the head of the pool: `candidates()` orders by
  support DESC, so the 394 pairs verified in this lane before today are the easy end — 22%
  support-2 against the lane's 48%, and zero Russian. The draw reproduces the lane closely
  (de 140 / ru 42 / en 35 / sv 23 against a lane that is 63/17/11/10 percent).
- **Adjudication.** All 240 read one by one against the same evidence the review UI shows —
  the anchor's CC0 label and description in the display languages, the exact gloss `write.py`
  would create, support and anchor independence — and **blind to the verifier's verdict and
  reason**, which were joined back only afterwards. Recorded in `notes/runs/senseless_sample.csv`
  (decisions-shaped, `reviewer=claude/measurement`, deliberately NOT `config.DECISIONS`: it
  measures, it must not authorize a write). Read it with
  `calibrate.py --lane senseless --decisions notes/runs/senseless_sample.csv`.
- **Adjudicator.** Claude, not Karl. That is the caveat on every number below: the monosemous
  stream it is compared against was decided by a different reviewer. The rubric applied was
  "does this lemma, in this language, have a sense denoting this concept, and is the sense that
  would be created right?" — approve if a dictionary would give the anchor's concept as a
  definition of the lemma (a *minor* sense still counts, per the plan's S1-rank argument);
  defer on a scope/adjacency judgment call, a contested convention or a bad gloss; reject when
  the lemma has no such sense.
- **Cost.** $2.29 for 240 pairs over 30 `claude -p` calls, batch 8, sonnet — $0.0095/pair.
  Extrapolated full-lane verify: **~$110**.

## Result

|                                          | senseless (this run) | monosemous (live) |
| ---------------------------------------- | -------------------- | ----------------- |
| write-lane precision                     | **171/184 = 92.9%**  | 407/412 = 98.8%   |
| 95% Wilson CI                            | [88.3%, 95.8%]       | [97.2%, 99.5%]    |

Write-lane = the verifier said 'yes' AND the relation is EQUAL or a convention keep — i.e.
exactly what `write.py` would write. The same measure `review_queue.py status` prints.

**The lane does not clear the plan's gate.** The gate was "reviewed precision comparable to the
monosemous stream (overlapping CIs)"; the CIs are disjoint. Do not scale.

But the shortfall is not what the risk model predicted:

- **Zero of the 184 write-lane rows are a wrong sense.** All 13 losses are *defers* — scope,
  contested convention, or a bad gloss. The monosemous lane's 5 losses are outright rejections.
  So this lane is not creating false meanings; it is over-linking on scope and writing some bad
  glosses.
- **The verifier catches 25 of the 30 rejections** (83%), and all 5 it misses are rated
  INCOHERENT / LATERAL / NARROWER — outside the write lane, where the scope hints hold them.
  The gloss-less lemma-only mode did not produce the homograph flood the risk model feared:
  the homographs in the sample (Rückenflosse→Finns, Ruhepunkt→a Linux syscall, Versfuß→the
  anatomical foot, Lebensgefährte→an order-of-chivalry title, непалец→the Nepali *language*)
  were caught by the anchor-side evidence alone.

### Where the 13 write-lane losses come from

| class | n | fixable how |
| ----- | - | ----------- |
| member-vs-group (a demonym/ethnonym linked to the people) | 5 | deterministic — see below |
| gloss quality (wrong language, or a description that defines nothing) | 4 | deterministic |
| genuine scope over-link (`Leguan`→the genus, `fluff`→down feather, `gullet`, `borgare`) | 4 | prompt |

**Member-vs-group is inverted in this lane, and that is a bug, not a prompt weakness.** The
demonym→ethnic-group nudge that defers these in the monosemous lane is
`is_ethnic_demonym(gloss, anchor_desc)`, and a senseless target has no gloss, so it can never
fire — a gap `sense_creation_plan.md` already recorded. What was *not* known is that the
fallback does the opposite of nothing: `_CONVENTION` substring-matches the verifier's prose
reason, and that list contains "ethnic" and " people" for the *nationality* convention. So
`NARROWER: female individual vs whole Romani ethnic group` matches "ethnic" → `NARROWER·convention`
→ **keep**. The exact signal that means "contested, defer" when a gloss exists means "blessed,
keep" when it doesn't. Three of the five (Lappin, Zigeunerin, aztek) reach the write lane that
way; nationality demonyms in the same sample (US-Amerikanerin, иранка, малайзиец) are correct
keeps, so the class cannot simply be dropped — it has to be split on whether the anchor is an
ethnic group, which is `notes/redo.md` #3's `anchor_is_ethnic_group` field exactly.

**Higher support does not help.** Write-lane precision by support: 95.3% (n=64) at 2, 90.0%
(n=50) at 3, 92.9% (n=70) at 4+. The plan's fallback "restrict to a tighter slice (higher
support) and re-measure" is refuted for this lane — flat, if anything inverted. (The *raw*
yes-rate does rise with support, which is what earlier eyeballing saw; that is the verifier
rejecting more low-support pairs, not the survivors being better.)

**Russian scored 31/31**, and Russian cannot be glossed in Russian. `anchor_gloss` resolves
through `config.CONCEPT_LANGS_DISPLAY`, which has no `ru` — so every Russian sense would be
created with an English gloss even though 1,370 of the lane's 1,895 Russian pairs (72%) have a
Russian CC0 description available. That matters beyond tidiness: Russian is 17% of this lane,
Karl does not read it, and the review capacity Infovarius offered on the talk page is the plan's
answer for it.

## What this says to do

1. **Not scale, and not write from this lane yet** — the gate is not met on the number that
   was specified in advance.
2. **Fix the two deterministic classes first** (9 of the 13 losses). Neither needs a better
   model: one is a triage inversion, one is gloss selection.
3. **Then re-measure against this same sample.** The adjudications above are now a gold set for
   this lane, and the verdict cache is additive, so any prompt revision is graded free:
   `validate_prompt.py --decisions notes/runs/senseless_sample.csv --baseline-ver <old>`.
4. The residual 4 genuine scope over-links are the part that actually needs the model to get
   better, and they are ~2% of the write lane. Whether that is worth a prompt iteration is a
   decision to make *after* the deterministic 9 are gone and the number is re-measured.
