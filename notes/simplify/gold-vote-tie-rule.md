# One majority-vote helper for the gold harnesses (tie rule)

**Finding (2026-07-06 /simplify, reuse).** score_approaches.py and
build_problematic_cases.py both replay `ab.load_cache()` into a
`defaultdict(Counter)` majority vote (same three-line dedup comment pasted in both) —
and the copy-paste has forked on tie handling: score_approaches' `resolve()` sends a
tied vote to **defer** ("a split verifier belongs in human review"), while
build_problematic_cases takes `most_common(1)` (arbitrary on a tie).

**Why deferred.** The gold's clean-rebuild reproducibility is pinned (a rebuild must
reproduce all 119 cases, memory `problematic-cases-gold`); changing the rebuild's vote
rule could churn the emitted base/multi columns.

**First step (cheap, settles it).** Scan `ab.load_cache()` for any (arm, gold-pair)
whose vote counter actually ties. If none ties — likely, with samples=3 — extracting
one `vote_actions()` helper into ab_concept.py with ties→defer is a provable no-op;
do it. If some tie, decide the rule once (ties→defer is the documented, principled
one), regenerate, and eyeball the CSV diff — only the informational base/multi columns
can move; PROPOSED (the actual gold) is untouched by construction.
