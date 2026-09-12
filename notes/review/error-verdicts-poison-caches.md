# Transient LLM failures are cached as done (verify / gender / ab_concept)

**Finding (2026-07-06 /review, CONFIRMED).** All three resumable caches write
`error`/`parse_error` rows on batch failure, and every done-set checked row presence only —
so one timeout or malformed reply permanently removed those pairs, with no recovery short of
`--force` (a full paid re-verify) or, for gender, manual SQL. In `ab_concept` the cached
failures were worse than lost: `predicted_action` maps them to `defer`, so an infrastructure
failure entered the A/B metric as if it were a verdict.

## Cheap layer — DONE 2026-08-16

Landed together with the additive verdict cache, since a long paid measurement run was about
to be the first thing to hit it:

- `verify.py` — done-set excludes `error`/`parse_error` (and now also keys on prompt version).
- `gender.py` — done-set excludes them too.
- `ab_concept.load_cache` — skips failure entries instead of loading and scoring them.
- `llm.py` — the fence strip no longer raises `IndexError` on a one-line ```` ```[...]``` ````
  reply, which used to become a permanent error row in most callers and killed an ab_concept
  arm outright.

Extended 2026-08-16 to the two places a failure was still counted as a *result* rather than
merely cached: `ab_concept.run_arm` scored in-run error verdicts as `defer` votes (an outage
moving the metric the arm measures), and `audit_decisions.gloss_audit` dropped failed batches
while reporting every queued row as audited — "N links audited, 0 flagged" was the most
reassuring possible way for that tool to fail.

## Proper layer — STILL OPEN

The root cause is unaddressed: the batch-LLM loop is copied five times (verify, gender,
measure_lift, audit_decisions, ab_concept) and only `ab_concept` has quota-abort
(`QUOTA_MARKERS` → `QuotaAbort`, pre-cache) and timeout retry. **A quota stall mid-run in
verify or gender still converts the rest of the budget into error rows** — they are retried now
rather than permanent, and no longer scored, but the run still burns to the end writing failures
instead of stopping.

**Fix.** One shared batch driver in `llm.py` — typed quota error classified at the source
instead of ab_concept's string grep, timeout retry, fill-error-and-continue — that all five
callers use.

**Trigger.** Next time any of the five loops needs touching anyway. Bring it forward if a run
actually stalls on quota: that is the failure this layer prevents and the cheap layer doesn't.
