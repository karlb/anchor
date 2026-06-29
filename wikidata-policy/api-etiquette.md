# API:Etiquette (and maxlag)

- Source: https://www.mediawiki.org/wiki/API:Etiquette
- Retrieved: 2026-06-29 (from raw wikitext)
- Status: MediaWiki Action API best practices (referenced by the bot policy and the mass-editing RfC).

## Nutshell

- "Set an informative **User-Agent** string with contact information, or you may
  be IP-blocked without notice."
- "Make your requests **in series rather than in parallel**, by waiting for one
  request to finish before sending a new request."
- "Perform grouped updates via one single `editEntity`, for all languages and for
  labels, descriptions, and aliases at once." (Wikibase-specific advice.)

## Request limit

There is **no hard speed limit on reads**, "but be considerate and try not to
take a site down" — admins may block you if you endanger stability. Serial
requests "should result in a safe request rate." Also fetch in bulk: pipe-join
titles (`titles=PageA|PageB|PageC`), use generators, and `Accept-Encoding: gzip`.

**Writes are rate-limited.** The exact limit depends on the action, your rights,
and the wiki. Check yours via:

```
action=query&meta=userinfo&uiprop=ratelimits
```

When you hit it you get error code **`ratelimited`**; you may retry but "should
increase the time between subsequent requests. A common strategy for this is
Exponential backoff."

## maxlag

For non-interactive tasks (nobody waiting on the result), use the **`maxlag`**
parameter — "an integer number of seconds." The page's example uses `maxlag=1`.

> "Higher values mean more aggressive behaviour, lower values are nicer."

It "will prevent your task from running when the load on the servers is high"
(the API returns a maxlag error and you back off). The page defers the
recommended value to *Manual:Maxlag parameter*; **`maxlag=5` is the value
commonly used by Wikidata write-bots** (convention, not stated on this page).

## User-Agent

> `User-Agent: clientname/version (contact information e.g. username, email) framework/version...`

Don't copy a browser's User-Agent — a descriptive one makes problems traceable.
From browser JS, where you can't set User-Agent, use the `Api-User-Agent` header.
On Wikimedia wikis a meaningful User-Agent is **required**.

## Other practices

- Prefer **GET over POST** for reads (POST isn't cacheable and may hit a farther
  datacenter); in rare POST-for-read cases set `Promise-Non-Write-API-Action: true`.
- Use **JSON** output; **cache** data you'll reuse.
- Wikimedia wikis also apply separate API rate limits and the WMF User-Agent
  policy.

## Relevance to this project

For `write.py`: issue edits **serially** (await each before the next); set
`maxlag` (5 by Wikidata convention) and **back off on a `maxlag`/`ratelimited`
error** with exponential backoff; batch label/description/alias/sense changes
into one `editEntity`/`wbeditentity` write where possible; and send a descriptive
**User-Agent with a contact** so a problem gets an email, not a silent IP block.

## Related links

- [mw:Manual:Maxlag parameter](https://www.mediawiki.org/wiki/Manual:Maxlag_parameter)
- [mw:Manual:Rate limits](https://www.mediawiki.org/wiki/Manual:Rate_limits) · [mw:Wikimedia APIs/Rate limits](https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits)
- [mw:API:Assert](https://www.mediawiki.org/wiki/API:Assert)
- [Wikimedia Foundation API Usage Guidelines](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_API_Usage_Guidelines) · [User-Agent policy](https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy)
- [Wikidata:Bots](https://www.wikidata.org/wiki/Wikidata:Bots) — see `bots.md`
