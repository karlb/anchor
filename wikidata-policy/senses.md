# Wikidata:Lexicographical data/Documentation/Senses

- Source: https://www.wikidata.org/wiki/Wikidata:Lexicographical_data/Documentation/Senses
- Retrieved: 2026-07-27 (from raw wikitext)
- Status: Community documentation (part of the Lexicographical data documentation set). No policy banner; descriptive, with worked examples.

A sense has exactly three components: an **ID**, its **glosses**, and its
**statements**.

## Sense ID

"Lexeme senses have identifiers starting with the ID of the lexeme they belong
to, followed by a hyphen ("-") and an "S", followed by a natural number in
decimal notation: e.g. `L3746552-S4`. These IDs are unique within Wikidata; when
a new sense is created within a lexeme, an entirely new sense ID is provided for
it."

**There is no ordering rule.** Nothing on this page — or in the Glossary or the
data model — defines a "main", "primary", or "first" sense, or says that S1
should hold the dominant meaning. See `lexeme-data-model.md` for what the
numbering actually means mechanically.

## Glosses

"Glosses define the meaning of the sense using natural language."

- **No circularity.** "The glosses for a lexeme sense should avoid using the
  lexeme within that gloss" — don't define French *tour* as merely 'tour'.
- **Detail level depends on which language the gloss is in.** For a lexeme in
  language X, "the gloss in language X … should be a more detailed explanation of
  the meaning of the sense, almost akin to providing a sort of dictionary-style
  definition. The glosses in other languages Y and Z of that same sense may be
  less detailed, so long as they are clear enough to speakers of Y and Z what the
  meaning of the sense is."
- **Over-long foreign-language glosses are actively discouraged**, because
  "one reading more-detailed glosses may not immediately understand what is meant
  by them, causing confusion later." The page's own examples: the English gloss
  for the Turkish L1124154-S1 should be `answer`, *not* "response to a question";
  the German gloss for L20410-S1 should be `Saturn`, not "sechster Planet unseres
  Sonnensystems".
- **Disambiguate where the target language forces it.** The Bengali gloss for
  that same sense adds '(প্রশ্নের)' — "(of a question)" — because উত্তর means
  both 'answer' and 'north'.

Note this pulls the opposite way from the data model page's "short glosses of
only a single or a few words should be avoided" (see `lexeme-data-model.md`).
The reconciliation is the language axis: **detailed in the lexeme's own
language, short and unambiguous in every other language.**

## Item-linking properties (which one applies)

| Property | Use |
|---|---|
| **P5137** *item for this sense* | a **substantive** concept — "typically on a noun or adjective" |
| **P9970** *predicate for* | a **predicative** concept — "typically on a verb or verb phrase" |
| **P6271** *demonym of* | a **relationship to a place** — "typically on a proper noun or adjective" |

P5137 "is most often used with nouns, [but] those are not the only lexical
category to which it may be applied." The stated constraint is the important one:

> "It is important to be sure that an item properly represents a concept and does
> not represent something only *partially* related to it."

The page's three worked negatives are all the same shape — an adjective, adverb,
or postposition sense should **not** take P5137 to an item that "is better linked
to a … noun" in that language (Estonian L381626-S1, Bengali L483767-S1, Turkish
L1160515-S2). Noun/verb pairs for the same action are handled by splitting across
properties: the noun gets P5137, the verb gets P9970, both pointing at the same
item.

## Sense-to-sense properties

- **P5972** *translation* (different language) and **P5973** *synonym* (same
  language) are both described as "*generally* not necessary if the sense has a
  P5137, P9970, or P6271 statement; if two senses … have the same property
  pointing to the same item, then they can reliably be considered translations
  [/ synonyms] … and thus the [statement] becomes redundant."
- **P5974** *antonym*, **P6593** *hyperonym*, **P8471** *pertainym of* (adjective →
  its noun, adverb → its adjective) — all same-language only.

## Provenance properties

- **P8394** *gloss quote* — the definition as it appears in a dictionary.
  "Such definitions should originate from public domain dictionaries, and the
  dictionaries themselves *must* be provided as a reference on the statement."
  Where sources carve up senses differently, quotes may be **split with ellipsis**
  across our senses (worked examples in Hindustani, Turkish, English).
- **P1343** *described by source* — for sources that cannot be quoted.
- **P5831** *usage example* — may sit on the sense directly (rather than on the
  lexeme with a P6072 qualifier) "particularly when there are many senses on a
  single lexeme."

## Usage properties

- **P6084** *location of sense usage* — regional meanings: link the sense to the
  places where the word carries that meaning. "A lexeme can have a mixture of regional
  and non-regional meanings, or might only have regional meanings."
- **P10339** *semantic gender* — "used on senses which only refer to people of a
  particular gender or animals of a particular sex. This is distinct from P5185
  [grammatical gender] — P10339 is about the meaning of the word, P5185 is about
  how the word behaves." Values are the P21 values. In languages where
  grammatical gender normally implies semantic gender, **`novalue`** explicitly
  marks a gender-neutral word (German L248778 *Person*).
- **P18** *image* — the "addSenseImage" gadget helps.

## Relevance to this project

- **P5137 is the right property for our slice.** The pipeline is concrete nouns,
  which is squarely the "substantive concept" case. Worth knowing the boundary:
  if the candidate pool is ever widened to verbs, the property changes to P9970,
  and place-relational senses want P6271 — a bare P5137 there would be wrong.
- **The "partially related" constraint is the upstream form of our
  broader/narrower triage** (`broader-narrower-triage` memory). The page's own
  negatives are all "this item is better linked to a noun in that language" —
  i.e. the community reads P5137 as demanding a *tight* concept match, not a
  loose association. That supports defer-over-accept on lateral and wrong-rank
  pairs.
- **Our gloss format is validated on the foreign-language axis but thin on the
  lexeme-language one.** Deriving the gloss from the anchor item's description
  produces exactly the short, clear, non-circular gloss this page wants *for
  languages other than the lexeme's*. For the lexeme's own language the page
  wants a fuller, dictionary-style definition — worth a look at what
  `sense_creation_plan.md`'s multi-gloss payload writes into the lexeme's own
  language slot.
- **P5972 is redundant for us, by the page's own rule.** Since every sense we
  touch gets P5137 to a shared item, the cross-language translation link is
  already implied; we should not be writing P5972 as well.
- **P10339 is confirmed additive**, matching the `p10339-additive-not-precondition`
  memory: it is a semantic-gender annotation, explicitly not a precondition for
  or modifier of the P5137 link.

## Related links

- [Wikidata:Lexicographical data/Documentation](https://www.wikidata.org/wiki/Wikidata:Lexicographical_data/Documentation) — index of the doc set
- [Wikidata:Lexicographical data/Documentation/Lexeme statements](https://www.wikidata.org/wiki/Wikidata:Lexicographical_data/Documentation/Lexeme_statements)
- [Template:Lexicographical properties](https://www.wikidata.org/wiki/Template:Lexicographical_properties) — full property inventory
- [Wikidata:Lexicographical data/Notability](https://www.wikidata.org/wiki/Wikidata:Lexicographical_data/Notability) — see `lexicographical-notability.md`
