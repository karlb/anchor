# The verifier sees the sense inventory: one call answers relation AND sense

*Measured 2026-09-03. Numbers FROZEN to that run; for live counts run `review_queue.py status`
per lane.*

## What changed

Every lexeme that has senses is shown its numbered inventory — `senses: (1) …; (2) …` — a
monosemous lexeme as a one-item list, a senseless one still as the bare lemma. The structured
reply gained `"sense"`: a number, `"none"`, or null. `verify.parse_sense` resolves it into a new
cache column `verdict.target_sid` (S-id / `none` / NULL). Prompt `d8769650`; predecessor
`32e24a6f`; re-verify of the gated pool 2,638 pairs, ~$45 including two restarts.

**The rule that mattered:** after `none` the verdict is about the NOUN. A meaning the noun has
that no listed sense records is still *yes* (EQUAL) — the sense is MISSING, not different; only a
homograph or unrelated concept is DIFFERENT. Without that sentence the first draft answered
`DIFFERENT / no: "… sense not listed"` on register and passage, i.e. it threw away exactly the
population the create action exists for.

`compose_reason` tags those rows `·missing-sense`; `scope_kind → rel_class` makes them the
`MISSING` class with `REL_ACTION = create`. The action rides in the reason head because that is
what every policy consumer reads (queue sort, `status` precision, calibrate, auto-defer) — so a
missing sense is never counted as a keep. `assign.py` is superseded in the queue and kept as the
measured instrument the verifier's picks are graded against.

This is the "generalize by ACTION, not sense count" design (`sense_assignment_findings.md`)
landing in the verifier: S-id = match that sense, `none` = create a sense, NULL with no senses
= create the first. The lanes remain as gate/UI/measurement strata.

## Is it better? Four graders, $0 beyond the re-verify

Head-to-head where the two prompts' triage actions differ (each row names a winner):

| gold | n differ | old right | new right |
|---|---|---|---|
| 146 disagreeing pairs, blind (`disagree_sample.csv`) | 69 | 24 | **45** |
| 240 senseless pairs, blind (`senseless_sample.csv`) | 18 | 5 | **13** |
| `decisions.csv` (confounded toward the old prompt) | 68 | 24 | **44** |
| 119-case curated gold, 69 scorable | 15 | 4 | 1 |

The 119-gold's "4" is three `create` answers (plantering, shipment, Rückkehr) that its acceptable
sets, written before the action existed, cannot score, plus one genuine loss (retiree,
LATERAL→EQUAL); the "1" is wise guy. Read as a tie. Keep-precision on the confounded log: 88.5%
vs 89.7% (433 keeps / 383 right vs 409 / 367 — more keeps, 16 more right, 8 more wrong). On the
50 human-rejected pairs in scope the old prompt would write 8, the new 10, and the new says
`create` on 4. `validate_prompt` FAILs its 50% catch line at 41.7%, as the predecessor would
have on the same rows; the catch gate is starved (`notes/review/reject-gate-starved.md`).

Per lane on the 146 blind set: mono 14 vs 11, poly 23 vs 7, senseless 8 vs 6.

**Verdict: keep `d8769650`.** Better on both blind sets and on the log that favours the
incumbent; the one flat result is the gold that cannot score the new action.

## The sense pick, graded against assign.py (347 poly pairs both answered)

| | n |
|---|---|
| same sense | 203 |
| both `none` | 83 |
| verifier picks, assign abstained | 42 — 40 of them triage to *defer* (scope relation), 2 keep |
| verifier `none`, assign picks | 11 — all verdict *no* (DIFFERENT), i.e. rejections, not create claims |
| different sense | 8 |

82% agreement with the instrument measured at 93–95% pick / 60:1 abstention; the disagreements
are almost all on the hold side. The 8 different-sense rows are the class to watch as decisions
land.

## What it opens

Verifier-'yes' rows by action, live pool at the freeze:

| lane | keep | defer | **create** |
|---|---|---|---|
| mono | 816 | 239 | **104** |
| poly | 144 | 118 | **51** |
| senseless | 531 | 50 | — |

104 monosemous lexemes whose one recorded sense is not this meaning were invisible before
(DIFFERENT / no). They now reach the queue with a `+ create sense` button and the gloss the new
sense would get. Nothing writes from `create` until sense creation is disclosed; the decisions
accumulate in the append-only log meanwhile.

Drift for reference: verdict flips 201/2,638 = 7.6%; triage action changed on 254/1,926
both-'yes' pairs = 13.2% — larger than the previous prompt change because the sense answer is a
new axis, not noise on the old one.
