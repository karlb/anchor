# P5137 from translation clusters

Code behind [Karlb42Bot](https://www.wikidata.org/wiki/User:Karlb42Bot): adds **P5137 (item for this sense)** to Wikidata lexeme senses,
inferred from Wiktionary translation clusters ([WikDict](https://www.wikdict.com)).
Discussion: [Wikidata talk:Lexicographical data](https://www.wikidata.org/wiki/Wikidata_talk:Lexicographical_data#Creating_first_senses_from_translation_clusters).

## Method

1. Translation edges are grouped by Wiktionary sense (`lexentry, sense_num`), never by gloss, so
   *Bank* (bench) and *Bank* (finance) stay apart.
2. If cluster members already carry P5137 → Q and agree, Q is proposed for the members that lack it.
3. An LLM compares the lexeme with the item's own labels and descriptions and answers yes/no and
   which sense (or none).
4. A human approves each edit, except for the bot task below.

Only CC0 data is written: the item, the item's description as gloss of a new sense, and the
reference P887 → [inferred from translation (Q140308675)](https://www.wikidata.org/wiki/Q140308675)
plus P813. Wiktionary text (CC BY-SA) is never copied.

Most of the code also serves the operator's assisted, individually approved edits (P5137 on
existing senses); only `--bot` runs without approval.

## Bot task: first senses

`write.py --bot` creates a first sense with P5137 on lexemes that have no senses, when:

- a translation cluster links the lemma to the item,
- the item's label or alias in the lexeme's language is exactly the lemma,
- the LLM check passes.

Gloss: the item's description in the lexeme's language, else in another language.
Trials: 149/150 correct (de/en/sv, reviewed by the operator), 100/100 links correct (ru, reviewed by
Infovarius).

Safety, per edit: the lexeme is re-fetched and skipped if it gained a sense; no existing P5137 is
ever changed. Edits are rate-limited (`--max-edits-per-min`, default 30), send `maxlag=5`, use
`assert`, and each run is one [EditGroups](https://editgroups.toolforge.org) batch.

## Pipeline

```
infer.sqlite3 ─build_clusters.py─▶ clusters ─┐
lexeme dump ──load_lexemes.py──▶ wikidata ───┴─match.py─▶ candidates
  ─fetch_items.py, fetch_anchor_langs.py─▶ item labels/descriptions (CC0)
  ─verify.py─▶ LLM verdicts
  ─review_queue.py─▶ notes/runs/decisions.csv (append-only human decisions)
  ─write.py─▶ Wikidata (wbeditentity)
```

Stdlib-only Python, run with [uv](https://docs.astral.sh/uv/). Data lives in `data/` (git-ignored).
`notes/` has measurements and design notes.

## Run

```bash
uv run build_clusters.py
uv run load_lexemes.py --dump latest-lexemes.json.gz
uv run match.py
uv run fetch_items.py && uv run fetch_anchor_langs.py
uv run verify.py --gate AD --senseless-only --all

uv run review_queue.py serve --senseless      # http://localhost:8731
uv run write.py --dry-run --include-senseless # print payloads, submit nothing

WIKI_USER=… WIKI_PASS=… uv run write.py --write --include-senseless  # approved rows, operator account
BOT_USER=… BOT_PASS=… uv run write.py --write --bot --limit 50        # bot task, bot account
```

Credentials are [bot passwords](https://www.wikidata.org/wiki/Special:BotPasswords).

Logic test without the dump:

```bash
uv run load_lexemes.py --dump test/fixture_lexemes.jsonl --out data/wikidata_fixture.sqlite3
uv run match.py --wikidata data/wikidata_fixture.sqlite3
```
