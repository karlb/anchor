# Structured convention tag in the verifier reply — LANDED 2026-08-16

**Finding (2026-07-06 /simplify, altitude).** The gender/demonym convention keep was detected by
keyword-scraping the LLM's ≤10-word free-text reason: `review_queue._CONVENTION` (`" people"`,
`"female"`, …) matched inside `scope_kind`. The REL token was parsed structurally, but this second
signal hung on prose — a model paraphrase ("woman-specific form of …") silently flips keep→defer
per pair, and every prompt revision invisibly changes which pairs the convention fires on.

**Landed as three booleans, not one enum.** The proposed fix was
`"convention":"gender|demonym|none"`. Measuring the senseless lane showed that is not enough: a
*demonym* convention keep is only blessed when the anchor is a nationality, and contested when it
is an ethnic group, so the tag has to carry that distinction. The reply now asks for
`relation` plus `gendered_form` / `demonym` / `anchor_is_ethnic_group` —
`notes/redo.md` #3's exact shape — and `verify.compose_reason` serialises them into the existing
`reason` column as `<REL>·<tag>·<tag>: prose`. No schema migration, the column stays readable, and
`scope_kind` branches on the tags.

**It was a correctness fix, not a tidiness one.** The predicted failure ("a paraphrase flips the
action") turned out to have a systematic instance: in the senseless lane there is no target gloss,
so the demonym→ethnic-group downgrade could not fire, and the word "ethnic" in the model's prose
reached `_CONVENTION` instead — the *keep* list. The same token meant "contested, defer" with a
gloss and "blessed, keep" without one. Largest single error class in that lane
(`notes/senseless_lane_findings.md`).

**`_CONVENTION` is NOT deleted**, and shouldn't be: the verdict cache is additive, so every
pre-2026-08-16 row is still there in its prose shape and is still graded against by
`validate_prompt --baseline-ver`. `scope_kind` keeps the legacy path verbatim for rows whose
reason carries no tag. It can go once no consumer reads a pre-structured prompt version.

**Cost note.** The old "why deferred" reasoning — a `verify.SYSTEM` edit invalidates the whole
cache — no longer holds: the cache keeps every version. What a prompt edit still costs is that
consumers read only the *current* version, so the pool needs re-verifying at the new prompt before
the live queue is whole again (~$0.0095/pair).
