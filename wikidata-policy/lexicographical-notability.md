# Wikidata:Lexicographical data/Notability

- Source: https://www.wikidata.org/wiki/Wikidata:Lexicographical_data/Notability
- Retrieved: 2026-07-27 (from raw wikitext)
- Status: Documentation page in the `Wikidata:` namespace. It carries **no policy or guideline banner** and no shortcut — it is the community's written-up reading of how [Wikidata:Notability](https://www.wikidata.org/wiki/Wikidata:Notability) applies to lexemes, not a separately adopted policy. It says its standards "are intended to align with Wikidata's notability criteria."

## Why lexemes need their own page

Lexemes have no sitelinks (Wiktionary interlanguage links are handled by the
Cognate extension, not Wikidata), so **notability criterion 1 is unavailable**.
The page therefore spells out how criteria **2 (clearly identifiable entity)**
and **3 (structural need)** apply to lexemes, forms, and senses.

## Criterion 2 — clearly identifiable

A lexeme is clearly identified when "a serious and publicly available source
treats it distinctly from other lexemes that may be mentioned in that source,
*and* that source is indicated on the lexeme somewhere, either through an
external identifier statement, a 'described by' statement, or as a reference on
another statement on that lexeme." For a dictionary or lexical database this
"usually entails that it is dealt with within a single entry."

Note the two-part test: a source must exist **and** be recorded on the entity.

### Senses specifically

One sentence, and it is the whole rule:

> "The senses of a lexeme may be clearly identified in a similar manner to
> lexemes themselves."

So a sense is expected to be one that some serious, publicly available source
treats distinctly — with that source indicated on the sense. **Forms are the
explicit exception**: they "may generally be added without a need to separately
source each one." Senses get no such exemption.

### The five ways to indicate a source

Listed in the page's own **decreasing order of preferability**:

1. An **external identifier** to a lexical resource describing the lexeme.
2. A **P8394** (gloss quote) on a sense, with the resource as a reference.
3. A **P5831** (usage example), with the source as a reference.
4. A **P1343** (described by), qualified with P304 (page) or similar.
5. A **P973** (described at URL).

And: "It is generally preferred to use a source as a reference on other
statements to the extent that is possible, rather than leaving it as a P1343 or
P973 statement on a lexeme, form, or sense."

The worked example for senses: "Each of the five senses of the Bengali lexeme
L618339 has gloss quotes from two dictionaries."

## Criterion 3 — structural need

Lexemes may be kept "simply due to improving the completeness of another lexeme,
form, or sense" — mainly to complete **derivation chains** (P5191 *derived
from* / P5238 *combines*), or to fill **combination gaps** (a component of a
compound that is ill-described on its own, "*if that lexeme is promptly linked
to the larger lexeme that contains it*").

This is about *lexeme* creation, not sense creation; the page gives no
structural-need argument for senses.

## Other points

- Sum-of-parts entries are **not** automatically excluded the way they are on the
  English Wiktionary, "*as long as* there are adequate sources on them."
- **Phonemes are not accepted** as lexemes (they belong in items). **Letters are
  accepted**, but should be modelled as nouns rather than using Q9788.

## Relevance to this project

This is the page that governs the senseless-target stream in
`notes/sense_creation_plan.md`, and it is a **live gap** in that plan.

Our created senses carry a gloss derived from the **anchor item's description**
plus a P5137 statement. Neither is one of the five source indications above: the
item description is not a lexical source, and P5137 is not a provenance
property. So by the letter of criterion 2 the created sense is unsourced — it is
identified by *us*, not by a dictionary that treats it distinctly.

Three things soften that, and they should be stated explicitly in the BRFA
rather than left implicit:

- The **lexeme** is pre-existing and (presumably) already sourced; only the sense
  is new, and the page's sentence on senses is a single permissive analogy rather
  than an enforcement rule.
- **MachtSinn** makes exactly this edit at scale today with no source on the
  created sense, and the Wikibase data model page names that behaviour approvingly
  (see `lexeme-data-model.md`).
- Our upstream **WikDict/DBnary** sense distinction *does* come from a dictionary
  that treats it distinctly — we simply cannot quote it (licensing), which is the
  same situation the page describes for the Bengali example whose dictionaries
  "cannot be quoted for copyright reasons." That suggests a cheap mitigation:
  attach a **P1343 / external-identifier** statement pointing at the source entry,
  even where P8394 is unavailable. Nothing in the current write path does this.

## Related links

- [Wikidata:Notability](https://www.wikidata.org/wiki/Wikidata:Notability) — see `notability.md`
- [Wikidata:Lexicographical data/Documentation/Senses](https://www.wikidata.org/wiki/Wikidata:Lexicographical_data/Documentation/Senses) — see `senses.md`
- [Wikidata:Lexicographical data/Documentation/Resources](https://www.wikidata.org/wiki/Wikidata:Lexicographical_data/Documentation/Resources) — per-language source lists
- [Help:Sources](https://www.wikidata.org/wiki/Help:Sources)
