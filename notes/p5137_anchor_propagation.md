# P5137 anchor-propagation: method and pilot results

*WikDict/DBnary → Wikidata lexeme sense-linking (de/en/sv pilot). Measured 2026-06-20/25
against the weekly `latest-lexemes.json.gz` dump and `infer.sqlite3`.*

> **How to read this document.** The **method**, the **license boundary**, the **negative
> results**, and the **reviewer guidance** below are current and load-bearing — the reviewer
> section is the project's working rubric.
>
> Every **number** is a frozen measurement of the *original* de/en/sv, monosemous-only,
> few-language corpus. Do not quote them as current: clusters and anchors were since rebuilt
> over all WikDict languages, three more review languages were added, and two further lanes
> (sense creation, per-sense assignment) opened. For live counts run the tools (see the README);
> for what the later measurements changed, read `sense_assignment_findings.md`.

## Problem

Wikidata's lexeme namespace has many word-forms but few *meanings*: only a minority of
lexemes carry a sense, and **P5137 ("item for this sense")** — linking a lexeme sense to a
Q-item — is the only viable concept-linking route. Its precision had never been measured.
Separately, Wikidata lacks translation data because Wiktionary's CC-BY-SA tables can't be
relicensed into a CC0 namespace.

## Method: anchor propagation

Take a WikDict translation cluster, find a member lexeme that is **already** grounded to a
Q-item via P5137, and propagate that *known* concept to the other members. The LLM never
invents a concept — it only answers a constrained yes/no, checked against the anchor Q's
**own CC0 label + description**. Grounding both sides to the same Q simultaneously fixes the
senseless-lexeme problem and creates the translation link.

**The #1 risk is polysemy stitching**: a lemma triangle can connect *different* senses of a
polysemous word, so an anchor propagated across such a pseudo-cluster is confidently wrong.
Mitigation: clusters are grouped on the structural key `(lexentry, sense_num)` — a single
pivot sense — never on the gloss text. (This also keeps us license-clean: the CC-BY-SA gloss
is never read.)

## License boundary (CC0-clean)

- **Verification target = the anchor Q's own CC0 label/description** (Wikidata→Wikidata). The
  CC-BY-SA DBnary gloss is never read by the verifier or written to Wikidata.
- DBnary contributes only **structure** (which lemmas cluster — facts) and **lemmas** (facts).
- Output is a single bare P5137 link with a `based on heuristic (P887)` reference.

## Pipeline

```
infer.sqlite3 ──build_clusters.py──▶ clusters.sqlite3 ─┐
                                                       ├─match.py──▶ candidates.sqlite3 ─┬─ precision.py (held-out)
latest-lexemes.json.gz ─load_lexemes.py──▶ wikidata.sqlite3 ─┘                          ├─ fetch_items.py ─▶ wd_items.sqlite3
                                                                                        ├─ verify.py ─▶ verify.sqlite3
                                                                                        └─ measure_lift.py
```

## Results (de/en/sv, 2026-06-20)

### Corpus
- **371,089** de/en/sv lexemes (de 241,931 · en 73,780 · sv 55,378); 262,966 nouns.
- **65,184** senses carry P5137; **56,617** of those on noun lexemes (the anchor universe).

### Candidates (match.py)
- **70,470** clusters with a single dominant anchor.
- **41,601** candidate (target lexeme, anchor Q) pairs → **34,821 clean** (unambiguous match,
  no cross-pivot conflict), of which **31,260 ground a currently-senseless lexeme** (the
  safest, highest-value writes). Clean by language: de 24,302 · sv 7,011 · en 3,508.
- Abstains: 15,243 conflicting-anchor clusters, 26,069 no-anchor, 6,309 cross-pivot conflicts.
- Runtime 6 s (was 85 min before indexing the intermediate tables).

### Structural precision (precision.py — held-out leave-one-out)
For each lexeme that already has P5137, hide its Q, recompute the anchor from the *other*
cluster members, check recovery. 55,495 decisions (75.2% coverage; the rest abstain).

