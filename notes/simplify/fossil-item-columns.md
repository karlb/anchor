# Retiring the fossil de/en/sv item columns (export rendering + the 11-field row)

Two related /simplify findings (2026-07-06) deferred together because they share the
same blocker: the de/en/sv `item` table is a documented fossil (fetch_items.py), but
two consumers still need it.

**1. export_review renders anchors from the fossil columns.** The live queue renders
via `config.resolve_concept_langs` over item_lang ("one store, one resolver"); the
HTML export still shows `label_de…desc_sv` with a de/en/sv-fallback `pick`. Two review
surfaces show different evidence for fr/no (and future) anchors.
*Why deferred:* `--all-clean` exports include anchors outside the fetched ABD universe
(fetch_anchor_langs only fetches ABD anchors), where the fossil columns are the only
evidence. A straight switch would blank those rows.
*Fix sketch:* an item_lang-first cell with fossil fallback, or extend the fetch to all
clean anchors — then render both export tiers through the shared resolver.

**2. verify.candidates' 11-field positional row.** Six of the eleven fields are the
fossil label/desc columns; four files consume the tuple by magic index
(`fmt_concept` unpack, measure_lift's fabricated row, `r[8:11]` slices in ab_concept /
build_problematic_cases). Changing the SELECT breaks them at unnamed positions.
*Why deferred:* pure churn until the SELECT actually changes; also the frozen
measurement harnesses' base arm still grades on the fossil format, so the columns
can't just be dropped.
*Trigger:* whenever the fossil columns come out of the SELECT (i.e. when finding 1 and
the base-arm dependency are resolved), introduce a namedtuple in the same commit —
that's the moment the positional indices break anyway.
