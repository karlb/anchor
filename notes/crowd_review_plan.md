# Crowdsourcing the human review: Distributed Game plan

**Status: LIVE.** Plan written 2026-07-07; phases 0–2 shipped. The game runs on Karl's server,
`game_export.py` publishes the feed, `import_crowd.py` pulls votes into
`notes/runs/crowd_decisions.csv`, scores voters into spammer/normal/reliable tiers off the trap
tiles, and promotes consensus into `decisions.csv` with a `crowd:<names>` reviewer that survives
into the edit summary as approver credit. Refuted pairs are report-only. Real voters have earned
reliable status and real pairs have been promoted and written.

**What is still open is phase 3** — the measurement that decides whether the crowd can be trusted
to promote without a per-batch human sign-off: is trap accuracy good enough, is k=2 right, which
classes (if any) graduate, and how does the crowd compare with Karl on a double-labelled subset.
Sections 1–7 below are a record of the design as built; §8 phases 3–4 and §9 are the live parts.

Goal: move the bulk confirm/reject work now done one person at a time in
`review_queue.py` out to volunteer reviewers, without giving up the two things the
current pipeline gets right: a single append-only decision log that authorizes
writes, and calibration data that tells us which suggestion classes are trustworthy.
A hard secondary goal: every crowd interaction is logged richly enough to later
optimize suggestion generation against it (prompt A/Bs, gate tuning, ranking).

## 1. Platform choice: Distributed Game, not a MachtSinn fork

Two existing vehicles were evaluated (verified 2026-07-07):

**Wikidata Distributed Game** (wikidata-game.toolforge.org/distributed/, Magnus
Manske). The hub hosts no games itself — it aggregates "tile providers": any web
API implementing a three-action protocol. Protocol confirmed from a live provider
implementation (google/wikiloop-wikidata-game):

- `?action=desc` → static JSON: multilingual `label`, `description`,
  `instructions`, `icon`.
- `?action=tiles&num=N&lang=xx` → `{tiles: [{id, sections: [...], controls:
  [{type:'buttons', entries: [...]}]}]}`. Section types include
  `{type:'item', q:'Q123'}` (hub renders the item) and `{type:'html', title, text}`
  (arbitrary content — what we'd use for the lexeme side, since item-rendering of
  L-entities is unverified). JSONP (`callback=` param) required.
- `?action=log_action&user=...&tile=<id>&decision=yes|no|skip` → hub calls us back
  with every player decision. We answer with a status blob.
- Button entries: `{type:'green'|'blue'|'white', decision, label, api_action?}`.
  **`api_action` is optional.** With it, the hub executes the given Wikidata API
  call (e.g. `wbeditentity` + JSON data) under the *player's* OAuth. Without it,
  the click is log-only. This is the hinge of the whole design: we can run a
  votes-only game with zero edit rights.

**MachtSinn** (github.com/Nudin/makesense, tools.wmflabs.org/machtsinn/). Flask +
MySQL on Toolforge; generates its own candidates by same-language lemma==item-label
matching; on accept it creates a new sense (gloss = item description) + P5137 under
the player's OAuth. It is conceptually our senseless lane, but: candidates are
baked in by its own SQL (no injection API — using it means forking the app and
running our own OAuth consumer, UI, and hosting), and its matching is
same-language, so it can't carry our cross-lingual translation-anchored
suggestions anyway.

**Decision: build a Distributed Game tile provider.** We write only a thin API;
the hub supplies UI, player accounts, OAuth, edit execution, and an existing
player base. MachtSinn remains relevant as (a) prior art for tile wording and
sense-creation UX, and (b) a dedup concern for the senseless lane (its pending
pool overlaps same-language label matches; our existing pre-write re-check of the
live lexeme in write.py already defuses double-writes).

Fallback if the hub turns out moribund (registration broken, no players): fork
MachtSinn's Flask/OAuth skeleton and serve our feed through it. Everything below
except §5's protocol details survives that switch unchanged — that is what the
boundary is for.

## 2. The boundary

Two artifacts, one in each direction, both versioned, nothing else crosses:

```
  generation side (this repo, offline)          review side (own server, online)
 ┌──────────────────────────────────┐          ┌──────────────────────────────┐
 │ match → gates → verify → hints   │  feed    │  dumb tile spooler:          │
 │ game_export.py ─────────────────────────────▶  serve tiles, record votes   │
 │                                  │ (jsonl)  │  (no P5137 semantics at all) │
 │ import_crowd.py ◀───────────────────────────── vote log export             │
 │   → crowd_decisions.csv          │  votes   └──────────────────────────────┘
 │   → promotion → decisions.csv    │
 └──────────────────────────────────┘
```