| Slice | Precision | n |
|---|---|---|
| **Overall (strict)** | **61.8%** | 55,495 |
| ≥2 agreeing anchors | **81.9%** | 15,235 |
| Single anchor | 54.2% | 40,260 |
| Monosemous target | 63.6% | 51,074 |
| Polysemous target | 40.6% | 4,421 |
| de / en / sv | 64.8 / 54.2 / 67.7% | — |
| Write-unit (deduped) | 49.3% | 27,820 |

**P279 tolerance** (crediting near-misses via the P31/P279 graph): exact 61.8% → +parent/child
66.2% → +sibling 70.9%, leaving **16,136 (29%) genuine misses**. So even generous structural
tolerance leaves a large real-error rate — the LLM verifier is load-bearing, not optional.

### Structural gates (ablation.py — held-out, what we can win before the LLM)
Gates: **A** ≥2 agreeing anchors · **B** monosemous target · **C** same-label/cognate ·
**D** concrete anchor (predicted Q has a P31 type).

| Gate | Precision (full truth) | Precision (non-cognate, unbiased) |
|---|---|---|
| baseline | 61.8% | 54.3% |
| A | 81.9% | 77.1% |
| D | 66.4% | 59.7% |
| A & D | 84.2% | 80.1% |
| A & B & D | — | **80.9%** |
| C | 92.4% | (degenerate) |
| A & C | 95.9% | (degenerate) |

**De-bias:** provenance is absent (only 48/65,184 P5137 carry any reference, none P887), so
label-matched edits can't be filtered out. **82.7%** of all held-out truth is *cognate*
(lemma == true Q's label), so gate C's 92–96% is circular and the honest test bed is the 16%
**non-cognate** subset. There, structure honestly caps at **~81%** (A&B&D). A separate test of
the **reciprocity / `backlink_score`** lever found *no* signal beyond gate A (A=77.1% vs
A&backlink≥0.3=77.9% at half the volume) — the ≥2-anchor gate already captures cluster
robustness. **Conclusion: all five structural levers exhausted; structure alone caps ~81%, so
a trustworthy ≥95% requires the LLM verifier**, which independently checks cognate correctness
from the description (catching false friends the same-label gate waves through).

> **Note (2026-06-25):** the three subsections below are the **original lemma-only verifier**
> (anchor CC0 label/description only). The current verifier also reads the target lexeme's own
> CC0 Wikidata gloss and uses a tolerant EQUAL/BROADER/NARROWER→yes criterion — see the
> **"Gloss·tolerant verifier"** subsection below for the updated numbers.

### Verifier (verify.py — CC0 label/description, single-vote, `claude -p` haiku)
On a 48-candidate senseless-target sample: **36 yes / 9 no / 3 uncertain** (~25% pruned). The
rejections were correct, including the two *highest-support* candidates `Schützin@de` /
`skytt@sv` → Q1649489, whose label is "shooter" but whose description is "alcoholic mixed
drink" — a wrong anchor the structural method alone would have written. Other catches:
`Partie@de` → "political party" (false friend; *Partei* is the party), `förlust@sv` →
"casualty", `röra@sv` → "touching".

### Verifier lift (measure_lift.py — held-out ground truth, n=120, single-vote haiku)

| | Precision | |
|---|---|---|
| Write everything (baseline) | 63.3% | 76/120 |
| **Write only verifier-"yes"** | **82.0%** | 73/89 |
| True hits kept (recall) | 96.1% | 73/76 |
| Verifier-"no" hit-rate | 7.4% | 2/27 → 93% of rejects correct |

**The verifier raises precision 63% → 82% while discarding only ~4% of genuine matches, and
its rejections are right 93% of the time.** (Small-n caveat: ±~8% at n=120.)

### ≥95% "High" tier (measure_lift.py --gate ABD, sonnet)
The plan's High tier = the cleanest structural tier (**A** ≥2 anchors ∩ **B** monosemous ∩
**D** concrete/P31) + a *good*-model verifier-"yes". Measured on the 10,323-candidate A&B&D pool,
two independent draws:

