# Wikidata:Requests for permissions/Bot

- Source: https://www.wikidata.org/wiki/Wikidata:Requests_for_permissions/Bot (instructions from the transcluded `/Header` subpage)
- Retrieved: 2026-06-29 (from raw wikitext)
- Status: The board where bot-flag requests are filed and decided. Shortcuts: **WD:RFBOT**, **WD:BRFA**, **WD:RFP/BOT**.

The main page is mostly a list of live requests transcluded onto it; the standing
instructions are short and live in the header. The substantive *criteria* are in
[[Wikidata:Bots#Approval process]] (see `bots.md`), not here.

## How to file a request

> "To request a bot flag, or approval for a new task, in accordance with the bot
> approval process, please input your bot's name into the box below, followed by
> the task number if your bot is already approved for other tasks. Then transclude
> that page onto this page, like this: `{{Wikidata:Requests for permissions/Bot/RobotName}}`."

Mechanically: an input box creates the subpage
`Wikidata:Requests for permissions/Bot/<RobotName>` from a preload template (a
ready-made request form), then you transclude that subpage onto the board. Old
requests are moved to the archive.

## Decision and flag

> "Once consensus is obtained in favor of granting the botflag, please post
> requests at the bureaucrats' noticeboard."

So: discussion + community consensus on the request → posted at the bureaucrats'
noticeboard → a **bureaucrat grants the flag**. Per the bot policy, the request
must be opened **before** the 50–250-edit test run, and an admin/bureaucrat closes
it once the trial succeeds.

## Relevance to this project

This is the concrete venue if `write.py` graduates to unattended bulk editing:
separate "…bot" account → open a request subpage here (before testing) describing
the task → 50–250-edit trial → consensus → bureaucrats' noticeboard → flag. The
*what you must satisfy* lives in `bots.md`; this page is just the procedural front
door. Until then, individually-curated edits stay on the manual side of the line.

## Related links

- [Wikidata:Bots](https://www.wikidata.org/wiki/Wikidata:Bots) — approval criteria; see `bots.md`
- [Wikidata:Bureaucrats' noticeboard](https://www.wikidata.org/wiki/Wikidata:Bureaucrats%27_noticeboard)
- [Wikidata:Requests for permissions/Archive](https://www.wikidata.org/wiki/Wikidata:Requests_for_permissions/Archive)
- [Wikidata:Requests for comment/Mass-editing policy](https://www.wikidata.org/wiki/Wikidata:Requests_for_comment/Mass-editing_policy) — see `mass-editing-policy.md`
