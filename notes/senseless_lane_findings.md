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

## Refinement, and what re-measuring it showed (same day)

The structured verifier reply landed (`notes/redo.md` #3 — `relation` +
`gendered_form` + `demonym` + `anchor_is_ethnic_group`, serialised as
`<REL>·<tag>·<tag>: prose`) and the same 240 pairs were re-verified under it, prompt
`32e24a6f`, $2.91. Graded against the adjudications above.

|                            | prose `59d6f472` | structured `32e24a6f` | monosemous |
| -------------------------- | ---------------- | --------------------- | ---------- |
| write-lane precision       | 171/184 = 92.9%  | **172/183 = 94.0%**   | 98.8%      |
| 95% CI                     | [88.3, 95.8]     | [89.6, 96.6]          | [97.2, 99.5] |

`validate_prompt.py --decisions notes/runs/senseless_sample.csv --baseline-ver 59d6f472`
on the new prompt: **keep 182/182 = 100%**, **write-lane catch 24/24 = 100%**, PASS.

**The mechanism works and is verified.** Probed directly on the eight decisive pairs, the
demonym split is 8/8: Lappin, Zigeunerin, Polynesier, aztek and pariah all defer;
US-Amerikanerin, иранка and малайзиец all keep. In the full run, four pairs changed action
*because a flag was set*, and all four were correct.

**But the aggregate barely moved, and that is the more useful finding.** Twenty pairs changed
action between the two runs. Decomposed by whether the structured fields were involved:

| rows | changes | correct | wrong |
| ---- | ------- | ------- | ----- |
| flag-carrying (`·gendered`, `·demonym`, `·ethnic-anchor`) | 4 | 4 | 0 |
| plain (no flag set) | 16 | 7 | 8 (+1 neutral) |

So the fix contributed only wins, and everything else is **run-to-run variance in the model's
relation call** — symmetric, and the same size as the fix. Holding the relations fixed and
changing only the triage, the demonym fix alone is worth 171/184 → 171/179 = 95.5%
[91.4, 97.7]; the observed 94.0% is that minus the noise that happened to land the other way
(Emblem, Jagdfalke, Nachahmung, сокровище, дьяволопоклонничество all went EQUAL → BROADER or
INCOHERENT on re-run, with no flag involved).

**This is the `rigorous-method-retesting` rule biting.** A single sample per pair cannot resolve
a ~2pp effect when the relation call alone moves ~6% of pairs between runs. Any further prompt
iteration on this lane needs a multi-sample vote before its aggregate is read as a result — the
decomposition above, not the headline, is what made this run interpretable.

**Still missing the gate**: 94.0% [89.6, 96.6] against 98.8% [97.2, 99.5], CIs still disjoint.

### The other loss class: gloss quality, partly addressed

- **Russian glosses — FIXED.** `anchor_gloss` now reaches the target language even when it is
  outside `CONCEPT_LANGS_DISPLAY` (`review_queue.display_spec`). 1,370 of the lane's 1,895
  Russian pairs now get a Russian gloss instead of an English one; no other language's gloss
  choice moves, and the verifier's own concept spec was deliberately not touched.
- **Gloss in a third language only — MEASURED AND DECLINED.** Holding rows whose sense could
  only be glossed in neither the target language nor English would have caught Ideologin (sv
  gloss on a German lexeme) and förbundskansler (German, feminine-specific, on a Swedish one) —
  2 of the sample's 13 losses. But the class is **36 of 11,371 pairs, 0.3% of the lane** (the
  sample over-represents it at 0.8%), so its real effect is ~0.3pp, and the precedent on
  rare-class automation is to leave it. Recorded so it is not re-proposed without the number.
- **Non-defining glosses** (`Differenz` → "philosophischer Fachausdruck") and
  **person-vs-activity** (`Hafenarbeiter`, whose anchor description defines the occupation, not
  the worker) are left open. Neither has a deterministic test that does not also fire on
  terse-but-real glosses like "Musikinstrument", which are fine.

## What this says to do

1. **Not scale, and not write from this lane yet** — the gate is not met, before or after the
   refinement.
2. ~~Fix the deterministic classes~~ **DONE for the two that were worth it** (the demonym triage
   inversion, and Russian glosses). The third was measured and declined as too rare.
3. ~~Re-measure~~ **DONE**, and it produced a methodological result rather than a precision one:
   at one sample per pair, relation-call variance is the same size as the fix. **The next prompt
   iteration on this lane must use a multi-sample vote**, or its headline will again be
   uninterpretable. Grading itself stays free —
   `validate_prompt.py --decisions notes/runs/senseless_sample.csv --baseline-ver <old>`.
4. The residual genuine scope over-links (`Leguan` → the genus, `fluff`, `gullet`, `borgare`) are
   the part that needs the model to get better. They are ~2% of the write lane, which is below
   the noise floor of the measurement design used so far — another reason the next iteration
   needs multi-sample voting before it can claim anything.