| | n=120 | n=350 | pooled (n=470) |
|---|---|---|---|
| Write-everything baseline | 85.8% | 86.0% | — |
| **Write-only verifier-"yes"** | 96.9% (95/98) | **95.6% (283/296)** | **95.9% (378/394)** |
| Recall (hits kept) | 92.2% | 94.0% | — |
| Verifier-"no" hit-rate | 21.4% | 26.5% | — |

**The High tier meets ≥95% as a point estimate (pooled 95.9%) but hugs the threshold.** The
n=120 96.9% was a lucky draw; at n=350 it regresses to 95.6%. Pooled Wilson 95% CI ≈
**[93.5%, 97.5%]** — the lower bound is still ~93.5%, and at a true rate this close to 95% no
feasible n lifts the whole interval above 95%. Caveats both ways: (1) strict held-out truth
*undercounts* — some "misses" are near-synonym Q-items (steam/vapor) P279-tolerance would
credit, so 95.6% strict is a floor; (2) recall held at 94% at scale (verifier isn't
over-pruning). To clear 95% comfortably: **2-of-3 voting** (precision↑, recall↓) or rely on
the per-edit human review queue (the pre-BRFA edits are approved by hand anyway).

### Gloss·tolerant verifier (2026-06-25)
The verifier now also reads the **target lexeme's own CC0 Wikidata sense gloss** (monosemous lexemes
only) and judges with a **tolerant** criterion: EQUAL/BROADER/NARROWER → yes, only a *genuinely
different concept* (homograph / wrong sense) → no, falling back to the bare lemma when the gloss is
missing/terse/foreign. It targets the sense-mismatch class the lemma-only verifier is structurally
blind to: a monosemous Wikidata lexeme whose one recorded sense is the word's *dominant* meaning,
bridged onto a cluster built on a *different* sense (`sheet`=paper vs the nautical line; `hov`=royal
court vs hoof).

**Why it wins** — blind 2×2 (input × criterion), 3-sample majority vote, judges blind to the human
call, on the cleaned human-gold set. Holding the criterion *tolerant*, adding the gloss takes catch
from 0 → all 4 genuine homographs at ~100% keep; the earlier "gloss hurts keep" result was a
*strict-EQUAL* criterion artifact, not gloss noise (glosses are high quality: 140/171 RICH, 0 empty).

| arm | keep% | catch |
|---|---|---|
| lemma·strict (old verifier) | 94.2 | 2/8 (scope only; misses all 4 homographs) |
| lemma·tolerant | 99.4 | 0/8 |
| gloss·strict | 93.6 | 6/8 |
| **gloss·tolerant** | **100.0** | **4/4 genuine homographs** (spares the 2 scope + 2 mistaken rejects) |

**Production validation after the `--force` cache rebuild (sonnet, 669 High-tier candidates):**
- verdict mix shifted lemma-strict **362 yes / 300 no** → gloss-tolerant **470 yes / 198 no / 1
  unc** (tolerant now counts broader/narrower; the 22 old-`yes`→`no` flips are the homograph catches).
- human-gold set: **keep 169/172 = 98.3%**, **catch 4/6 = 66.7%** (the 4 genuine homographs; it
  correctly spares the 2 defensible scope rejects `retiree`/`arvtagerska`). `validate_prompt` **PASS**.
- held-out strict-Q lift (`measure_lift`, n=350): **86.0% → 93.8% at 100% recall**. The 93.8% is a
  *floor* — the tolerant criterion deliberately accepts broader/narrower near-synonyms that exact-Q
  held-out truth scores as misses, so the human-reviewed 98.3% keep is the truer precision signal.

Robustness: the verdict cache now stores a **prompt fingerprint** (`prompt_ver` = sha1(SYSTEM)[:8]);
a prompt change auto-invalidates stale verdicts instead of silently trusting them (`--force` still
forces a full re-verify). Full investigation in project memory `sense-gloss-matching`.

