Ideas on axes the plans don't cover — things that would change what the project *is*, not just how much it writes. Written 2026-07-06, ranked roughly by usefulness-per-effort; status updated 2026-08-16.

**1. Community-scale the human gate: deploy the review queue as a public tool.** ✅ **BUILT** — this is now `notes/crowd_review_plan.md`, a live Distributed Game with scored voters and crowd-credited promotions. Recorded here because the *argument* still holds and still isn't fully cashed in: the bottleneck was that Karl is the only reviewer, and every crowd-confirmed edit is an assisted edit needing no bot flag, which dissolves much of the licensing/BRFA anxiety. The remaining work is phase 3 (does crowd accuracy justify dropping the per-batch sign-off?), not the tool.

**2. Close the loop into WikDict itself.**
You maintain WikDict — and this pipeline's intermediate artifact (sense clusters pinned to Q-items) is exactly what a dictionary wants. Three concrete payoffs: (a) WikDict entries gain a concept link → images (P18), taxonomy, and Wikipedia links via the Q-item, per *sense*; (b) new translation pairs for language pairs WikDict can't currently infer — once two senses in different languages share a P5137 anchor, you get a translation edge for *any* pair among the ~26 languages, including pairs with no direct Wiktionary data (this is the "translation as shared reference" model already in your notes, run in reverse); (c) the Q-item becomes a stable cross-release sense identifier, which WikDict currently lacks. This multiplies the value of every P5137 you write, and none of it needs Wikidata's permission.

**3. Ship the disagreement stream to Wikidata's Mismatch Finder.**
The pipeline already catches wrong existing P5137s as a side effect (Schützin → "alcoholic mixed drink", Habicht → hawk-vs-goshawk). Wikidata runs a purpose-built platform for exactly this: external datasets upload suspected mismatches, and the community adjudicates in their UI — you need no write access, no BRFA, no review lane of your own. A one-time exporter from the `conflicting_anchors` + verifier-'no'-on-existing-link slices would make the project useful to Wikidata *tomorrow*, and "they found real errors before writing anything" is the best possible credibility asset for a bot request. Earlier notes parked this as a phase-2 audit *product* to build ourselves; Mismatch Finder means we don't have to.

**4. Make the pipeline an explicit flywheel.**
Every written P5137 is a new *anchor*, and anchors are the scarce input — so writes unlock candidates that didn't exist in the previous pass, in every cluster the newly-anchored sense touches (including for languages you'll never review yourself, once idea 1 exists). Right now the pipeline is architecturally one-shot. A thin "iteration" wrapper — new dump → rebuild → diff against decided pairs → report the net-new wave — turns 6,700 senseless writes into the seed of the next 10,000 candidates rather than an endpoint. Cheap to build (it's mostly what you already run by hand, plus the diff), and it changes the project's ceiling from "the current candidate table" to "fixpoint of the lexeme graph."

**5. Add PanLex as a second, CC0 evidence source.**
PanLex is meaning-mediated (translations grouped by meaning, not just word pairs), covers thousands of language varieties, and is CC0 — so every edge it adds strengthens support counts and cross-checks *without* touching the DBnary/sense_num licensing worry at all. Where WikDict and PanLex agree, you'd get much stronger gate-A support; a PanLex-only corroboration could even become a distinct structured hint in review. Real caveats: PanLex's meaning granularity is coarser and quality varies by source, so it should enter as *additional evidence* on existing clusters first, never as the cluster key. Worth a sizing spike before committing.

**6. Revive the non-noun "dead end" via P5972 instead of P5137.**
The verbs/adjectives negative result was specifically about P5137 *anchors* being scarce (~145 verb anchors). But Wikidata's own model says non-nouns translate via **P5972 (translation)** sense-to-sense, no concept item needed — and your clusters already contain exactly those sense-grounded verb/adjective pairs. That would open the majority of WikDict's content that the current design structurally excludes. It's a genuinely different edit class with its own licensing analysis (a direct translation claim is closer to the source data than an inferred concept link), so it's a phase-4 conversation — but it's the only idea here that grows the project's *addressable domain* rather than its throughput.

If I had to pick one: **#1**, with **#3 as the quick win beside it** — #3 is days of work and buys community goodwill; #1 removes the constraint every other plan in `notes/` is quietly shaped around. #2 is the sleeper: it's the only idea where the payoff lands in a project you fully control. *(#1 has since been built; #3 is still the standing quick win, and it is worth more now than when this was written — "we found real errors before writing anything" is exactly the credibility a sense-creation request will need.)*

## Small open items

Kept from a 2026-06 evaluation whose other recommendations have all shipped:

- **Stamp the item revision + retrieval time** when caching CC0 labels. They are mutable, and the verifier judged a specific revision — a genuine provenance-accuracy fix, cheap.
- **Verdict-stability flip rate.** Re-verify a slice of cached 'yes' rows and report how many flip. It quantifies the LLM non-determinism that the drift between two independently drawn measurement samples hinted at, and it is the honest denominator behind every precision figure we quote.
- **`sense_ref` may be dead weight.** It was extracted to de-bias precision against existing provenance; that turned out to be impossible (below). Print the live count once and consider dropping the table.

## Settled — do not re-propose

Negative results that cost real work to establish:

- **Provenance de-bias of the precision baseline is empirically dead.** Almost no existing P5137 statement carries a reference at all, and essentially none carries P887 — there is no signal to de-bias with, and no amount of work changes that.
- **Verbs and adjectives are a dead end for P5137.** There are too few verb/adjective anchors to propagate from, across all pilot languages. (Idea #6 above is the *only* live version of this: a different property, P5972, not a bigger P5137 push.)
- **Don't pre-filter anchors that are typed as languages** to catch the feminine-demonym error class. Measured: held-out language anchors are *higher* precision than average, because they are legitimate language-name nouns. The distinction is semantic — only the verifier can make it. (Full argument in `p5137_anchor_propagation.md`.)
- **Cross-script / NFC normalization guards** — speculative tooling for languages the pipeline doesn't enable, and NFC-on-join would perturb current candidates. Exact-string matching is self-protecting across scripts.
- **Don't fold the scope filter into the verifier prompt.** It re-litigates the documented split — the verifier judges denotation, the gate judges scope — and would invalidate the cache to remove a handful of QIDs.
