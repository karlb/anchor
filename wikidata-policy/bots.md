# Wikidata:Bots

- Source: https://www.wikidata.org/wiki/Wikidata:Bots
- Retrieved: 2026-06-29 (from raw wikitext)
- Status: **Official policy.** (The Mass-editing RfC proposes to *slim this down* once that policy is adopted — see `mass-editing-policy.md`.)

## What a bot is

> "**Bots** … are tools used to make edits without the necessity of human
> decision-making."

They can add sitelinks, labels, descriptions, statements, sources, and create
items. Because they edit fast and can do damage, a bot policy applies.

## Bot accounts

- **Separate, dedicated account** — operators must create a distinct account for
  the bot, generally named after the operator or the function plus "bot".
- The bot's user page should carry the `{{bot}}` template; the **operator must be
  identified** on it. The operator remains responsible for all the bot's edits.
- Bots **shouldn't edit while logged out**, and can use the `assert` parameter to
  guarantee it.
- On damage: the operator is asked to stop the bot, an admin may block it, and
  the **operator must clean up** the damage. Watching [[Wikidata talk:Bots]] is
  suggested.
- Bot accounts must stay within their approved tasks and must never make
  non-automated edits in the user-talk namespace.
- **Respect [maxlag] and follow [API:Etiquette].** (No specific maxlag number is
  given here — it links to the manuals. See `api-etiquette.md`.)

## Approval process

- File a request at **[[Wikidata:Requests for permissions/Bot]]** describing the
  task. **The request must be started *before* the test run begins.**
- Run a **test run of between 50 and 250 edits** so the community can verify
  correct behavior.
- If testing succeeds and the task has community support, an admin closes the
  request and a **bureaucrat grants the bot flag** (a bureaucrat closer can flag
  at the same time).
- List all of a bot's tasks on its user page; use descriptive edit summaries that
  say what's happening and that a bot is doing it.
- **No approval needed** if the bot only edits the operator's/bot's own
  userspace or the sandboxes — but it must still respect maxlag.
- **Adminbots** (block/delete/protect) need approval at *both* Requests for
  permissions/Bot *and* Requests for permissions/Administrator.
- Revocation: open a section on [[Wikidata talk:Bots]] + notify the bureaucrats'
  noticeboard. When in doubt about scope, file another request.

## Bot requirements

**All bots**
- Be able to set a limit for maximum edits per minute.
- Set the bot flag for **all** edits.

**Langlink import bots** (full list on *Import bot features*)
- Support `action=wbgetentities`; do interwiki conflict checking.

**Statement-adding bots** (most relevant to `write.py`)
- Add **sources** to any statement added, unless it's agreed "common knowledge"
  (then state where it was copied from).
- Add P31 (instance of) and/or P279 (subclass of) where possible.
- Check they aren't adding **duplicate** statements.
- A new source for an existing statement → add it **as a source**, not as a new
  statement.
- Imports from Wikipedia → add P143 (imported from) and P813 (retrieved).
- Monitor the constraint-violation reports for errors the bot caused.

## Relevance to this project

If `write.py` ever runs unattended at speed it falls under this policy: a
dedicated "…bot" account with `{{bot}}` and operator named on the user page;
a task request filed at Requests for permissions/Bot **before** a 50–250-edit
trial; bureaucrat-granted flag; a configurable per-minute cap; maxlag + API
etiquette respected. The statement-adding rules already bite: it adds P5137 sense
links to *existing* statements, so the "add a new source rather than a new
statement" and "no duplicates" guidance applies, and constraint-violation reports
are worth watching. For now its edits are individually curated — the manual side
of the line — so the Mass-editing RfC is the more directly applicable framework.

## Related links

- [Wikidata:Requests for permissions/Bot](https://www.wikidata.org/wiki/Wikidata:Requests_for_permissions/Bot) — see `requests-for-permissions-bot.md`
- [Wikidata:Bots/Import bot features](https://www.wikidata.org/wiki/Wikidata:Bots/Import_bot_features)
- [mw:Manual:Maxlag parameter](https://www.mediawiki.org/wiki/Manual:Maxlag_parameter) · [mw:API:Etiquette](https://www.mediawiki.org/wiki/API:Etiquette) — see `api-etiquette.md`
- [mw:API:Assert](https://www.mediawiki.org/wiki/API:Assert)
- [Wikidata:Bot requests](https://www.wikidata.org/wiki/Wikidata:Bot_requests) · [Wikidata:Creating a bot](https://www.wikidata.org/wiki/Wikidata:Creating_a_bot)
- [Wikidata:Requests for comment/Mass-editing policy](https://www.wikidata.org/wiki/Wikidata:Requests_for_comment/Mass-editing_policy) — see `mass-editing-policy.md`
