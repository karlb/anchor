# Wikidata:Requests for comment/Mass-editing policy

- Source: https://www.wikidata.org/wiki/Wikidata:Requests_for_comment/Mass-editing_policy
- Retrieved: 2026-06-29 (from raw wikitext; the vote is live and may have moved)
- Status: **Open RfC — a *proposed* policy, not yet adopted.** Written by Lydia Pintscher (WMDE) with Peter F. Patel-Schneider, Egezort, Romaine, AdrianoRutz, Epìdosis and others.

## Why it exists (the problem)

The RfC frames mass editing as one fixable piece of a broader sustainability
problem. Three bottlenecks:

- **Query service** — the stack can't keep up with edit + query volume; the WDQS
  graph split was needed just to stay up. Worst case: "Wikidata inaccessible for
  the majority of use cases."
- **Database tables** — the `revision` and `terms` tables are near their
  scalability limits. Worst case: "taking down not just Wikidata but also
  Wikipedia and the other Wikimedia projects."
- **Social/maintenance** — "a single active editor has to oversee nearly 10,000
  Items" (≈700 if counting only Items that get edited). Most Items are created
  by a few accounts that add but don't maintain data.

Crucially: "our bot policy is ineffective. A lot of mass editing is done by
non-bot accounts but is effectively unregulated bot work … with QuickStatements
and OpenRefine," while editors who *do* go through bot approval hit delays — so
the status quo "rewards evading the bot policy."

## Goal of the RfC

1. Adopt the mass-editing policy below.
2. **Slim down [[Wikidata:Bots]]** to remove parts this policy would cover.
3. Notability reform is explicitly **out of scope** (a separate follow-up RfC).

## Scope / definition

> "all edits where changes are made to existing entities or the addition of new
> entities without being reviewed individually by the person making the edits
> and which could not reasonably be done manually."

Both mass *changes* to existing data and mass *import* of new data count as "mass
editing." There is deliberately **no numeric threshold** — the most-repeated
criticism (see votes).

## The proposed process (4 steps)

1. **Prerequisites** — registered account; gain experience with manual edits
   first; "at the very least … autoconfirmed status"; learn the data-modelling
   conventions of the area and the relevant WikiProject(s); read the `/Caveats`
   subpage.
2. **Notability & license check** — must comply with [[Wikidata:Notability]];
   data must be CC0-compatible or public domain ("Data that is not compatible
   with CC0 can not be mass added").
3. **Prepare edits**
   - Uncontroversial → skip to step 4.
   - Possibly controversial (quality, quantity, or modelling) → post a short plan
     to **Wikidata:Mass editing proposals** (why, source, amount, quality,
     modelling; **50–100 test edits** encouraged; one-off vs. recurring), notify
     the WikiProject(s), answer questions, and **wait 1 week** — proceed only if
     "no major disagreements." No consensus ⇒ not approved.
4. **Execute** — make edits in **batches that are traceable and revertable**
   (e.g. an [[Wikidata:Edit groups]] tag); check for new constraint violations /
   EntitySchema violations and fix them; **you own the cleanup** if something
   goes wrong (help via WikiProjects, Project chat, Requests for deletion).

## Dispute resolution — note the revert threshold

Following the policy means you won't be blocked on the first complaint, but your
edits can still be reverted and you may have to stop. Key number:

> "All mass edit batches not following this policy, especially those with **more
> than 100 or 5% problematic edits**, are liable to being quickly reverted."

Reverting batches mixed in with normal edits may cause collateral damage to the
normal edits.

## Best practices for tool developers (directly relevant to write.py)

- Link to the policy from inside the tool.
- Make multiple changes to the same entity a **single edit**, not many.
- Make edits traceable/revertable — e.g. an [[Wikidata:Edit groups/Adding a tool]]
  tag; each batch identifiable, with an enforced *meaningful* description (not
  just a batch number).
- Follow [[mw:API:Etiquette]].
- A tool repeatedly making bad mass edits "can be blocked until it is fixed."

## Where the vote stands (snapshot 2026-06-29)

Roughly **12 support / 5 oppose** (one oppose is collapsed as being from a
globally banned account). Support is mostly "imperfect but better than nothing,
and the problem statement is excellent." The substantive oppose arguments:

- **No definition** (DrThneed) — "what edits does this policy apply to? One
  person's mass edit is another's quick Tuesday afternoon QS batch."
- **Step 3 is the weak point** (GA Kevin) — the "uncontroversial" escape hatch
  and an arbitrary 1-week wait; praises steps 1/2/4 and the developer guidelines;
  suggests an OSM-style 80% consensus bar for disputes.
- **Author / governance** (So9q) — opposes solely because it's WMDE-authored;
  argues the fix is the backend (QLever/his Entitybase), not slowing editors.
- **Vague + adds social load** (ChristianKl) — "meaningful"/"controversial" are
  fuzzy; 50–100 test items is too many (5–10 is enough); batch editing should be
  a well-built first-party tool that shows effects and checks constraints.
- **No evidence** (GraphControl, globally banned) — nothing shows mass edits have
  a higher bad-claim rate; mass editing is a subclass of editing, so steps 1–4
  arguably apply to all edits.

## Relevance to this project

`write.py` is exactly the QuickStatements/OpenRefine-style semi-automated editing
this policy targets. If adopted, the operational checklist is: stay CC0-only
(already true), batch per entity, Edit-groups tag every batch with a meaningful
description (already done), post potentially-controversial runs to *Mass editing
proposals* and wait a week, watch the **100-edits / 5%** revert threshold, and
own cleanup. The RfC also signals the bot policy will be *trimmed* in favor of
this one.

## Related pages

- [Wikidata:Bots](https://www.wikidata.org/wiki/Wikidata:Bots) — see `bots.md`
- [Wikidata:Notability](https://www.wikidata.org/wiki/Wikidata:Notability) — see `notability.md`
- [Wikidata:Edit groups](https://www.wikidata.org/wiki/Wikidata:Edit_groups) — see `edit-groups.md`
- [Wikidata:Mass editing proposals](https://www.wikidata.org/wiki/Wikidata:Mass_editing_proposals)
- [mw:API:Etiquette](https://www.mediawiki.org/wiki/API:Etiquette) — see `api-etiquette.md`
- Credited inspiration: OpenStreetMap [Import Guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines) and [Automated Edits code of conduct](https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct)
