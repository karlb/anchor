# P5137 anchor-propagation: method and pilot results

*WikDict/DBnary → Wikidata lexeme sense-linking (de/en/sv pilot). Measured 2026-06-20
against the weekly `latest-lexemes.json.gz` dump and `infer.sqlite3`.*

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
cleaner. The verifier-"yes" precision on fresh candidates isn't directly measurable (no ground
truth), but the rejections being right is strong indirect evidence it's high. Per-case verdicts:
`notes/runs/high_tier_verdicts.csv`.

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
- **High tier hugs 95%.** Pooled n=470 gives 95.9% (CI ~[93.5%, 97.5%]) — a point estimate at
  the bar, not clear of it. 2-of-3 voting or the human review queue is needed to be safely above.

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

Spec + QuickStatements are in `write.py`; pin the new Q-id to `HEURISTIC_ITEM` (the writer
refuses `--write` until then). Creating the item is itself part of the BRFA disclosure.

## Reproduce

```bash
uv run build_clusters.py
uv run load_lexemes.py --dump data/latest-lexemes.json.gz
uv run match.py
uv run precision.py          # structural precision (+ P279 tolerance once items fetched)
uv run fetch_items.py        # CC0 labels/descriptions + P31/P279 (needs www.wikidata.org)
uv run verify.py --senseless-only
uv run measure_lift.py --sample 120
uv run measure_lift.py --gate ABD --sample 120 --model sonnet   # ≥95% High tier
```

## Next

1. (optional) 2-of-3 verifier voting to push the High tier safely above 95% (pooled is at 95.9%).
2. Review queue + `wbeditentity` writer with the P887 reference → ~10 pre-BRFA edits.
3. P887 provenance extraction to de-bias the precision baseline.
4. Talk-page post (this doc + the plan's license rationale).