5. **Two gates now disagree, and that is a decision for Karl, not for the code.** The plan's gate
   (precision comparable to the monosemous stream) says stop. `calibrate.py`'s pinned
   auto-approve gate — ≤2% human-rejected AND ≥80% approved — reads
   `EQUAL → AUTO-APPROVE (safe 100%, appr 95%)` on this very sample, because every loss in this
   lane is a *defer* and none is a wrong write. The two gates measure different risks: "is this
   as good as the lane we already trust" versus "would auto-approving it write something wrong".
   Nothing should auto-approve here on the strength of a 240-pair sample adjudicated by one
   reviewer, but the disagreement is real and worth resolving deliberately rather than by
   whichever instrument gets run first.

## Postscript: the full-pool re-verify contradicts the 240-pair read

The whole cached pool was re-verified at `32e24a6f` (3,152 pairs, 182 batches, **$33.49**) to
restore the live queue, which reads only the current prompt version. That produced a second,
much larger read on the same prompt change — 490 human decisions instead of 240 measurement
adjudications — and it does not agree with the first.

**Against the human decision log, the structured prompt is worse, not better.** Of the pairs each
prompt would write, the share the human approved or wrote:

| prompt | write-lane precision |
|---|---|
| `59d6f472` (prose) | 409/441 = **92.7%** |
| `32e24a6f` (structured) | 413/467 = **88.4%** |

It keeps 26 more pairs and is right about fewer of them. Grading every action change individually:
**63 right / 60 wrong** — indistinguishable from a coin flip, exactly the noise floor this
document already reported, now confirmed on 5× the data.

The flag-carrying decomposition, which was the reason to believe the fix worked (4/4 correct on
the sample), **inverts at scale: 11 right / 18 wrong.** Both halves are informative:

- **Right, and the fix is doing its job:** Kirgise, belarusier, lett, gitan, Chinese, Irishman,
  fransyska all moved keep → defer. These are the genuinely contested ethnic-minority demonyms —
  the Lappin/Zigeunerin/aztek class the change was built for.
- **Wrong, and it is one systematic class:** Asiat, Asiatin, afrikan, afroamerikan, europé,
  Philippine moved keep → defer. `anchor_is_ethnic_group` fires on *continent- and
  nation-scale origin terms*, which this project has already decided are blessed keeps — these
  are among the 19 written rows that closed the deterministic P31 route (same failure mode, new
  mechanism). The model draws the ethnic/nationality line in a different place than Wikidata's
  codified "citizens or residents of X" convention does.
- **Wrong, and unanticipated:** Neuseeländerin, Österreicherin, Néo-Zélandaise, filippinska,
  maltes now come out `INCOHERENT` — *"sv says ethnic group, others say citizens/residents"*.
  Asking the model to separate ethnic groups from citizens taught it to notice that many
  demonym items conflate the two across their multilingual descriptions, and it now refuses to
  judge them instead of choosing. That is a real observation about those items and the wrong
  response to it: the queue defers a class Karl has been writing.

Scale of the drift overall: 139/3,152 verdict flips (4.4%); of the 2,343 pairs both prompts call
'yes', the relation changed on 15.7% and the **triage action on 12.2%** — 11.0% among the 1,697
still-undecided pairs, so the queue's composition really does move. 42 approved/written decisions
would no longer be kept, 22 of them newly (the rest were already human overrides of a defer).