Principles:

- **The service knows no P5137 semantics.** Tiles are fully pre-rendered at export
  time — sections, button entries, and (in Mode A) the exact `api_action` payload
  are baked into the feed. The service pages through opaque tiles, enforces
  serving policy, and records votes. Every substantive change (wording, hints,
  which classes ship, edit payloads) is a generation-side change, shipped as a new
  feed version, diffable in this repo.
- **Suggestion generation ends at the published feed.** Matching, gating,
  verification, scope hints, lane/mode assignment, tile rendering: all here, all
  regenerable, all attributable to a `feed_version` + git commit.
- **Review ends at the vote log.** Aggregation (k-votes), promotion into
  `decisions.csv`, and any write remain generation-side, so the existing
  authorization chain (log-driven `write.py`, EditGroups tagging, revid capture)
  is untouched.

### Repo layout (decided 2026-07-07)

Two repos, split exactly along the boundary above:

- **`anchor` (this repo):** `game_export.py`, `import_crowd.py`, promotion logic,
  analysis. They need the caches, `gates`/`review_queue` composers, and
  `decisions.csv`, and they change with the pipeline.
- **Service repo (new, small, public):** the tile spooler only. (Update
  2026-07-07: hosting moved from Toolforge to Karl's own server — the hub only
  needs a public HTTPS URL; wikiloop's games ran on Google Cloud, so there is
  precedent. That drops the Toolforge open-source-policy argument for the
  split, but the repo stays separate and public: it enforces "no P5137
  semantics" by construction — the service cannot import repo helpers, only
  read the feed — and volunteers can inspect what the tool does with their
  clicks.)

Schema-drift mitigation: the service repo owns the contract (feed + vote-log
schema doc and a small JSON Schema); `game_export.py` validates every feed
against that schema at export time. Producer asserts, consumer owns the spec.

## 3. Suggestion feed (generation → review)

`game_export.py` (new) writes `data/game_feed/feed_v<NNN>.jsonl.gz`, uploaded to
the server. One row per suggestion:

```json
{
  "sid": "L476208|Q3686031|v1",         // stable id: lid|qid|tile-schema — the log_action tile id
  "lane": "mono",                        // mono (add P5137) | senseless (create sense) — pilot: mono only
  "mode": "B",                           // B = log-only buttons, A = yes-button carries api_action
  "class": "EQUAL",                      // rel_class() output: the calibration bucket
  "tile": { "sections": [...], "controls": [...] },   // final tile JSON, hub-ready
  "gold": null,                          // or "approved"/"rejected" — trap tile from decisions.csv (§6)
  "meta": { ... }                        // optimization snapshot, §7 — opaque to the service
}
```

Pool = exactly what `review_queue.pool()` serves today (verifier-'yes' High tier
via `gates.high_tier_where`), minus decided pairs (`config.DECISIONS` replay),
minus auto-defer classes, minus the classes reserved for expert review (§6).
Tile content (amended 2026-07-07): lemma + CC0 sense gloss (`demonym_context`)
as an `html` section, an `item` section for the anchor Q — the hub frontend
renders the item in detail, so the earlier multilingual CC0 concept block was
dropped as redundant — and an optional Notes section carrying only
DETERMINISTIC hints: the neutral alias hit (`alias_match`; a target-language
comparison the frontend won't compute) and the convention note on `·convention`
tiles (task definition: without it honest players mark gendered/demonym forms
"no match" against the blessed convention). The verifier's free-text reason is
deliberately NOT shown: every live tile is verifier-'yes', so the reason always
argues "match" — showing it anchors players toward yes and corrupts the §7
calibration signal, and on rejected traps the relation token would leak the
expected answer. Notes derive from class/data, never from `gold`, so traps stay
indistinguishable. Buttons: **match** (green/yes), **no match** (blue/no),
**can't tell** (white/skip). Instructions text explains P5137 ("the sense means
exactly this concept") with two worked examples, per language.

A new feed version fully replaces the old (same sids persist for undecided pairs;
decided/withdrawn ones drop out). The service keeps accepting `log_action` for
sids no longer in the feed and marks them `stale` — votes are never dropped.

## 4. Review service (own server)

Runs on Karl's server behind HTTPS (hub pages are HTTPS, so a plain-HTTP
provider is mixed content and gets blocked; a stable URL is the game's
registered identity). Mode B holds no Wikimedia credentials — even Mode A edits
are executed by the hub under the player's OAuth. Python + SQLite — the same
stdlib-first style as `review_queue.py`; Flask only if JSONP/routing gets
annoying. The vote DB is volunteers' donated work: back it up, and run
`import_crowd.py` pulls on a schedule from day one.

