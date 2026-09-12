# Gloss-ladder fork: verifier sees evidence the reviewer never does

**Finding (2026-07-06 /review, CONFIRMED — live triggers).** The language-preference
ladder exists twice and the copies disagree on the last rung: `verify.pick_gloss`
(verify.py:295-301) falls back to a gloss in ANY language
(`next(iter(vals.values()), "")`), while `review_queue.pick` (:200-206) returns
nothing. So the LLM verdict can rest on text the review UI renders as "(no sense
gloss)". Live High-tier examples: L1453756 fr "fragilité" (only gloss is Bengali),
L1529522 sv "edsvuren" (Finnish), L294911 sv "besöksförbud" (Polish), L304186 no
"kubaner" (gloss keyed `nb` — outside BOTH ladders for target 'no'). The SYSTEM
prompt partially anticipates odd-language glosses, but the human can't audit what the
verdict rested on.

**Fix.** Restore evidence parity in the cheap direction: make the UI side show the
same any-language gloss, tagged with its lang code — i.e. one shared ladder function
with the fallback, `pick` delegating to it. Changing `pick_gloss` instead (dropping
the fallback) would silently change prompt CONTENT without bumping PROMPT_VER (the
SHA1 covers SYSTEM only), leaving the verify cache inconsistent — avoid that
direction, or treat it as a prompt revision with full re-verify + gold recheck (per
aliases-hurt-validation: verifier-evidence changes need gold validation).

While there: the 'no' ladder should include 'nb' (resolve_concept_langs already
encodes the collapse; the kubaner case shows both ladders miss it).

**Trigger.** UI-parity fix any time (no LLM cost, no cache impact). Any
verifier-side ladder change: fold into the next prompt revision.
