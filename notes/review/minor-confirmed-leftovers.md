# Minor confirmed findings (2026-07-06 /review) — grouped

Four CONFIRMED-but-small findings, each fixable independently; grouped here because
none justifies its own file.

**1. run_arm tie rule forks from vote_action.** ab_concept.py:169 uses
`c.most_common(1)[0][0]` (tie → insertion order) while `vote_action` — created to
single-source the tie rule — maps ties to defer; the docstring admits the fork. On
any tied vote the live A/B and the gold harnesses score identical cached verdicts
differently. Latent (no ties in today's cache).
*Fix:* `return {k: vote_action(c) for k, c in votes.items()}`; delete the docstring
caveat. Fold into the next ab_concept touch.

**2. Nondeterministic lemma pick in the candidate table.** match.py:104 GROUPs BY
(target_lid, anchor_qid) selecting bare `target_lemma` alongside three aggregates, so
SQLite picks an arbitrary group row — 62 live multi-lemma groups (center/centre,
offence/offense). The pick feeds the verifier prompt (`noun="{lemma}"`), the
alias_match hint (exact string match can flip across rebuilds), undifferentiated_gloss,
and the append-only decisions.csv snapshot. (The homograph-count-mismatch half of the
original finding is moot: diverging groups have max(homograph_n_lid)≥2 and are gated
out of every consumer by clean_clause.)
*Fix:* deterministic pick, e.g. `min(target_lemma)` in the SELECT. Note it changes
prompt content for the affected pairs without a PROMPT_VER bump — harmless for
already-cached verdicts, but do it before a full rebuild rather than mid-cache.

**3. precision coverage denominator excludes sole-anchor lexemes.** precision.py:70-71
`if len(members) < 2: continue` yields no record, despite the docstring counting "L is
the only anchor" as a no-coverage outcome; the printed `no_anchor` row is effectively
dead and coverage is inflated.
*Fix:* yield a `no_anchor` record for single-member clusters (or relabel the stat to
"coverage among multi-anchor clusters"). Re-run before quoting coverage anywhere.

**4. `--preview --include-senseless` previews the wrong population.** write.py:117-118
switches the preview to pool(senseless=True) ONLY (n_senses=0 rows), while a real
`--write --include-senseless` run processes monosemous AND senseless approved rows
(the flag's help text says "ALSO"). Sizing `--limit` off the preview count budgets
against the wrong stream.
*Fix:* preview both pools when the flag is set (concat pool(False) + pool(True)), or
split the lane into an explicit `--preview-senseless`.