Tables: `suggestion(sid, lane, mode, class, gold, tile_json, feed_version,
first_served, n_served)` and `vote(sid, user, decision, ts, feed_version,
stale)` (append-only; PK `(sid, user)` — a player's repeat click updates their own
vote only, mirroring wikiloop's dedup).

Serving policy (the only "logic" the service owns):

- Never serve a sid the requesting flow has already shown this session; prefer
  sids with fewest votes; stop serving a sid once it reaches k votes or a
  terminal state; honor `lang` by matching the suggestion's target language,
  falling back to any (wikiloop does the same).
- Interleave trap tiles (`gold != null`) at ~10–15%, indistinguishable from live
  tiles.
- `/export?since=<ts>` → the vote log as JSON, for `import_crowd.py` to pull. No
  auth needed (votes are public-by-nature; the hub shows them in recent changes
  anyway).

## 5. Edit execution: Mode B first, Mode A by graduation

- **Mode B (log-only)** — all buttons lack `api_action`. Votes accumulate; k-vote
  consensus is computed generation-side; writes happen through the existing
  `write.py` path (bot account, EditGroups `CB` tagging, revid appended to
  `decisions.csv` as `written`). Start here: no edit rights needed, no risk from a
  single confused player, and every write stays inside the current authorization
  chain. Cost: a player's click doesn't visibly edit, which the instructions must
  say plainly ("your answers are compared against other players' and applied in
  batches — see [batch link]").
- **Mode A (direct edit)** — the yes-button carries `api_action` (`wbeditentity`
  on the L-id adding the P5137 claim to the sense; senseless lane would add the
  sense + claim in one call). Player's account, instant gratification,
  single-vote. Graduate a `class` from B to A only when its crowd record clears
  the same pinned calibration philosophy as auto-approve (§7): trap-tile accuracy
  and crowd-vs-Karl agreement both above threshold on ≥ GATE_MIN_SAMPLES. Until
  then Mode A is off for everything.

k for Mode B: start k=2 agreeing votes to conclude (2 yes → confirmed, 2 no →
refuted); a 1–1 split takes a third vote; majority-of-3 else `contested`. Skip
votes never count toward k. Revisit k with the Phase-3 agreement data.

## 6. Division of labor: crowd vs expert queue

The crowd gets the *confirmation* work; the scope-triage judgment stays local.

- **To the game:** classes whose suggested action is `keep` — `EQUAL`,
  `BROADER·convention`, `NARROWER·convention` — and no polysemy-collapse flag.
  This is the high-volume, low-ambiguity slice where a yes/no framing is honest.
- **Stays in `review_queue.py` (Karl):** defer-suggested classes (`BROADER`,
  `NARROWER`, `LATERAL`, `INCOHERENT`, `demonym-ethnic`), polysemy-collapse
  flagged pairs, the whole senseless lane initially (sense creation is
  higher-stakes: gloss text + first-sense choice), and everything the crowd
  returns as `contested` or as a refuted-but-trap-suspicious case. The local
  queue becomes the appellate court, not the assembly line.
- **Trap tiles:** ~10–15% of served tiles are pairs already decided in
  `decisions.csv` (both approved and rejected ones). They measure per-user and
  per-class crowd accuracy, are excluded from promotion, and their known verdict
  never leaves the generation side (the service stores `gold` only so the
  importer doesn't have to re-join).

## 7. Return path and the data we keep for optimization

`import_crowd.py` (new) pulls `/export`, appends raw votes to
`notes/runs/crowd_decisions.csv` (append-only, same discipline as
`decisions.csv`, but a separate file — raw crowd votes must never masquerade as
the authorizing log). Columns: `ts, sid, target_lid, anchor_qid, user, decision,
feed_version, stale, is_gold`.

**Promotion:** consensus results become ordinary `decisions.csv` events with
`reviewer='crowd:k2'` (the column exists for exactly this), `status`
approved/rejected — which is what `write.py` and `calibrate.py` already consume.
During the pilot, promotion is gated on a human batch sign-off:
`import_crowd.py --promote` prints the batch (lemma, anchor, votes, trap-accuracy
context) and Karl confirms once per batch, not once per pair. Automatic promotion
per class is a later graduation, decided by the same gate style as
`calibrate.py`'s pinned thresholds. Crowd-refuted pairs land as `rejected` but
remain re-adjudicable via the established `recheck` append convention.

**The optimization snapshot** (`meta` in the feed, denormalized so analysis never
needs the regenerable DBs at the feed's git commit):

- provenance: `feed_version`, git commit, tile schema version, export ts;
- gates & evidence: tier bits (clean/A/B/D), `support_clusters`, n anchor
  languages present, `alias_match` hit (bool), `polysemy_hint` n;
- verifier: verdict, relation token, `rel_class`, reason text, model, prompt
  version, sample count;
- pair: target lang, lemma, `n_senses`, gloss length / `undifferentiated_gloss`.

Joined with votes this answers, per class × language × any meta dimension:
crowd accept rate, skip rate (difficulty signal), contested rate, per-user trap
accuracy, votes-to-consensus, serve→vote latency. Concrete consumers, in order
of expected value:

1. **Calibration:** crowd-vs-Karl agreement on trap tiles + the promoted classes'
   precision feed the existing `calibrate.py` readiness table — crowd consensus
   becomes a second, much cheaper label source for the pinned auto-approve gates.
2. **Disagreement mining:** every (verifier-keep, crowd-refuted) and every
   contested pair is a candidate row for `notes/problematic_cases.csv` — the gold
   set grows from real failures instead of hand-curation.
3. **Prompt/gate A/Bs at scale:** score_approaches-style comparisons can grade
   against accumulated crowd consensus (thousands of labels) instead of only the
   119-case gold — with trap-accuracy weighting so low-quality voters don't skew
   the target.
4. **Ranking:** skip/contested rates by meta dimension tell us which suggestion
   features predict human difficulty → serve-order and future gate tuning.

## 8. Phases

**Phase 0 — verify externals.** ✅ DONE. The hub is live and self-serve; the fallback was not
needed.

**Phase 1 — exporter (repo side).** ✅ DONE: `game_export.py` (schema doc = its docstring until
the service repo exists; always-on per-row contract check instead of a separate test; stdlib
only). Reuses the queue composers, so crowd tiles show exactly what the local queue shows. One
data-driven amendment to §6: keep-class *rejections* are far too rare to catch yes-spammers (the
log is almost unanimously 'approved' on keep classes), so rejected traps are sourced from ANY
class — the human's confident 'rejected' is the gold label, the class is invisible in the tile,
and the importer buckets trap accuracy by class so judgment-call rejections (BROADER/LATERAL)
don't over-penalize good-faith players. Traps split roughly half rejected / half approved.

**Phase 2 — pilot (service side).** ✅ DONE. Service live on the server, Mode B, k=2, de/en/sv
first. `import_crowd.py` scores each voter off the traps (spammer / normal / reliable), promotes
consensus into `decisions.csv` under a `crowd:<names>` reviewer with approver credit in the edit
summary, and treats refutations as report-only rather than as rejections.

**Phase 3 — measure, then decide.** ⬅ **the open phase.**
An analysis pass over §7's metrics. Decisions due: is trap accuracy good enough to trust
promotion without the per-batch sign-off (target: at least what the High tier already delivers)?
Right k? Which classes (if any) graduate to auto-promotion? Plus a full Karl-vs-crowd comparison
on a double-labelled subset. Until this lands, promotion keeps its human batch sign-off — which
is the safe default, not a blocker.

**Phase 4 — scale and extend.**
Full keep-class pool across all REVIEW_LANGS; auto-promotion for gated classes;
consider Mode A for graduated classes; design the senseless-lane tile (sense +
gloss creation — MachtSinn UX as prior art) as its own follow-up plan.

## 9. Risks / open questions

- ~~**Hub liveness.**~~ Settled — the hub works and the game runs. **Throughput** is still the
  real risk: lexeme games are niche, and if weekly vote volume is too low for k=2 to converge,
  that is itself a cheap, useful negative result — the exporter and vote-log design carry over
  to any other review frontend (including a public copy of `review_queue.py` with OAuth).
- **Crowd quality on P5137 subtleties.** The keep-class restriction plus trap
  tiles plus promotion sign-off bound the damage; the real answer arrives with
  Phase-3 data. Do not widen to defer-classes on optimism.
- **Vote gaming / vandalism.** k-votes must come from distinct users; per-user
  trap accuracy discounts or excludes bad actors at import time (generation-side
  policy, so it can be changed retroactively over the raw log).
- **Attribution & consent.** Player names are public in hub recent-changes
  already; we store them raw in the vote log but publish only aggregates.
- **BRFA interaction.** Mode B writes stay batch writes under our account —
  the pending bot-approval milestone and the write.py User-Agent follow-up apply
  unchanged; crowd provenance (`reviewer='crowd:k2'`) should be mentioned in the
  BRFA text as a strengthening factor, not hidden.
- **Open:** exact hub registration flow; whether `log_action` includes the
  player's language/UI locale (affects per-language quality splits); whether the
  hub throttles `tiles` requests (affects serving-policy design); the service's
  public hostname/URL (its registered identity — pick one that can stay).