### Writable volume (how many statements this generates)
The held-out 689 is only the *monosemous* slice (the part with ground truth to measure). The
real writable High tier is larger because most candidates ground **senseless** lexemes — which
have no existing P5137, so they can't appear in the held-out measurement but are the
highest-value writes (they create a lexeme's first meaning).

| Tier (cumulative, all clean) | Candidates | of which senseless (new sense) | ~precision |
|---|---|---|---|
| All clean | 34,821 | 31,260 | ~62% |
| + ≥2 anchors (A) | 12,259 | 11,089 | ~82% |
| **+ concrete anchor (A&D)** | **7,400** | **6,679** | ~86% structural |
| + has CC0 evidence | 7,400 | 6,679 | (all 7,400 have evidence) |

**High tier ≈ 7,400 candidate statements** (~6,700 creating a new sense). Caveats: (1) the
95.9% precision is measured only on the 689 monosemous targets; the 6,679 senseless writes
*inherit* it as an assumption the ~10-edit pre-BRFA run spot-checks. (2) de/en/sv pilot only —
the method is language-agnostic, so more languages (or relaxing to gate A alone: ~12,000 at
~82%) scales the pool further.

### Full High-tier verification — fresh candidates are harder than held-out (689, sonnet)
Verifying **all 689** monosemous High-tier *candidates* (lexemes that LACK the grounding — the
actual writes, not held-out ground truth): **354 yes (51%) / 228 no (33%) / 107 uncertain (16%)**.
That ~51% "yes" is far below the **85%** "yes" on the held-out gate-ABD set — and the gap is
*real*, not a verifier fault:

- **Held-out truth is enriched for easy cases.** Those lexemes already had a P5137 (often from
  label-matching), so they skew cognate/correct. The fresh candidate stream is never-curated
  and genuinely harder.
- **The rejections are correct catches.** A 12-row sample of "no" was 12/12 right, including a
  *systematic* error class: feminine demonyms whose cluster anchor is the **language** item
  (`Albanerin/Armenierin/Bulgarin/Chinesin/Dänin/Estin/Finnin → Q<language>` = "X woman, not
  the language") — ~26% of all "no" reasons mention person/female/language mismatch. Also
  caught: fractions (`Achtzigstel`→one-eightieth), gendered animals (`Füchsin`→vixen), title
  vs common noun (`Despot`→tyrant). Structure writes these confidently wrong; the verifier is
  load-bearing exactly here.

**Revised writable yield:** applying the *measured* 51% "yes" (not the optimistic 85%) to the
7,400 pool → **~3,800 verifier-approved writes** — fewer, but with the bad ~half pruned, so
cleaner. The verifier-"yes" precision on fresh candidates is now measured **directly** by the
human review: of 188 verifier-"yes" High-tier candidates reviewed, 180 were confirmed →
**95.7%** (Wilson 95% CI [91.8%, 97.8%]). This is the *real write stream* (not the held-out set,
which is enriched for easy cognate cases), and it overlaps the held-out 95.9% — two independent
estimates that agree. Caveat: a single reviewer, and the verifier and reviewer are not
independent (both judge from the same CC0 description). Reproduce with `review_queue.py status`;
per-case verdicts are cached in `data/verify.sqlite3` (export on demand with `verify.py --csv`).

**Demonym→language pre-filter — investigated, NOT viable (negative result).** Tempting to drop
anchors that are P31 a language (Q34770/Q33742/Q1288568/…) since the demonym errors cluster
there. But measurement kills it: in held-out A&B&D, language anchors hit **93.1%** (n=29) vs
84.8% non-language — *higher*, because held-out language links are legit language-name nouns
(*Latein*→Latin). The candidate-stream language anchors are the opposite population (feminine
demonyms *Albanerin/Chinesin*→language, never grounded). Same structural feature, opposite
correctness — so a structural filter would drop high-precision legit cases (and in the 689
candidates it would drop the one verifier-"yes" case, *Lateinisch*→Q397 Latin, while the
verifier correctly kept it and dropped the other 31). The demonym-vs-language-name distinction
is **semantic**, only the verifier can make it (31/32 correct here). Don't pre-filter on anchor
class; let the verifier judge from the description.

## Conclusion

The method works: a structurally-clean candidate stream at ~62% precision, lifted to ~82% by a
cheap CC0-only LLM verifier with 96% recall. The design choices (the `(lexentry, sense_num)`
gate, single-dominant-anchor requirement, never overriding existing P5137, CC0-only
verification) are all validated on real data.

## Caveats

- **Provenance not yet separated.** The held-out "truth" includes P5137s that may share the
  method's own label-matching origin, which can inflate the baseline. De-biasing needs P887
  reference extraction in `load_lexemes.py` (not done).
- **The verifier alone hugs 95%; the per-edit human gate is the real safety mechanism.** Pooled
  held-out n=470 gives verifier-only 95.9% (CI ~[93.5%, 97.5%]) — at the bar, not clear of it,
  and at a true rate this close no feasible n lifts the whole interval above 95%. This is the
  wrong number to gate on: in the pilot/trial every edit is individually human-confirmed against
  the anchor's CC0 description before writing, so output precision is bounded by human review, not
  the verifier's statistical rate. The 95.7% fresh-stream figure (180/188) is reported for
  transparency, not as the guarantee. 2-of-3 voting remains available but is optional.

## Reviewer guidance (recurring judgment calls)

P5137 links a **sense to a concept** — it is form-, frequency-, and gender-independent. The
calls reviewers (and BRFA examiners) keep hitting:

- **Scope mismatch → ABSTAIN, but the test is dominant-referent vs lateral, not "broader vs
  equal" (the most important rule).** P5137 asserts the sense *denotes that exact concept*. A
  scope difference is **not** an automatic reject — the discriminating question is: *is the anchor
  the lemma's dominant/default referent, in a clean is-a relation?*
  - **Accept** when the anchor is the word's central meaning and the gloss merely reads wider than
    the word's actual denotation (gloss-artifact breadth): *Kugel* glossed "rundes 3D-Objekt" →
    Q12507 "sphere" (the word *centres* on sphere; the breadth is generic phrasing — closer to a
    register difference than a true hypernym); *Kanute* glossed "jemand, der Kanu fährt" → Q13382566
    "canoe athlete" (the word's prototype is the sportsperson). The demonym and gender conventions
    below are the same case — individual ↔ people-collective and m./f. forms are **EQUAL** for
    P5137 (the relation lives on the lexeme, not the sense), so *keep* them.
  - **Abstain** when the anchor is a **laterally-overlapping near-synonym** that is *not* the word's
    centre, or a **taxon rank narrower than the word implies** — a loose link asserts a false
    equivalence and is worse than no link. *Zwielicht* (dim/ambiguous light, can occur with the sun
    above the horizon) → Q164160 "twilight" (*defined* by the sun below the horizon): twilight is
    not what "Zwielicht" centrally means → reject. *retiree* → "pensioner", *renässans* (revival) →
    the Renaissance *epoch*, *Mammut* (genus) → one *species*: same class, the *steam/vapor*,
    *novella/novel* family that strict precision scores as a miss. Declining these is the discipline
    that lifts precision to ~96%.
  The verifier passes scope mismatches through on a surface-plausible read ("both are dim light"),
  so this is exactly where **human review** adds the most value — `review_queue.py` tags each
  broader/narrower pair with a "defer?/keep?" scope hint and floats them to the top of the queue.
  You need not find a better item; if the anchor is the wrong granularity, abstain.
  - **Tell: verifier hedge-words flag a near-synonym.** When the verifier's reason (or the item's
    description) reaches for *"substantially overlap"*, *"broadly"*, *"most commonly"*, *"similar
    to"* instead of asserting plain denotation, inspect for a superset/subset mismatch — it usually
    is one. *retiree* → Q1749879 "pensioner" (verifier: "substantially overlap"; a retiree need not
    draw a pension, a pensioner need not be retired) → reject. Contrast a clean accept whose reason
    is unhedged ("X *is* / *means* / *denotes* Y").

- **Frequency / register is out of scope — archaic common nouns are valid targets.** Wikidata's
  lexicon is comprehensive (archaic, poetic, dialectal, obsolete all belong). If the sense
  denotes the concept, link it. *Aar* → Q2092297 (eagle) is correct even though *Aar* is archaic
  and unknown to most Germans — grounding under-served vocabulary is a strength, not a problem.
  "Archaic" is a separate optional register annotation, never a reason to withhold the link.
- **"Common noun" means the grammatical class (Gattungsname), not "frequent word."** It excludes
  proper nouns, not rare ones. *Aar* is in scope.
- **Abstract / relational concepts are out of the concrete-noun pilot scope → abstain.** If the
  anchor is a quality, relation, role, or generic umbrella rather than a concrete thing, decline
  for the pilot. *plus* → Q24050179 "advantage" (P31 *general term*, P279 *relative quality*) is
  abstract and loosely denoted (a "general term" vs the colloquial "a plus") → reject. This is
  gate D's known limit: "anchor has *any* P31" admits abstract typed classes. The fix is the
  abstract-class **P31/P279 blocklist** — now built as `gates.py` (see below).
  - **Refinement: precise count/event/object nouns approve; fuzzy quality/trait nouns reject.**
    "Abstract" is not the test on its own — *fuzzy-boundary* is. A non-physical noun whose
    denotation is a **precise, countable concept** is fine: *helgonförklaring* → Q51621
    (canonization, an act/event), *fackterm* → Q12812139 (specialized term, a metalinguistic
    count noun) — both exact, both approve. What we reject is the **quality/trait** with edges
    that can't be pinned to the item: *ihärdighet* → Q21168708 "determination" (perseverance /
    tenacity / diligence all blur — the runner-up anchor is literally *flit*/diligence), same
    class as *plus*/*pro* → advantage. Process nouns that are *also* near-synonyms (e.g.
    *efterforskning* → utredning/inquiry) sit on the seam → abstain.
- **Register gate (pejorative / slang / vulgar).** P5137 links a sense to the concept it denotes,
  and register is normally a property of the *word*, not the concept — so it is *usually* not a
  reason to reject. The exception: when the anchor item is itself **modelled as register-marked**
  (`P31 → Q545779` *pejorative/skällsord*, or its description says slang/derogatory/vulgar), the
  target must share that register, else abstain. A neutral word linked to a pejorative-framed item
  (or vice-versa) is a real mismatch. Confirmed clean both ways: *dumskalle* → Q193607 "idiot"
  (P31 *pejorative*) and *babbe* → Q356367 "blatte" (pejorative for a person) — target and anchor
  are both insults, so the link is *strengthened*, not weakened.
- **Ignore plurality.** P5137 is on the sense, not a form. The lemma is conventionally singular;
  an item labeled with a plural (e.g. *Asiaten* / "Asian people") is just how collective concepts
  are labeled — not a mismatch. *Asiat* → Q4058325 ("Asian people") is fine.
- **Ignore grammatical gender UNLESS the concept itself is gendered.** Neutral people/profession
  concepts (no P2521 female-form / P3321 male-form split) take both masculine and feminine
  demonyms: *Asiat* (m.) and *Asiatin* (f.) both link to the same neutral item; gender lives on
  the lexeme, not in P5137. Only if the anchor is specifically "X man" vs "X woman" does a
  gender-crossed link become wrong.
- **For demonyms, the real check is people-vs-place-vs-language.** The anchor must be the *people*
  concept (correct: *Asiat* → "Asian people"), not the continent or — the systematic error class
  the verifier catches — the **language** (wrong: *Albanerin* → Albanian language).
- **Count vs collective** (one *Asiat* vs the *Asian people* group) is a known soft spot in
  Wikidata's demonym modelling. The bot inherits the existing convention; it does not try to fix
  it, and that is not a reason to abstain.
- **Biological common names → taxa.** Anchor at the taxon *rank the vernacular denotes* (cactus =
  family, dog = species, oak = genus) — never narrower than the word implies. Common-noun sense →
  taxon is standard practice; the taxon doubles as the group of organisms and usually records the
  word back via P1843 (taxon common name), so the link is reciprocal. Prefer a "group of organisms
  known by a common name" item only if one *exists* (it does for *eagle* → Q2092297; it does **not**
  for *cactus*, so the family Q14560 is the only and correct anchor). The real risk is a
  **wrong-organism homonym**, not rank-vagueness: "Cactus" labels the plant family (Q14560 ✓), a
  former plant genus, *and* a crustacean genus — the verifier must confirm the right organism at
  the right rank. *Kaktus* → Q14560 (family Cactaceae) is correct.

## Anchor-class scope filter (gates.py)

A SCOPE filter (not a precision filter): excludes anchors whose P31/P279 is a class that can't be
a concrete-common-noun concept. Two sources: **makesense** (Nudin/makesense `queries/en.sparql`)
76-entry P31 + 9-entry P279 false-positive lists (humans, films, albums, bands, genes,
disambiguation pages, creative works…) + an **abstract-quality extension** (general term Q12379553,
relative quality Q100195948, quality Q1207505 — the *plus/pro* → "advantage" class).

The key reason it's worth building (and *not* redundant with the verifier): **the verifier checks
denotation, not scope.** It said "yes" to *plus* and *pro* → Q24050179 "advantage" because they
genuinely denote it — abstractness/scope is an axis it doesn't judge. So the filter catches what
the verifier structurally can't. Consequently held-out hit-rate is the *wrong* yardstick (a correct
propagation to an out-of-scope concept should still be dropped); measured anyway, it removes a
small, mildly-below-average slice: held-out A&B&D dropped set 71–75% (vs 85% kept), **~3% of
High-tier volume** (20/689). Applied in `review_queue.py` (the pool query) and `export_review.py` so out-of-scope
anchors never reach human review (removed 6 from the live queue, incl. *plus*/*pro*). Contrast the
**demonym→language** filter, which measurement *rejected* (it dropped a 93%-precision legit set);
the difference is that this filter drops on a scope axis the verifier ignores, not a precision claim
the verifier already covers.

