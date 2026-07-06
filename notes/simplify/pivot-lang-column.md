# pivot_lang as a candidate_raw column

**Finding (2026-07-06 /simplify, altitude).** The polysemy-collapse hint recovers the
source headword's language by string-parsing the lexentry prefix through a private
ISO-639-3→2 map (`review_queue._LANG3TO2`, used in `polysemy_hint`). The language is
structured data the pipeline already has: `cluster_member.lang` is right there in
match.py's build join. Cost: a future review language whose 3-letter code isn't in the
map makes the hint silently never fire for it (no error), and there are two language
tables (`load_lexemes.LANG_QID`, `_LANG3TO2`) to keep in sync.

**Fix.** Emit `pivot_lang` (from `cm.lang`) as a column of `candidate_raw` in match.py's
BUILD script, aggregate it in `polysemy_index`'s GROUP BY, delete `_LANG3TO2` and the
prefix parsing.

**Why deferred.** Needs a `candidates.sqlite3` rebuild; the current DB lacks the column,
so shipping the read-side first would break the running queue.

**Trigger.** The next `match.py` rebuild (any candidate-pool refresh). Do both sides in
one commit; no migration needed since the DB is regenerable.
