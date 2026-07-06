# Structured convention tag in the verifier reply

**Finding (2026-07-06 /simplify, altitude).** The gender/demonym convention keep is
detected by keyword-scraping the LLM's ≤10-word free-text reason:
`review_queue._CONVENTION` (`" people"`, `"female"`, …) matched inside `scope_kind`.
The REL token is parsed structurally, but this second signal hangs on prose — a model
paraphrase ("woman-specific form of …") silently flips keep→defer per pair, and every
prompt revision invisibly changes which pairs the convention fires on.

**Fix.** The reply is already controlled JSON. Extend the schema in `verify.SYSTEM` to
`{"i":…, "verdict":…, "reason":"<REL>: …", "convention":"gender|demonym|none"}` and
branch on the token in `scope_kind`; delete `_CONVENTION`.

**Why deferred.** Any edit to `verify.SYSTEM` changes `PROMPT_VER`, which invalidates
the whole verify cache → a paid full re-verify plus the usual gold revalidation
(controlled 2×2 + multi-sample vote, per `rigorous-method-retesting`).

**Trigger.** Fold into the next prompt revision that happens anyway — the cache cost
is already being paid then, so the tag rides along for free.
