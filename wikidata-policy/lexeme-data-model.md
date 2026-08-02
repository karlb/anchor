# Lexeme data model (Glossary + WikibaseLexeme conceptual model)

- Sources:
  - https://www.wikidata.org/wiki/Wikidata:Lexicographical_data/Glossary (shortcut **WD:Gloss**)
  - https://www.mediawiki.org/wiki/Extension:WikibaseLexeme/Data_Model
- Retrieved: 2026-07-27 (from raw wikitext), plus code checks against
  [mediawiki-extensions-WikibaseLexeme](https://github.com/wikimedia/mediawiki-extensions-WikibaseLexeme) `master`.
- Status: Glossary is community documentation; the data model page is "a **living
  document** … not a specification of any concrete binding, implementation,
  mapping, or serialization." Where the two disagree with the code, the code wins
  and is cited below.

## The shape

- **Lexeme** — "a lexical element of a language, such as a word, a phrase, or a
  affix." Holds an ID (`L123`), a lemma, a lexeme language, a lexical category, a
  list of lexeme statements, **a list of forms, and a list of senses**.
- **Sense** — "a meaning of a lexeme which it may represent in a text. **Any
  number of senses may be defined, although every lexeme should have at least
  one.**" Holds an ID, a gloss, and a list of sense statements.
- **Subentity** — a form or a sense. Subentities "may be used in statements, but
  they do not have their own pages"; everything lives on the lexeme's page. This
  is why every sense edit is a revision of the *lexeme*.

## Glosses — the hard constraints

- A gloss is "a **non-empty** unordered set of pairs, each of which contains a
  string and a gloss language." So **a sense cannot exist without at least one
  gloss**: "A Gloss cannot be entirely empty, at least one language has to be
  provided."
- **"Glosses cannot be referenced."** There is no reference slot on a gloss —
  which is exactly why P8394 *gloss quote* exists as a statement.
- "A good gloss provides little or no space for ambiguity about the meaning.
  Lexemes with multiple senses should have glosses that are easily
  distinguishable from each other."
- "**Short glosses of only a single or a few words should be avoided** as it
  leaves too much space for interpretation of the meaning." Read this together
  with the opposite-leaning advice on `senses.md`; the axis that reconciles them
  is gloss language (detailed in the lexeme's own language, short elsewhere).

## Sense numbering — what S1 actually means

The documentation pages say only that IDs are `L<id>-S<n>`. The behaviour is in
the code:

- The Lexeme carries a **monotonic `nextSenseId` counter**. Adding a sense mints
  `S<nextSenseId++>` (`src/Domain/Model/Lexeme.php:303`), and
  `increaseNextSenseIdTo` **throws if asked to move the counter backwards**
  (`Lexeme.php:331`). So numbers are handed out in creation order and are
  **never reused** — deleting a sense leaves a permanent gap.
- Senses are always **rendered and serialized in numeric ID order**:
  `SenseSet::toArray()` sorts by the numeric part of the sense ID
  (`src/Domain/Model/SenseSet.php:41,62-71`), and the unsorted accessor is
  explicitly flagged "Only use this method if the order is certainly
  insignificant."

Consequences worth stating plainly:

1. **S1 means "created first" and nothing else.** No page in the documentation
   set defines a main/primary/dominant sense, and there is no requirement that
   the first sense added be the most common meaning.
2. **But the resulting order is permanent and visible.** Display order = creation
   order, there is no reorder feature, and delete-and-recreate mints a *higher*
   number — so S1 cannot be reclaimed for a later-discovered dominant sense.

## Item links are weaker than they look

An editorial note on the data model page (flagged there as an unresolved
documentation to-do, not settled guidance) warns about "a common source of
misunderstandings":

> "Senses can be connected to Wikidata Items via an appropriate Statement they
> evoke or denote … However, such a connection should **not** be interpreted as
> the lexeme actually representing the concept defined by the item … In
> particular, if two lexemes have senses that refer to the same concept in this
> way, this does not imply that the two lexemes are synonyms."

Its example: English *hot* and *cold* could both have a sense referring to
Q11466 (temperature), though they are antonyms. Note the scope — this is about
evokes/denotes-style item links in general; P5137's own documentation
(`senses.md`) is stricter, demanding that the item "properly represents" the
concept rather than being "only *partially* related to it."

Also noted as a to-do: "function words should not have senses."

## Relevance to this project

- **Our exact edit is named in the data model page**, approvingly:

  > "for apple the Q-items English description *fruit of the apple tree* is
  > copied as gloss when using tools like **MachtSinn** to match lexemes and
  > Q-items together and **create missing senses**."

  That is a stronger precedent than the "MachtSinn does it too" argument in
  `notes/sense_creation_plan.md:162` — the item-description-as-gloss pattern is
  written into Wikibase's own model documentation. Cite it in the BRFA.

- **The minor-sense-as-S1 concern** (`sense_creation_plan.md`, "Risk model") is real
  but is a *display* problem, not a rules problem. Creating S1 for a polysemous word's
  minor meaning breaks no requirement; it just parks that meaning permanently at
  the top of the lexeme page, where the counter guarantees it can never be moved
  down. So the mitigation that matters is the one already chosen — a gloss
  specific enough that a later editor cannot mistake it for the dominant meaning
  and pile data onto it. The sense *number* is not worth engineering around.

- **`is_create` payloads must always carry a gloss.** A sense with an empty gloss
  is rejected by the model, not merely discouraged.

- **The "not synonyms" caveat qualifies our translation claim** — see the
  `wikidata-translation-shared-reference` memory. Shared P5137 is a shared
  reference, not an assertion of synonymy; the caveat is weakest exactly where
  our pipeline lives (concrete nouns with a single dominant referent).

## Related links

- [Wikidata:Lexicographical data/Documentation/Senses](https://www.wikidata.org/wiki/Wikidata:Lexicographical_data/Documentation/Senses) — see `senses.md`
- [Wikidata:Showcase lexemes](https://www.wikidata.org/wiki/Wikidata:Showcase_lexemes)
- [Wikidata:Tools/Lexicographical data](https://www.wikidata.org/wiki/Wikidata:Tools/Lexicographical_data) — MachtSinn and friends
