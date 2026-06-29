# Wikidata:Edit groups

- Source: https://www.wikidata.org/wiki/Wikidata:Edit_groups
- Retrieved: 2026-06-29 (from raw wikitext)
- Status: Community/help documentation for the (unofficial) EditGroups tool.

## What an edit group is

> "**Edit groups** are sets of changes on Wikidata items which follow a similar
> pattern and are performed around the same time by a given user."

They're typically bot or semi-automated human edits across many items. MediaWiki
has no native concept of an edit group; the external **EditGroups** tool tracks
them (a separate instance exists for Commons). EditGroups is "an unofficial tool
maintained by volunteers."

## How it works

When you edit with tools like HarvestTemplates, QuickStatements, or OpenRefine,
each **batch gets a unique identifier and its own page on the EditGroups tool**.
The edit summary carries a link to that page. The page's worked example: an
OpenRefine edit whose summary includes

```
([[:toollabs:editgroups/b/OR/8cb3004|details]])
```

(here `OR` = OpenRefine, `8cb3004` = the batch id). That batch page does three
things:

- gives a high-level overview of the edit group;
- pre-fills an on-wiki **discussion page** for the batch (auto-categorized in
  *Category:Edit group discussions*);
- lets users **undo all edits** in the batch — which itself creates a new edit
  group that reverts each edit.

You can also find a group via the list of recent edit groups, filtering by tool,
user, and tag.

## FAQ highlights

- **Undo others' groups?** Yes, but each revert pings the author once — it's
  more polite to ask the author to self-undo (no notifications), and to discuss
  first (a revert costs as many edits as the original batch).
- **Group created items?** Undoing it means deleting those items, so you need an
  admin; you can request this from the batch page (leaves a note at *Requests for
  deletion*).
- **Redo an undone group?** Yes — from the original tool, or by undoing the undo
  group (restoring deleted items preserves the original QIDs, admin only).
- **Reverts can fail** if the data was changed since (usually means someone
  already fixed it); EditGroups still tries to undo the rest. When fixing edits
  by hand, **use MediaWiki's undo** (not deleting a claim / changing a label) so
  the author is notified and EditGroups sees the edit as undone.

## Supported tools

QuickStatements v2, QuickStatements v3, OpenRefine, HarvestTemplates, namescript,
Author Disambiguator, WikibaseJS-cli, EditGroups itself, and **any custom bot
that follows the *Adding a tool* manual**. **Pywikibot is explicitly *not*
supported** (phab:T200234).

## The EditGroups service

Unofficial, volunteer-maintained. Main instance:
https://iw.toolforge.org/editgroups/ ; bugs via its GitHub repository. (The page
links it as `:toollabs:editgroups/`, which resolves to the same Toolforge tool.)

## Relevance to this project

`write.py` already tags batches for EditGroups. The detail that matters lives on
the *Adding a tool* subpage (exact summary syntax, the generic `CB` id vs. a
registered one) — see `edit-groups-adding-a-tool.md`.

## Related links

- [Wikidata:Edit groups/Adding a tool](https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool) — see `edit-groups-adding-a-tool.md`
- [EditGroups tool](https://iw.toolforge.org/editgroups/) and its [tool statistics](https://iw.toolforge.org/editgroups/tools/)
- [Wikidata:Requests for deletion](https://www.wikidata.org/wiki/Wikidata:Requests_for_deletion)
- [Wikidata:Requests for comment/Mass-editing policy](https://www.wikidata.org/wiki/Wikidata:Requests_for_comment/Mass-editing_policy) — see `mass-editing-policy.md`