**Reverting is free.** The cache is additive and keyed by prompt version; the 3,451 rows under
`59d6f472` are still there, and `PROMPT_VER` is `sha1(SYSTEM)[:8]`, so restoring the prose
prompt byte-for-byte makes them live again with no re-verify. Verified by recomputing the hash
from `115596b~1`. The structured-reply plumbing (`compose_reason`, `parse_reason`,
`scope_kind`'s structured branch) is version-agnostic and can stay either way.

**The methodological lesson is the durable part, and it is sharper than before:** a 240-pair
sample adjudicated in one pass endorsed a change that 490 real decisions say is a regression.
The sample was not wrong about the four pairs it saw — it was too small to see the class the
change would break, because the senseless lane it was drawn from is 73% de/ru common nouns and
almost none of the affected demonyms live there. **Measure a prompt change on the population it
will be applied to, not on the lane that motivated it.**

### Correction to the postscript: the regression verdict rested on the confounded benchmark

The section above graded the prompt change against `decisions.csv` alone. Two more reference sets
exist, and both are cleaner. The verdict does not survive them.

**The curated 119-case gold** (`config.GOLD` — contested mono-lane pairs, each with a written
rationale and an explicit acceptable-output set; 99 of 119 still hold verdicts under both prompts):

| | in-set | best-action exact | defers | false-keep |
|---|---|---|---|---|
| prose `59d6f472` | 93/99 = 93.9% | 58/99 = 58.6% | 76 | 0 |
| structured `32e24a6f` | 92/99 = 92.9% | 65/99 = **65.7%** | **54** | 1 (`sv arrangemang`) |

Head-to-head on the 34 cases where the two disagree, scored against the acceptable set:
**prose-only-right 6, structured-only-right 5, both acceptable 23, both wrong 0.** That is a tie,
not a loss. Structured reaches the gold's single *best* action 7pp more often and defers 29% less
on the hardest slice — same correctness, less review burden. (An earlier read of this table said
28–6 for structured; that was a scoring bug — cases where both actions were acceptable were being
credited to the new prompt. Corrected here.)

**The decision log is not a neutral referee.** Every one of its 681 decisions was made with the
prose prompt's own `reason` visible in the queue, and since 2026-06-25 (`8332300`) with its action
hint too. Grading a challenger against decisions taken while reading the incumbent's output gives
the incumbent a home-field advantage of unknown size, and 4.3pp is well inside what that could
produce. The pre-hint slice is too small to bound it (n=22, 12 vs 10 keeps).

**Reverting definitely re-breaks one thing.** On the blind 240-pair senseless gold, the four
contested demonym rows are unanimous:

| | prose | structured | human |
|---|---|---|---|
| de Lappin, de Polynesier, de Zigeunerin, sv aztek | keep | defer | **hold** (all 4) |

That is the member-vs-group inversion this document opened with — the legacy `_CONVENTION` path
cannot fire without a target gloss, and the senseless lane has none by construction. Nothing is
written from that lane today, so the bug is latent rather than live, but a revert restores it.

**Two classes to keep an eye on either way**, from the 681 decided pairs:
- `demonym-ethnic` is 32 rows: 25 correctly held, 7 wrongly held (78%). The postscript listed only
  the 7 losses, which made a mostly-working class look like a regression.
- `INCOHERENT` is 26 rows and the human kept 46% of them — deferring on it is barely better than a
  coin flip, and the structured prompt produces more of them (48 → 64). This is the soft spot.

**Where that leaves it: no benchmark shows structured is better, two show a tie, and the one that
shows it worse is the one that cannot be neutral.** The honest summary is that the two prompts are
not distinguishable on correctness with the evidence in hand.

**The experiment that would settle it costs $0.** 186 undecided pairs carry both verdicts already
and the two prompts disagree about writing them (101 defer→keep, 85 keep→defer). Adjudicating a
blind sample of *those* — no LLM calls, both answers cached — is the only unconfounded head-to-head
available, and grading disagreements only is high-signal: ~50 pairs separates a real 4pp effect
that 681 confounded decisions cannot.

### The blind head-to-head settles it: keep the structured prompt

*2026-09-03. $0 in LLM calls — both verdicts were already cached.*

The population is every undecided pair in the gated pool (all three lanes) that both prompts
call 'yes' but triage differently: **146 pairs** (the earlier "186" counted cache rows outside
the current gates). Adjudicated one by one, sorted by (language, lemma), against what the review
UI shows — anchor CC0 label/description per display language, the target's sense inventory with
the assigned sense marked (poly), or the gloss that would be created (senseless) — and blind to
both verdicts and to which prompt keeps. Same rubric and same adjudicator as the 240-pair sample.
Recorded in `notes/runs/disagree_sample.csv` (decisions-shaped, `reviewer=claude/measurement`,
not `config.DECISIONS`).

Every pair has exactly one prompt saying *write*, so each adjudication names a winner:

| | n | prose right | structured right |
|---|---|---|---|
| **all** | 146 | 55 | **91** |
| mono | 53 | 27 | 26 |
| poly | 44 | 13 | **31** |
| senseless | 49 | 15 | **34** |
| structured holds what prose wrote | 79 | 19 | **60** |
| structured writes what prose held | 67 | **36** | 31 |

Two-sided sign test p = 0.004. Of what each prompt would write from this set, prose is right
19/79 = 24%, structured 31/67 = 46%.

Reading by lane:
- **mono is a tie**, exactly as the 119-case gold said. Structured's extra writes here are
  8/20 right, its extra holds 18/33 right — the same noise-floor trade in both directions.
- **poly is not a discrimination win.** 43 of the 44 disagreements should be held (11 are an
  assigned sense that is *already linked*, the rest a missing or wrong free sense), and
  structured simply holds more of them: 30/31 of its extra holds right, but 1/13 of its extra
  writes. Neither prompt sees the sense inventory; `assign.py` and the taken-sense guard carry
  that lane, not the verifier.
- **senseless is the real win**, in both directions: structured's extra writes are 22/34 right,
  its extra holds 12/15 — including the four demonym rows (Lappin, Polynesier, Zigeunerin,
  aztek) held again blind. Which is what the change was built for.

**Verdict: the decision-log regression was the benchmark, not the prompt.** On the only
unconfounded head-to-head, structured is better on two lanes and equal on the live one.
Keep `32e24a6f`. The 42 decided pairs that now disagree with the cache stay as they are: the
log is the authority on decided pairs, and they never re-enter the queue.

Still true either way: the disagreement set is where both prompts are at their worst (24% and
46% keep-precision), so nothing here changes the auto-approve picture — these pairs are the
review queue's job.
