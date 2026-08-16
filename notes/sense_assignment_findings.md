# Sense assignment: what the measurements said, and what got built

*2026-08-15/16. Everything here is measured against the live DBs unless marked as an estimate.
LLM spend for the whole exercise: ~$21 (~$8 measurement arms, ~$13.80 the lane build).*

The pipeline used to ask a **pairwise** question — "does this noun denote this concept?" — and
answer it at **lexeme** granularity. That is only sound while gate B pins the target to exactly
one sense, which is why the pilot was monosemous. This is the record of generalising it to a
per-sense question, and of what the generalisation turned up.

---

## 1. The measurements

Three self-grading arms (`assign_eval.py`), no human labelling. All share the production prompt
by import, fingerprint `4346c40f` — a measurement of a drifted prompt measures nothing.

| arm | n | result |
|---|---|---|
| **gold** — which sense does this anchor belong to? | 300 | top-1 **94.7%** [91.5, 96.7] |
| **absent** — same, with the correct sense deleted | 200 | said "none" **60.0%** [53.1, 66.5] |
| **absent_intact** — unmodified, same batches | 100 | top-1 93.0%, abstained **1%** |
| **control** — monosemous pairs the human decided | 376 | 98.9% named the sole sense |

**Selection is not the problem.** All 16 gold misses were read individually: **zero clean model
errors.** Nine are Wikidata recording the same sense twice (*odontology* "a science dealing with
the teeth" / "the science of dentistry"), two are cases where the model beat the on-wiki link
(*accélérateur* — the concept is a cement admixture and the model chose the sense reading "…la
prise du béton", while the wiki points at the *device* sense; *friendliness* — a personality
trait, not "positive emotion"), two are defensible scope abstentions, three are judgement calls.
**94.7% is a floor, not a ceiling.**

**Abstention discriminates 60:1.** In the same batches, 60% abstention when the answer was
removed against 1% when it was present. That is the number write-safety rests on, and the gold
arm was blind to it by construction.

**The residual is ~9.5%, not 40%.** All 80 absent-arm misses were read: ~51 picked a *redundant
duplicate* of the deleted sense (benign — the statement written is still true), ~10 are
broader/narrower rank shifts the convention already defers, **19 picked a genuinely different
sense**. And that is the constructed worst case, where the true sense is absent entirely.

### The multiplier, measured live

The one quantity the arms could not supply was how often the true sense is simply **missing from
Wikidata**. Running the assigner over the live lane produced it as a side effect:

> 347 verifier-approved polysemous candidates → **125 abstentions = 36.0%** [31.2, 41.2].
> Inverting the two conditionals: **P(true sense missing) = 59%**, range [51%, 68%].
> → genuinely-wrong sense reaching human review ≈ **5.6% of candidates**.

Every abstention inspected is a real sense Wikidata has not recorded: *consumption* →
tuberculosis, *heart* → soul, *demonstration* → political protest, *point* → full stop,
*register* → chimney damper, *slide* → playground slide.

Same inversion the other way: **P(the sense really is missing | the assigner abstained) ≈ 99%.**
An abstention is a strong positive signal that a sense is *needed*.

⚠️ Both figures assume the measured conditionals transfer from the gold population to the live
one. That has **not** been checked independently, and it is the single biggest unvalidated
assumption in this note.

### Abstention is wildly uneven by language

| en | sv | de | fr | ru | no |
|---|---|---|---|---|---|
| **48%** (210 rows) | 43% | 17% | 15% | 12% | 11% |

Flat across choice-set size (32% / 42% / 37% at 2–3, 4–6, 7+ senses), so this is not harder
questions — it is **which languages Wikidata has under-sensed**. Practical: review the
non-English half first, ~85% usable against 52%.

---

## 2. What got built

Nothing writes to Wikidata by default; every lane is human-approved and `--dry-run` is the default.

- **Three lanes, partition-checked.** `gates.high_tier_where(n_senses=0|1|"poly"|None)`.
  `uv run gates.py` asserts every lane is non-empty and that mono + poly exactly partition the
  general MATCH form: **1,461 + 549 = 2,010**, no overlap, no gap.
- **`assign.py`** — the per-sense question in production, downstream of verification (the
  verifier's relation still drives scope hints and auto-defer). Cache `data/assign.sqlite3`.
- **The review UI shows the whole sense inventory**, chosen sense marked, already-linked senses
  flagged. This is the lane's entire safety story: the residual failure is a wrong *sense*,
  invisible if you show only the chosen one. en *captain* — 11 senses, 8 already linked — is the
  case in miniature.
- **`decisions.csv` gained `target_sid`**, frozen at approval time so a prompt change cannot
  rewrite which sense a human approved. 1,262 rows migrated, verified value-for-value.
  `append_decision` now **raises** on a header/`LOG_COLS` mismatch.
- **`write.py --include-poly`** — same edit class as the monosemous stream, differing only in
  which sense. Three live guards: sense still exists, doesn't already carry this anchor, carries
  no *other* P5137.
- **Duplicate-sense preflight** before creating 2+ senses on one senseless lexeme. Lazy: fires
  only on multi-sense CREATE groups. 0 of 8 real groups flagged.

### Two latent bugs, both reachable only once the poly lane existed

1. `build_edit` dispatched the sense-**creation** branch on the `--include-senseless` *flag*
   rather than the sense count, so any approved m≥2 row would have invented a new sense on a
   lexeme that already has some.
2. `append_decision` would silently write rows wider than the file header, sending the extra
   value into `DictReader`'s `None` key — on an append-only log, unrecoverable.

### Retired

- **The multi-candidate gold sheet** (`build_multi_gold.py` + its CSV). Wrong population (its
  pool reached below gates A and D — 28% multi-anchor against **2% of the live review queue**),
  wrong frequency (competition needed a human tie-break **once in 694 decided lexemes**), wrong
  frame (one-best-of-N assumes rivals for one slot; at 59% missing senses they are usually
  claimants on different *unrecorded* senses). Sense-keying plus abstention dissolved 25 of 29
  apparent competitions in the poly lane.
  **Its 14 adjudications were not salvageable** — of 30 `wrong` rows, 11 were never clicked
  (clicking `best` auto-filled the siblings) and 19 are the ambiguous all-wrong lexemes, leaving
  9 relative `best` picks. *Decompose before calling human labels precious.*
- **`gates._LEGACY_CLEAN`** and the equivalence half of `_check()` — a fossil pinning
  pre-refactor SQL into live code, replaced by the live invariants above.

---

## 3. What we learned about scale

This is the part that changed the plan.

| | pairs |
|---|---|
| gated pool, all lanes | 13,381 |
| — of which **senseless** | **11,371 (85%)** |
| mono | 1,461 |
| poly | 549 |

- **Auto-approval unlocks ~274 edits today** (~2,000 lifetime, all languages). Only the EQUAL
  class clears the pinned gate (≤2% rejected, ≥80% approved); poly contributes zero until it has
  ~30 human decisions of its own.
  ⚠️ **EQUAL clears at 99% but its sole-anchor subset fails at 98%.** Auto-*defer* hides a row if
  *either* class or refinement clears; auto-*approve* must require **both**. Same code, inverted
  rule — getting it backwards auto-approves the population measured as unsafe.
- **Volume and precision are in direct conflict.** Relaxing gates: drop D → 21,638; drop A →
  36,965; drop both → 63,275. But sole-anchor pairs (the closest proxy for a loosened
  population) run **18% rejected against 8%**, and the auto-approve gate allows 2%. Nine times
  the budget. You cannot loosen and auto-approve at the same time on current evidence.
- **All languages ≈ 4,500 mono pairs lifetime** (from 1,461), *estimate only*: yield varies
  **22×** across the six calibration languages and nothing we can observe predicts it — the
  binding constraint is how many monosemous, unlinked, unambiguous noun lexemes a language
  happens to have, which is down to which editors curated it. Russian: 102k lexemes → 21 pairs.
  French: 21k → 459. The exact number is one rebuild away (reload lexemes without the
  anchor-only restriction, re-run `match.py`): compute hours, no API spend.
- **Therefore the BRFA is a decision about the senseless lane, not the monosemous one.**
  Mono + poly tops out ~4,500 lifetime and needs no bot flag — those edits are assisted and
  human-confirmed today. The volume, and the licensing conversation, are both in sense creation.

---

## 4. Next steps

**1 — Post the talk-page draft.** `notes/talkpage_post.wiki` already asks the decisive question
("should I push <100 items/week as assisted writes?"). Cheapest information available, commits
to no task description, and gets `redo.md` §7's benefit — learn what the community requires
*before* building more — without a BRFA to outgrow.

**2 — Write the 45 approved edits** (40 senseless, 5 mono). Payloads build cleanly; no BRFA
needed. Three lanes are built and **only one has ever written** — this closes that gap and
exercises sense creation on-wiki for the first time.

**3 — Review the poly lane's 222 assigned rows, non-English first.** ~85% usable versus 52%.
Recording which rows a human rejects also yields the first sense-level human labels the project
has ever had — as a side effect of reviewing, not a labelling exercise.

**4 — The prompt + cache revision** (`redo.md` #3 + #4, "the one move I'd make"). No external
dependency, and it gets *more* expensive with every lane opened: the re-verify cost went from
2,670 to 3,220 pairs when poly landed.

### Deferred, with reasons

- **BRFA** — only when the senseless lane has a track record to file about (it has written zero
  edits). When written, **scope it at the ACTION**: "create a sense for a translation-attested
  meaning Wikidata does not yet record", not "senseless lexemes". The senseless lane is then its
  m=0 case, and `create_add` doesn't outgrow the request.
- **`create_add`** — creating a sense on a lexeme that already has senses. Not hypothetical: the
  125 poly abstentions are exactly this action, and P(sense really missing | abstained) ≈ 99%.
  Currently refused by `write.py`. Wait until (2) and (3) are done.
- **All-language expansion** — do the rebuild for a real number first. And note `redo.md` §7:
  every language added pre-BRFA multiplies the cost of a policy change the community forces.
- **Unifying the three lanes in code.** The generic unit is `(lid, sid|NEW, qid)` and mono is
  genuinely a special case — but the split is a *measurement* boundary (an established lane's
  calibration must not be restated over an unreviewed population), and collapsing it buys no
  capability without `create_add`.

### Known-unvalidated

- The two conditionals transferring from the gold population to the live one — the assumption
  under both the 59% and the 99%.
- **No human sense-level ground truth exists.** `decisions.csv` carries no sense-level label and
  the control arm is a same-model consistency check. Step 3 is the cheapest route to it.
- The 59% rests on one prompt, one model, six languages, and a pool already filtered by a
  lemma-level verifier that runs *without* a gloss on this lane — so it does no sense-level
  filtering at all, and the assigner carries that gate alone.
