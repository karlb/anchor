# Latent mechanisms (2026-07-06 /review, PLAUSIBLE) — real code paths, no live trigger

Verified-real mechanisms whose triggering data doesn't exist today. Cheap guards;
each can ride along the next touch of its file.

**1. Blank target_n_senses aborts write.py.** write.py does `int(d["target_n_senses"])` over all
replayed approved rows, unguarded. Every current decisions.csv row is populated, but a future
hand-appended approved/recheck row with a blank snapshot column → ValueError before any edit is
built. (Still live as of 2026-08-16, and the log has since grown a `target_sid` column, so
hand-appending is now slightly likelier.)
*Guard:* skip-with-warning (or default) on blank.

**2. anchor_cc0 misses the nb/no collapse.** `anchor_cc0` queries item_lang with the exact
target lang; the de/en/sv fallback excludes 'no', and Wikidata stores Norwegian overwhelmingly
under 'nb'. Fires on the first target_lang='no' senseless candidate: the created sense would
silently lose its target-language gloss. Still latent as of 2026-08-16 — the senseless pool has
since gained a review language, but not that one, so this is one lexeme-load away from firing.
*Fix:* route anchor_cc0 through config.resolve_concept_langs like anchor_langs
already does.

**3. demonym_context: sid-blind gloss load, unchunked IN clauses.**
review_queue.py:524-531 selects sense_gloss ignoring sid (last row per (lid, lang)
wins — wrong-sense gloss if a polysemous lid ever enters; currently impossible
in-build since pool/decisions are all n_senses∈{0,1} from the same DB, residual risk
is only a cross-build desync) and builds IN clauses over all lids/qids with no
500-chunking (1,148 vars today; limit 32766 on this SQLite, 999 on <3.32).
*Fix:* reuse verify.load_glosses (sid-aware, monosemous-only, chunked) instead of the
inline SELECT.

**4. Retry-After date form crashes the fetch retry handler.** fetch_items.py:87-89
parses Retry-After with float(); the RFC also allows an HTTP-date, which would raise
ValueError inside the except handler and abort the run (resumable, but dead). No
evidence the WMF edge emits the date form.
*Guard:* try/except around the parse, falling back to the computed backoff.

---

Checked and REFUTED by live API tests during the same review (no action, recorded so
future reviews don't re-litigate): wbgetentities keys redirects under the REQUESTED
id with the target's data, so redirect anchors are not cached missing=1
(fetch_items.py:165); empty label/claim maps arrive as `{}` even on formatversion=1,
so parse_entity's dict assumption holds (fetch_items.py:106); batch-level API
failures raise in http_get before any fetched-markers are written, so
item_lang_fetched can't be poisoned by a failed batch (fetch_anchor_langs.py:108).