## P887 reference item (the heuristic)

Every written statement carries `based on heuristic (P887) → <item>` + `retrieved (P813)`, never
bare. Wikidata has an intentional "inferred from X" family of heuristic items (all `P31 →
heuristic Q201413`: e.g. Q69652283 inferred from title, Q84423633 inferred from label), but
**none fits P5137 cluster-propagation** — the closest, **Q118436369** "inferred from semantic
correspondence", is scoped to *etymology/derivation* ("derivation statements on lexemes where
the etymology can be inferred from a matching sense"), not sense→item linking. Decision
(2026-06): **create a purpose-made item** rather than misuse an existing one —

- label: **inferred from translation** · alias: *inferred from sense-aligned translation cluster*
- description: *inferring the item for this sense (P5137) of a lexeme from a translation in
  another language whose corresponding sense already has it*
- `instance of (P31) → heuristic (Q201413)`

Created as **Q140308675** (2026-06, `P31 → heuristic Q201413`, constraint-clean) and pinned to
`HEURISTIC_ITEM` in `write.py`. Creating the item is itself part of the BRFA disclosure.

## Reproduce

The run order lives in the README (it has drifted since — extra fetch stages, the lane
selectors, `assign.py`) and is not duplicated here so the two can't disagree. The measurement
entry points are `precision.py` (structural, held-out), `ablation.py` (the gate table) and
`measure_lift.py` (verifier lift).

## What happened to this doc's "Next" list

All four items are settled; kept here so they aren't re-proposed.

1. **2-of-3 verifier voting** — never needed. The per-edit human gate is what the precision claim
   rests on, so voting stayed optional and was not built.
2. **Review queue + `wbeditentity` writer** — built, and the monosemous lane has been writing
   assisted edits ever since. The append-only decision log is the authorization chain.
3. **P887 provenance de-bias** — **empirically dead.** Barely any existing P5137 statement carries
   a reference of any kind, and essentially none carries P887, so there is no statistical power to
   de-bias with. Don't re-open it.
4. **Talk-page post** — drafted (`talkpage_draft.wiki`, `talkpage_post.wiki`); posting the short
   question is the current next step.

For where the project goes from here, see `sense_assignment_findings.md` §4.
