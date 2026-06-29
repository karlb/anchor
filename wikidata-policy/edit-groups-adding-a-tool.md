# Wikidata:Edit groups/Adding a tool

- Source: https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool
- Retrieved: 2026-06-29 (from raw wikitext)
- Status: Integration guide for tool/bot authors who want EditGroups to track their batches.

There are two paths: registered **tool developers**, and unregistered
**custom bots** (the `CB` shortcut).

## Custom bots (`CB`) — the zero-registration path

Emit edit summaries of exactly this form, replacing the hash with a freshly
generated one:

```
my very informative edit summary ([[:toolforge:editgroups/b/CB/89ead4fe|details]])
```

- `CB` stands for "custom bot" — no registration needed.
- The hash is a **randomly generated hex string, 4 to 32 lowercase hex digits**.
- The detection regex keys off the link, so the format is strict: **the link
  text must be `details`** and the URL must match exactly (the surrounding
  brackets are optional). Use the *same* hash for every edit in one batch.

Hash-generation snippets are given for JavaScript, Node.js, Java, Python, C#, and
Ruby. The Python one:

```python
import random
"{:x}".format(random.randrange(0, 2**48))
```

## Registered tools — branded identifier

If you want your own tool id (instead of `CB`):

1. Give each batch a unique identifier (a random hash is fine — "it does not need
   to reflect anything from your own application") and include it in every edit
   summary.
2. Notify **Arcstur** or **Pintoch** with:
   - the tool name;
   - the URL of a page describing the tool;
   - a regular expression to capture the identifier from an edit summary;
   - if edits go through another account (e.g. `QuickStatementsBot`), a regex to
     capture the *actual* author of the batch;
   - if users write their own summaries, a regex to catch those.

The recommended, non-ugly way to embed the id is the same link form, e.g.
`([[:toolforge:editgroups/b/mytool/e892da3f|details]])`. There is **no
self-service registration page** — you contact a maintainer.

## Relevance to this project

This is the page behind the `editgroups-cb-tagging` memory. `write.py` can tag
batches today with the generic `CB` id and a per-run hash; a branded id only
needs a message to a maintainer. The hard requirements to get right: link text
exactly `details`, URL form `:toolforge:editgroups/b/<TOOLID>/<BATCHID>`, batch
id 4–32 lowercase hex digits, one id per batch.

## Related links

- [Wikidata:Edit groups](https://www.wikidata.org/wiki/Wikidata:Edit_groups) — see `edit-groups.md`
- [EditGroups tool](https://iw.toolforge.org/editgroups/)
