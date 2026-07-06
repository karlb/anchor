# Align the harness gate D with the live scope exclusions

**Finding (2026-07-06 /simplify, altitude).** Gate D means two things: the live SQL gate
(`gates.concrete_anchor_clause`) is "has a P31 **and** not in EXCLUDE_P31/P279", while
the measurement harnesses (`precision.typed_qids`, used by measure_lift + ablation) test
has-P31 only. So the published High-tier lift (~95.9%) was measured on a slightly wider
tier than the one verify/review/write actually operate on, and any future change to the
exclusion lists silently widens the gap. The difference is now documented in
`precision.typed_qids`'s docstring, but not closed.

**Fix.** Subtract the excluded qids in `typed_qids`: build the excluded set from
`gates.EXCLUDE_P31`/`EXCLUDE_P279` against `item_type` (one query), same `it` attach.

**Why deferred.** It changes measured numbers mid-stream — a silent redefinition would
make old and new lift figures incomparable.

**Trigger / first step.** `ablation.py` is LLM-free: run it once with the
exclusion-aware D and report the delta at zero cost. If the delta matters, re-run
`measure_lift` under the new definition before citing the number again (e.g. in the
BRFA), stating both definitions once for continuity.
