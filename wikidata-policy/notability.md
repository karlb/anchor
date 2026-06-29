# Wikidata:Notability

- Source: https://www.wikidata.org/wiki/Wikidata:Notability
- Retrieved: 2026-06-29 (from raw wikitext)
- Status: **Official policy** (protected; shortcut **WD:N**). A reform discussion is open at *Wikidata:Requests for comment/Notability policy reform*.

## Framing

Two stated goals for Wikidata: **centralize interlanguage links** across
Wikimedia projects, and **serve as a general knowledge base**. An item is
acceptable **if and only if it meets at least one of the three criteria** below.
The guidelines are "intentionally left a bit vague"; disputes go to a request for
deletion and "the final decision is always up to the community."

## The three criteria (verbatim)

1. **Sitelink** — "It contains at least one valid sitelink to a page on
   Wikipedia, Wikivoyage, Wikisource, Wikiquote, Wikinews, Wikibooks, Wikidata,
   Wikispecies, Wikiversity, or Wikimedia Commons."
2. **Clearly identifiable entity** — "It refers to an instance of a clearly
   identifiable conceptual or material entity that can be described using serious
   and publicly available references."
3. **Structural need** — "It fulfills a structural need, for example: it is
   needed to make statements made in other items more useful."

## What makes a sitelink *invalid* (criterion 1 detail)

Not valid: talk pages; MediaWiki namespace; special pages; files; translation
pages; User or Draft namespace; LiquidThreads (Thread/Summary) and Structured
Discussions (Topic) namespaces; Portal subpages; TemplateStyles (`.css`) pages.
A sitelink may point to a redirect, but **a redirect link does not count toward
notability**. Project-specific rules:

- **Templates** need **at least two** sitelinks (excluding /doc, /XML, /meta,
  /sandbox, /testcases, /TemplateData); module subpages must not be /doc.
- **Commons-only category items** are allowed only if there's a corresponding
  main item with a Commons gallery sitelink, or the item is used in a
  Commons-related statement (e.g. P2033).
- **Wikisource** — mainspace, Author, Translation, and Portal pages are valid;
  Index/Page namespaces are not; mainspace subpages (e.g. individual chapters)
  aren't valid unless separately authored / stand-alone / separately translated.
- **Wikinews** Comments namespace — excluded.
- **Wikidata** structured-data namespaces (items, properties, lexemes,
  EntitySchemas) — not allowed as a sitelink target.
- **Wiktionary** citation pages and main namespace — excluded (Cognate handles
  interlanguage links).
- Meta-Wiki / MediaWiki.org / Wikimania etc. — status undetermined, case by case.

## If your data doesn't qualify

The policy points such data to the **Wikibase Ecosystem** (Wikibase Cloud /
Wikibase Suite, wikiba.se) rather than Wikidata.

## Relevance to this project

`write.py` adds P5137 ("item for this sense") links **to existing lexeme senses**
and links them to **pre-existing, notable** anchor items — it does not create new
items — so item-notability questions don't normally arise. Notability matters
mainly at **step 2 of the mass-editing process** (don't mass-create non-notable
items). Note also that **lexicographical data has its own separate notability
guidance** (linked below), distinct from these item criteria.

## Related links

- [Wikidata:Notability/Exclusion criteria](https://www.wikidata.org/wiki/Wikidata:Notability/Exclusion_criteria)
- [Wikidata:Notability/Items considered notable](https://www.wikidata.org/wiki/Wikidata:Notability/Items_considered_notable)
- [Wikidata:Lexicographical data/Notability](https://www.wikidata.org/wiki/Wikidata:Lexicographical_data/Notability)
- [Wikidata:Requests for deletion](https://www.wikidata.org/wiki/Wikidata:Requests_for_deletion)
- [Wikidata:Requests for comment/Mass-editing policy](https://www.wikidata.org/wiki/Wikidata:Requests_for_comment/Mass-editing_policy) — see `mass-editing-policy.md`
