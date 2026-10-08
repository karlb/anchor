#!/usr/bin/env python3
"""LLM yes/no verifier for P5137 candidates — CC0 inputs only.

For each candidate ``(target lexeme L, anchor Q)``, the model is asked a single
constrained question: does L's specific sense — shown via the lexeme's own CC0
Wikidata GLOSS when it has one — denote the concept identified by Q's **CC0**
label + description? The answer is a verdict (yes / no / uncertain) plus a
one-line reason — never copied text. The gloss disambiguates homographs the bare
lemma cannot ('sheet'=paper vs the nautical line, 'hov'=court vs hoof); the
criterion is TOLERANT (broader/narrower still count as yes, only a genuinely
different concept is 'no'). Validated by a blind 2×2 + 3-sample vote on the gold
set: gloss+tolerant scored 100% keep and caught every genuine homograph, beating
the lemma-only strict verifier — see memory ``sense-gloss-matching``.

A gloss is attached only when L is MONOSEMOUS (one sense); a candidate is
lemma-level (no sense id), so for a polysemous lexeme we can't know which sense
the cluster grounds and fall back to the bare lemma. Senseless targets (n=0) have
no gloss and use the lemma route too.

License: every input is CC0 — Q's own label/description (Wikidata→Wikidata), the
bare lemma, and L's own Wikidata sense gloss (also CC0). No DBnary gloss is ever
read (those are CC-BY-SA).

Backend: the local ``claude`` CLI in print mode (reuses your existing auth, no
API key). ``llm.batch_json`` runs it with ``--disallowedTools "*"``, which drops the
~17.6k-token tool-schema context the CLI otherwise reloads every call (this is a pure
text->JSON task that needs no tools; measured ~19k -> ~0.3k/call). Candidates are still
**batched** (default 8/call) to amortise the ~1.2k system prompt. Verdicts are cached in
``data/verify.sqlite3`` keyed by (target_lid, anchor_qid) — re-runs skip done work, but a
changed prompt (tracked by its SHA1 fingerprint) auto-invalidates stale verdicts so they
are re-verified rather than silently trusted.

Run after fetch_items.py (defaults to a 40-candidate sample):
    uv run verify.py                       # the highest-support clean candidates
    uv run verify.py --senseless-only      # only candidates grounding senseless lexemes
    uv run verify.py --all --batch 8       # full run (costs add up — see note above)
    uv run verify.py --gate ABD --all --model sonnet   # the High tier
    uv run verify.py --gate AD --senseless-only --sample 240 --seed s1   # measure a lane
"""

import argparse
import collections
import csv
import hashlib
import sqlite3

import config
import gates
import llm

# Production verifier feeds the concept's MULTILINGUAL CC0 descriptions (the +multi format,
# config.CONCEPT_LANGS_VERIFY from item_lang). This guide tells the model to read the
# language-tagged fields as ONE concept — without it a wide prompt mis-reads terser translations
# as scope evidence (over-equation). Validated to beat the de/en/sv base format on the 118-case
# gold once paired with the demonym→ethnic-group scope_hint nudge (see review_queue.scope_hint).
MULTILINGUAL_GUIDE = (
    "MULTILINGUAL CONCEPT FIELDS: the concept's label and description may be shown in several "
    "language-tagged fields separated by ' | ' or ' / '. These are translations of ONE concept "
    "— read them together as a single meaning, not as independent claims. Do NOT treat a terser, "
    "looser, or differently-phrased translation as evidence of broader/narrower scope; weigh scope "
    "only from the clearest, most specific description. Flag INCOHERENT only when translations name "
    "genuinely DIFFERENT referents (e.g. a debt vs. a lawsuit), never for mere variation in detail "
    "or completeness across languages. "
)
# "DESCRIPTION OVER LABEL" clause (added 2026-06-27): the verifier was accepting label-collision
# homographs whose gloss ALSO collides — e.g. de 'Achtzigstel' (the fraction 1/80) glossed
# 'eightieth' vs the ordinal '80th', both surfacing as the English word 'eightieth'. Gloss-tolerance
# can't catch these (the gloss matches too), so the clause tells the model to weigh the concept's
# DESCRIPTION above a bare label match and reject when the described KIND differs. Validated lean on
# the 119-case gold (samples=1): fixes Achtzigstel via correct 'fraction vs ordinal' reasoning,
# aggregate in-set/false-keep unchanged vs +multi (see notes; no samples=3 confirm run).
SYSTEM = (
    "You are a lexicographer auditing Wikidata P5137 sense links. For each item you are given a "
    "NOUN, its LANGUAGE, sometimes the word's own dictionary GLOSS (a short definition of the ONE "
    "specific sense being linked) or, for a word with recorded senses, its NUMBERED SENSES (one or "
    "more) with their glosses, and a candidate concept described by a Wikidata CC0 label and "
    "description. "
    "Use the GLOSS as the primary signal for WHICH sense of the noun is meant. If no gloss is shown, "
    "or it is a bare one-word synonym or written in an unexpected language, fall back to judging from "
    "the noun itself and the concept. "
    "When NUMBERED SENSES are shown, first decide which numbered sense the concept is the "
    "item-for-this-sense of, allowing ordinary translation and register differences and the concept "
    "being somewhat wider or narrower than the gloss; answer the sense NUMBER. Answer \"none\" when "
    "the concept denotes no listed sense — a meaning of the noun that is not among those listed, a "
    "homograph, or an unrelated concept; prefer \"none\" over forcing a poor fit, a wrong assignment "
    "is worse than an abstention. Then classify the relation for THAT sense. After \"none\" the "
    "verdict is about the NOUN: if the noun has a meaning the concept denotes that simply is not "
    "among the listed senses, that is still \"yes\" (EQUAL when it denotes exactly that) — the "
    "sense is MISSING from the list, not different; only a homograph-level or unrelated concept is "
    "DIFFERENT. Example: English 'passage' senses (1) section of text (2) advance of time, vs "
    "concept 'passageway between two buildings' -> sense none, EQUAL -> yes (the noun has that "
    "meaning; no listed sense records it). "
    "Classify how that sense relates to the concept: "
    "EQUAL (the sense denotes that concept — exact synonyms and translation/register differences "
    "still count as EQUAL); "
    "BROADER (the sense is a hypernym — wider than the concept); "
    "NARROWER (the sense is a hyponym — more specific than the concept); "
    "LATERAL (the right reading of the word, but the concept is ADJACENT rather than a kind of / "
    "wider than it — a process vs. the system or product that performs it, an attribute or facet "
    "vs. the whole, or two sibling concepts); "
    "INCOHERENT (the CONCEPT ITSELF is self-contradictory — its label, description, and "
    "translations disagree about what it denotes — so there is no coherent basis to judge the "
    "relation at all; flag this only for a real contradiction between the given fields, never for "
    "mere sparseness); "
    "DIFFERENT (a genuinely different concept: a wrong sense, homograph, or false friend). "
    "Judge by MEANING, not wording: a different synonym, register, or translation is NOT a mismatch. "
    "DESCRIPTION OVER LABEL: a shared label or word-form is not enough for EQUAL — the concept's "
    "DESCRIPTION is the authoritative statement of what it denotes, so weigh it above a bare label "
    "match. When the noun's sense and the concept share a label but the description shows a different "
    "KIND of referent — a different category, type, or unit (an ordinal number vs a fraction, an act "
    "vs its result, a quantity vs an operation) — that is DIFFERENT (or LATERAL if merely adjacent), "
    "not EQUAL. Do NOT infer a different kind from a merely terse or partial description. "
    "GENDER RULE: a gendered personal noun — ANY feminine or masculine person term, not only "
    "demonyms and occupations but also ordinary common nouns like 'schoolgirl'/'schoolboy', "
    "'waitress', 'widow' — denotes the SAME concept as its gender-neutral counterpart; on Wikidata "
    "the word's sex is recorded separately (semantic gender), NOT by this concept link. So a pure "
    "male/female difference at the same level is EQUAL, NOT NARROWER. "
    'Map the relation to a verdict (TOLERANT — surface every plausible candidate for human review '
    'even when scope or category differs): "yes" for EQUAL, BROADER, NARROWER, LATERAL, or '
    'INCOHERENT (an incoherent target still goes to a human, who will defer it); "no" ONLY for '
    'DIFFERENT (a clearly different concept); "uncertain" only if the gloss and description are '
    "both too sparse to classify. "
    "Examples (yes): English 'car' vs 'automobile' -> EQUAL -> yes. German 'Taenzerin' vs 'dancer' "
    "-> EQUAL (gender recorded separately) -> yes. German 'Bosnier' glossed 'male person from "
    "Bosnia' vs 'Bosnians' -> NARROWER -> yes. Swedish 'bergsbestigare' (mountain climber) vs "
    "'mountaineer' -> EQUAL -> yes. English 'schoolgirl' glossed 'young female student' vs "
    "'schoolchild' -> EQUAL (gender recorded separately, not narrower) -> yes. English 'huntsman' "
    "glossed 'hunts with weapons' vs 'hunter' -> NARROWER -> yes. "
    "Examples (lateral, still yes): English 'manner' glossed 'mode of action' vs 'behavior' -> "
    "LATERAL (a way of acting, not a kind of behavior). English 'facial recognition' glossed "
    "'identification of human faces' vs 'facial recognition system' -> LATERAL (the process vs. "
    "the system that runs it). "
    "Example (incoherent): Swedish 'anspraak' (demand/expectation) vs a concept whose English "
    "label says 'cause of action' but whose Swedish form is 'Fordran' (a debt) -> INCOHERENT (the "
    "target conflates two different concepts, no basis to judge) -> yes (surface to defer). "
    "Examples (no): English 'sheet' glossed 'a piece of paper' vs concept 'one of the lines on a "
    "sail ship' -> DIFFERENT (homograph) -> no. Swedish 'hov' glossed 'group of people surrounding a "
    "leader' vs concept 'hoof' -> DIFFERENT -> no. Swedish 'bit' glossed 'liten del' (small part) vs "
    "concept 'unit of information in computing' -> DIFFERENT -> no. German 'Achtzigstel' (the "
    "fraction 1/80) glossed 'eightieth' vs a concept labelled 'eightieth' whose description reads "
    "'ordinal number for 80' -> DIFFERENT (label coincides, but a fraction is not an ordinal) -> no. "
    + MULTILINGUAL_GUIDE +
    # STRUCTURED REPLY (2026-08-16, notes/redo.md #3). The triage layer used to substring-scrape
    # this ten-word prose field for words like "female" and "ethnic" to decide keep-vs-defer, so a
    # model paraphrase silently changed the action. Worse, it read the SAME token two ways: the
    # senseless lane has no target gloss, so the demonym→ethnic-group downgrade could not fire and
    # the word "ethnic" in the prose reached the *keep* convention instead — measured, and the
    # biggest single error class in that lane (notes/senseless_lane_findings.md). The three
    # booleans below are the fields the code actually branches on, asked for directly. The one
    # that matters is anchor_is_ethnic_group: Wikidata types nationality demonyms (Spaniards,
    # Guineans) as P31=ethnic group as well, so no deterministic rule over P31 or the description
    # can separate the contested case from the blessed one — it takes a judgment, which is why it
    # is asked of the model rather than computed.
    "Then answer three FACTUAL questions about this item, as JSON booleans: "
    '"gendered_form" — is the NOUN a gender-specific form (feminine or masculine) of a '
    "gender-neutral counterpart? "
    '"demonym" — does the NOUN denote a person by their origin, nationality, ethnicity or region '
    "(a Swede, a Bavarian, a Sami)? "
    '"anchor_is_ethnic_group" — is the CONCEPT an ethnic group, people or community defined by '
    "shared descent, culture or history (the Sami, the Romani, the Aztecs)? Answer false when the "
    "concept is instead the citizens, nationals or residents of a state (the Spaniards as "
    "nationals of Spain, 'citizens or residents of Iran'), even if it is loosely called a people; "
    "and false whenever the concept is not a human group at all. "
    'Reply with ONLY a JSON array (no prose, no code fences), one object per item in order: '
    '[{"i":1,"sense":2,"verdict":"yes|no|uncertain","relation":"EQUAL|BROADER|NARROWER|LATERAL|'
    'INCOHERENT|DIFFERENT","gendered_form":false,"demonym":false,"anchor_is_ethnic_group":false,'
    '"reason":"<=10 words"}] — "sense" is the chosen sense number, the string "none", or null '
    "when no numbered senses were shown."
)


def parse_sense(raw, senses):
    """The cache's target_sid from the model's `sense` field: an S-id, 'none', 'out_of_range',
    or None when the item offered no numbered senses (mono/senseless rows)."""
    if not senses:
        return None
    if isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit()):
        idx = int(raw)
        return senses[idx - 1][0] if 1 <= idx <= len(senses) else "out_of_range"
    return "none" if str(raw).strip().lower() == "none" else "out_of_range"

# The reason string the cache stores, composed by US from the model's fields — not free prose.
# Shape: "<REL>·<tag>·<tag>: <=10 words", with the '·plain' tag when no flag is set, so a
# structured row is always distinguishable from a legacy one by having any tag at all. '·' is
# already this project's class-token separator (rel_class's '·convention', '·sole-anchor').
FLAG_TAG = {"gendered_form": "gendered", "demonym": "demonym",
            "anchor_is_ethnic_group": "ethnic-anchor"}


def compose_reason(v, offered=False):
    """'<REL>·<tags>: <prose>' from one structured verdict object.

    Falls back to the model's own reason when there is no relation field at all, which is what a
    parse failure or an older reply shape produces — those stay legacy-shaped and the triage layer
    keeps scraping them, so a bad batch degrades to the old behaviour instead of to no relation.

    `offered`: numbered senses were shown, so a "none" answer is meaningful and is tagged
    ·missing-sense — the token the triage maps to the CREATE action (review_queue.REL_ACTION).
    It rides in the reason, not only in target_sid, because every policy consumer (queue sort,
    status precision, calibrate, auto-defer) reads the reason head and none reads the sid."""
    rel = (v.get("relation") or "").strip().upper()
    prose = " ".join((v.get("reason") or "").split())
    if not rel:
        return prose
    tags = [t for f, t in FLAG_TAG.items() if v.get(f) is True]
    if offered and rel != "DIFFERENT" and str(v.get("sense")).strip().lower() == "none":
        tags.append("missing-sense")     # DIFFERENT + none is an unrelated concept, not a gap
    return "·".join([rel] + (tags or ["plain"])) + ": " + prose


# Fingerprint of the prompt: a changed SYSTEM yields a new PROMPT_VER, and every read filters on
# the current one — so a prompt edit stops the old verdicts being trusted without destroying them
# (they stay in the cache as their own version, which is what makes a prompt change an A/B). The
# cost is that the pool has to be re-verified at the new prompt before the queue is whole again.
PROMPT_VER = hashlib.sha1(SYSTEM.encode()).hexdigest()[:8]

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict(
    target_lid TEXT, anchor_qid TEXT, target_lemma TEXT, target_lang TEXT,
    verdict TEXT, reason TEXT, model TEXT, prompt_ver TEXT,
    -- poly targets only: the S-id the verifier chose, 'none' (the concept is a meaning the
    -- lexeme has but no listed sense records — the create_add signal), or 'out_of_range'.
    -- NULL for mono/senseless rows and for every row verified before 2026-09-03.
    target_sid TEXT,
    PRIMARY KEY (target_lid, anchor_qid, prompt_ver)
) WITHOUT ROWID;
-- --samples N: every sample, kept. The verdict row above then holds their MAJORITY, so no reader
-- changes. n = -1 is the single-sample verdict the vote replaced, kept for comparison.
CREATE TABLE IF NOT EXISTS sample(
    target_lid TEXT, anchor_qid TEXT, prompt_ver TEXT, model TEXT, n INT,
    verdict TEXT, reason TEXT, target_sid TEXT, model_id TEXT,
    PRIMARY KEY (target_lid, anchor_qid, prompt_ver, model, n)
) WITHOUT ROWID;
"""

# The cache is ADDITIVE in the prompt version: one row per (pair, prompt_ver), and a prompt
# change adds rows instead of destroying them. It used to be keyed on the pair alone, so every
# SYSTEM edit orphaned the whole cache and cost a full paid re-verify — which is why prompt
# improvements kept being deferred to "the next revision that happens anyway" (notes/redo.md #4).
# Keeping both versions makes a prompt change an A/B by construction: the old verdicts are still
# there to compare against, and validate_prompt can grade two versions out of the one file.
#
# The cost of that is a rule every consumer must follow: a pair verified under two prompts is
# now TWO rows, so any read must say which version it means or it joins/counts twice. Use
# current_ver() for "what the live prompt says" — the answer the review queue, the gates and the
# write path all want.


def current_ver(alias="v"):
    """SQL predicate restricting a verdict read to the CURRENT prompt's rows.

    A literal, not a bound parameter, so it composes into the gates.py-style SQL fragments the
    consumers already build (PROMPT_VER is a locally computed sha1 hex — nothing user-supplied
    reaches the string)."""
    return f"{alias}.prompt_ver = '{PROMPT_VER}'"


def ensure_schema(cn):
    """Create the verdict cache if absent, and migrate an older one in place (idempotent).

    Two historical shapes are handled: a pre-fingerprint cache with no prompt_ver column at all,
    and the pair-keyed cache described above. Both migrations preserve every row — the point of
    the change is that verdicts stop being thrown away."""
    cn.executescript(SCHEMA)
    for col in ("prompt_ver",     # pre-fingerprint cache
                "target_sid"):    # pre-sense-inventory cache (before 2026-09-03)
        try:
            cn.execute(f"ALTER TABLE verdict ADD COLUMN {col} TEXT")
            cn.commit()
        except sqlite3.OperationalError:
            pass  # column already present
    pk = [name for _, name in sorted((r[5], r[1]) for r in
                                     cn.execute("PRAGMA table_info(verdict)") if r[5])]
    if pk == ["target_lid", "anchor_qid"]:
        n = cn.execute("SELECT count(*) FROM verdict").fetchone()[0]
        cn.executescript("""
            BEGIN;
            CREATE TABLE verdict_new(
                target_lid TEXT, anchor_qid TEXT, target_lemma TEXT, target_lang TEXT,
                verdict TEXT, reason TEXT, model TEXT, prompt_ver TEXT,
                PRIMARY KEY (target_lid, anchor_qid, prompt_ver)
            ) WITHOUT ROWID;
            INSERT INTO verdict_new SELECT target_lid, anchor_qid, target_lemma, target_lang,
                   verdict, reason, model, coalesce(prompt_ver, '') FROM verdict;
            DROP TABLE verdict;
            ALTER TABLE verdict_new RENAME TO verdict;
            COMMIT;""")
        print(f"verify cache: re-keyed on (pair, prompt_ver); {n:,} verdicts kept")


def candidates(gate="", min_support=1, senseless_only=False, max_senses=None, poly_only=False):
    """Clean candidates with a non-missing anchor item: (lid, lang, lemma, qid, support).

    `gate` adds structural gates (the same SQL fragments gates.high_tier_where composes):
    A (>=2 anchors), B (monosemous target, n_senses==1), D (concrete in-scope anchor).
    ABD is the measured ~95.9%-precision High tier.

    The ungated pool is a strict SUPERSET of what it was before admissibility moved out of
    gates.clean_clause: measured 60,006 -> 63,275, losing none and gaining 3,269 polysemous
    rows that `NOT cross_pivot_conflict` used to exclude for having a P5137 on some OTHER
    sense. Both gated lanes (--gate ABD, --senseless-only) are byte-identical — see
    `uv run gates.py`. `max_senses` caps the target's recorded sense count for a caller that
    wants only the lanes with a write path today (None = no cap); note capping at 1 drops
    937 wholly-unlinked polysemous rows the old clause did admit, so it is a deliberate
    restriction rather than a way back to the old behaviour.
    """
    cn = sqlite3.connect(config.CANDIDATES)
    cn.execute("ATTACH DATABASE ? AS it", [config.ITEMS])
    cn.execute("ATTACH DATABASE ? AS wd", [config.WIKIDATA])   # free_sense_clause reads this
    gate = gate.upper()
    where = (gates.clean_clause("c") + " AND " + gates.admissible_clause("c", "wd")
             + " AND " + gates.gate_a(
                 "c", max(min_support, 2) if "A" in gate else min_support))
    if max_senses is not None:
        where += f" AND c.target_n_senses <= {int(max_senses)}"
    if senseless_only:
        where += " AND " + gates.create_first_clause("c")
    if poly_only:
        # The polysemous review lane (m>=2 with a free sense). Mutually exclusive with
        # senseless_only and with gate B — see gates.poly_match_clause for why it is its own
        # lane and not just the tail of the general MATCH form.
        where += " AND " + gates.poly_match_clause("c", "wd")
    if "B" in gate:
        where += " AND " + gates.gate_b("c", 1)
    if "D" in gate:
        where += " AND " + gates.concrete_anchor_clause("c", "it")
    rows = cn.execute(
        f"""
        SELECT c.target_lid, c.target_lang, c.target_lemma, c.anchor_qid, c.support_clusters
        FROM candidate c
        JOIN it.item i ON i.qid = c.anchor_qid AND NOT i.missing
        WHERE {where}
        ORDER BY c.support_clusters DESC, c.target_lid
        """).fetchall()
    cn.close()
    return rows


def sample_pairs(rows, n, seed=""):
    """A reproducible uniform-random subset of `rows`, picked by hashing (seed, pair).

    `candidates()` orders by support DESC, so a plain `--limit` run takes the EASIEST rows —
    fine for a smoke test, wrong for measuring what a lane would actually write (the senseless
    lane is ~half support-2, and its yes-rate falls with support). This draws instead.

    The subset is defined by the pair keys alone, so it is stable under two things that break
    `rows[:n]`: re-ordering the pool, and part of the sample already being cached. That is what
    makes a measurement run resumable — an interrupted run re-draws the SAME sample and verifies
    only the rest. It is also NESTED in n: --sample 240 is a superset of --sample 48 at the same
    seed, so a pilot can be grown into the full sample without re-paying for it. Adding
    candidates to the pool can still swap a row in or out, so quote a sample with the pool it
    was drawn from."""
    return sorted(rows, key=lambda r: hashlib.sha1(
        f"{seed}|{r[0]}|{r[3]}".encode()).hexdigest())[:n]


def load_item_lang(qids):
    """{qid: {lang: (label, desc)}} from the all-language CC0 cache (item_lang in
    wd_items.sqlite3, built by fetch_anchor_langs.py). Empty if the table is absent."""
    out = {}
    qids = list(qids)
    if not qids:
        return out
    cn = sqlite3.connect(config.ITEMS)
    if cn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='item_lang'"
                  ).fetchone():
        for i in range(0, len(qids), 500):
            chunk = qids[i:i + 500]
            q = ("SELECT qid, lang, label, desc FROM item_lang WHERE qid IN (%s)"
                 % ",".join("?" * len(chunk)))
            for qid, lang, label, desc in cn.execute(q, chunk):
                out.setdefault(qid, {})[lang] = (label, desc)
    cn.close()
    return out


def unfetched_anchors(qids):
    """Anchor qids fetch_anchor_langs.py has never processed (absent from item_lang_fetched).

    Keyed on the fetch MARKER, not on item_lang having rows: an anchor can be legitimately
    fetched-but-sparse (no CC0 labels in any language) and still belongs on fmt_concept's
    graceful de/en/sv fallback. Only *un-fetched* anchors mean the fetch is stale relative to
    the candidate pool — verifying them would silently downgrade +multi to the base format
    (as happened after the all-language rebuild added anchors). The run guards on this."""
    qids = set(qids)
    if not qids:
        return set()
    cn = sqlite3.connect(config.ITEMS)
    has = cn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                     "AND name='item_lang_fetched'").fetchone()
    fetched = {r[0] for r in cn.execute("SELECT qid FROM item_lang_fetched")} if has else set()
    cn.close()
    return qids - fetched


def fmt_concept(row, langs, il_map):
    """(qid, label, description) for the anchor concept, built from item_lang (il_map) in the
    `langs` spec (a config.CONCEPT_LANGS_* entry), target-language first — the ONLY concept the
    verifier grades on (has_evidence guarantees a live-run row has an entry here)."""
    _lid, lang, _lemma, qid, _sup = row
    entry = il_map.get(qid)
    res = config.resolve_concept_langs(entry, langs, target=lang) if entry else []
    lab = " / ".join(dict.fromkeys(l for _d, l, _ds in res if l)) or "(no label)"
    desc = " | ".join(f"{d}: {ds}" for d, _l, ds in res if ds) or "(no description)"
    return qid, lab, desc


def has_evidence(row, il_map):
    """True if the anchor has a gradable CC0 concept (some label or description) in
    item_lang — the SAME source the live verifier grades on."""
    e = il_map.get(row[3])
    return bool(e) and any((lab or desc) for lab, desc in e.values())


def load_glosses(lids):
    """{lid: {lang: gloss}} of CC0 Wikidata sense glosses — MONOSEMOUS lexemes only.

    A candidate is lemma-level (no sense id), so a gloss is only safe to attach when the
    lexeme has exactly one sense; for a polysemous lexeme we can't know which sense the
    cluster grounds, so we omit it and fall back to the bare lemma."""
    out = {}
    if not lids:
        return out
    wd = sqlite3.connect(config.WIKIDATA)
    by_lid = {}
    lids = list(lids)
    for i in range(0, len(lids), 500):
        chunk = lids[i:i + 500]
        q = ("SELECT lid, sid, lang, gloss FROM sense_gloss WHERE lid IN (%s)"
             % ",".join("?" * len(chunk)))
        for lid, sid, lang, gloss in wd.execute(q, chunk):
            by_lid.setdefault(lid, {}).setdefault(sid, {})[lang] = gloss
    wd.close()
    for lid, senses in by_lid.items():
        if len(senses) == 1:                       # monosemous → unambiguous gloss
            out[lid] = next(iter(senses.values()))
    return out


def load_senses(lids):
    """{lid: {sid: {lang: gloss}}} for every lexeme WITH senses — the numbered inventory the
    verifier is shown so it can say WHICH sense the concept belongs to, or 'none'. Same offer as
    assign.py made (target language preferred, unglossed senses dropped, stable sid order), so
    the measured 93-95% pick / 60:1 abstention numbers transfer as a prior. A monosemous lexeme
    is a one-item list: 'none' there means its one recorded sense is not this meaning but the
    noun has it — the create_add signal the single-gloss format could not express."""
    out = {}
    if not lids:
        return out
    wd = sqlite3.connect(config.WIKIDATA)
    by_lid = {}
    lids = list(lids)
    for i in range(0, len(lids), 500):
        chunk = lids[i:i + 500]
        for lid, sid, lang, gloss in wd.execute(
                "SELECT lid, sid, lang, gloss FROM sense_gloss WHERE lid IN (%s)"
                % ",".join("?" * len(chunk)), chunk):
            by_lid.setdefault(lid, {}).setdefault(sid, {})[lang] = gloss
    wd.close()
    return by_lid


def offered_senses(smap, lid, lang):
    """[(sid, text)] the numbered list for one poly lexeme in the target language, else en/de/sv."""
    out = []
    for sid in sorted(smap.get(lid, {})):
        g = smap[lid][sid]
        text = next((g[c] for c in (lang, "en", "de", "sv") if g.get(c)),
                    next(iter(g.values()), ""))
        if text:
            out.append((sid, text))
    return out


def sense_shape(lids):
    """'0 senses: N, 1: N, 2+: N' for a set of target lexemes — the pool's lane mix."""
    if not lids:
        return "(empty)"
    counts = {}
    cn = sqlite3.connect(config.CANDIDATES)
    lids = list(lids)
    for i in range(0, len(lids), 500):
        chunk = lids[i:i + 500]
        q = ("SELECT DISTINCT target_lid, target_n_senses FROM candidate WHERE target_lid IN (%s)"
             % ",".join("?" * len(chunk)))
        for _lid, n in cn.execute(q, chunk):
            key = "2+" if n >= 2 else str(n)
            counts[key] = counts.get(key, 0) + 1
    cn.close()
    return ", ".join(f"{k} senses: {v:,}" for k, v in sorted(counts.items()))


def pick_gloss(gmap, lid, lang):
    """The lexeme's gloss, target-language preferred, else any (en/de/sv); '' if none."""
    vals = gmap.get(lid, {})
    for code in [lang, "en", "de", "sv"]:
        if vals.get(code):
            return vals[code]
    return next(iter(vals.values()), "")


def run_batch(rows, model, gmap, langs=None, il_map=None, timeout=300, smap=None):
    """Verify a batch. langs/il_map select the unified +multi concept format; smap (load_senses)
    attaches the numbered inventory to polysemous targets. Returns (verdicts, offered), where
    offered[i] is the [(sid, gloss)] list row i was shown — what parse_sense resolves against."""
    lines, offered = [], []
    for n, row in enumerate(rows, 1):
        lid, lang, lemma, *_ = row
        qid, lab, desc = fmt_concept(row, langs=langs, il_map=il_map)
        sen = offered_senses(smap or {}, lid, lang)
        offered.append(sen)
        if sen:
            gpart = " senses: " + "; ".join(f"({j}) {t}" for j, (_s, t) in enumerate(sen, 1))
        else:
            gloss = pick_gloss(gmap, lid, lang)
            gpart = f' gloss="{gloss}"' if gloss else ""
        lines.append(
            f'{n}. language={config.LANGNAME.get(lang, lang)} noun="{lemma}"{gpart} | '
            f'concept {qid}: label="{lab}" description="{desc}"'
        )
    return llm.batch_json(SYSTEM, "Items:\n" + "\n".join(lines), model, len(rows),
                          {"verdict": "parse_error", "reason": ""}, timeout=timeout), offered


def verify_rows(rows, model, gmap, smap, il_map, batch_size):
    """[(lid, qid, lemma, lang, verdict, reason, target_sid)] for `rows`, batched; a failed
    batch yields 'error' rows (not cached as done, so a re-run retries them)."""
    recs, counts = [], {}
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        try:
            verdicts, offered = run_batch(batch, model, gmap, smap=smap,
                                          langs=config.CONCEPT_LANGS_VERIFY, il_map=il_map)
        except Exception as e:  # noqa: BLE001 — one bad batch shouldn't kill the run
            print(f"  batch {i // batch_size} failed: {e}")
            verdicts = [{"verdict": "error", "reason": ""}] * len(batch)
            offered = [offered_senses(smap, r[0], r[1]) for r in batch]
        for row, v, sen in zip(batch, verdicts, offered):
            verdict = v.get("verdict", "parse_error")
            counts[verdict] = counts.get(verdict, 0) + 1
            recs.append((row[0], row[3], row[2], row[1], verdict, compose_reason(v, bool(sen)),
                         parse_sense(v.get("sense"), sen)))
        print(f"  {min(i + batch_size, len(rows)):,}/{len(rows):,}  {counts}", flush=True)
    return recs


def vote(samples):
    """(verdict, reason, target_sid) by strict majority over (verdict, target_sid); within the
    majority, the most common relation head wins the reason. No majority -> 'uncertain', which
    keeps the pair out of every verdict='yes' consumer."""
    keys = collections.Counter((v, sid) for v, _r, sid in samples)
    (v, sid), k = keys.most_common(1)[0]
    if 2 * k <= len(samples):
        return "uncertain", "no majority: " + " / ".join(
            f"{v}·{sid}" for (v, sid) in keys), None
    heads = collections.Counter(r.split(":")[0] for v2, r, s2 in samples if (v2, s2) == (v, sid))
    head = heads.most_common(1)[0][0]
    return v, next(r for v2, r, s2 in samples if (v2, s2) == (v, sid)
                   and r.split(":")[0] == head), sid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40, help="max candidates (default 40 sample)")
    ap.add_argument("--all", action="store_true", help="verify every eligible candidate")
    ap.add_argument("--sample", type=int, help="verify a reproducible RANDOM subset of N eligible "
                    "candidates instead of the top --limit by support — the draw a precision "
                    "measurement needs (see sample_pairs). Resumable and nested in N.")
    ap.add_argument("--seed", default="", help="sample seed (any string): picks WHICH random "
                    "subset --sample draws. Record it with the result.")
    ap.add_argument("--recache", action="store_true",
                    help="re-verify exactly the pairs that already hold a verdict under an OLDER "
                         "prompt version — what makes the cache whole again after a prompt change, "
                         "since every read filters on the current version. Resumable; ignores "
                         "--limit; combines with the lane flags to do one lane at a time.")
    ap.add_argument("--samples", type=int, default=1,
                    help="verify each pair N times and cache the majority (samples kept in the "
                         "`sample` table; resumable). Re-votes pairs already cached.")
    ap.add_argument("--create-class", action="store_true",
                    help="only pairs the current verdict calls yes with no fitting sense (the "
                         "create rows); ignores --limit. Combine with the lane flags.")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--min-support", type=int, default=1)
    ap.add_argument("--senseless-only", action="store_true")
    ap.add_argument("--poly-only", action="store_true",
                    help="the polysemous review lane (target n_senses>=2 with a free "
                         "sense); use with --gate AD, never B")
    ap.add_argument("--gate", default="", help="structural gate filter, e.g. ABD (the High tier)")
    ap.add_argument("--force", action="store_true", help="re-verify even cached candidates "
                    "(needed to apply a changed prompt/criterion; overwrites their verdicts)")
    ap.add_argument("--allow-degraded", action="store_true", help="run even when some anchors "
                    "lack item_lang — they are skipped (no +multi concept to grade) instead of "
                    "failing the run; off by default so a stale fetch fails loud")
    ap.add_argument("--csv", default="", help="export the full verdict table to this CSV "
                    "on demand (the verdicts cache is data/verify.sqlite3)")
    args = ap.parse_args()
    if args.senseless_only and "B" in args.gate.upper():
        ap.error("--senseless-only (targets n_senses=0) is incompatible with gate B (requires n_senses=1)")
    if args.poly_only and "B" in args.gate.upper():
        ap.error("--poly-only (targets n_senses>=2) is incompatible with gate B (requires n_senses=1)")
    if args.poly_only and args.senseless_only:
        ap.error("--poly-only and --senseless-only select disjoint lanes; pick one")

    out = sqlite3.connect(config.VERIFY)
    ensure_schema(out)
    # Only verdicts produced by the CURRENT prompt count as done; rows from other prompt versions
    # are kept (see the SCHEMA note) but don't satisfy this run.
    #
    # error/parse_error rows do NOT count as done either. They are written when a batch fails —
    # a timeout, a quota stall, a malformed reply — and presence-only done-sets used to make that
    # permanent: the affected pairs were never retried, and the only recovery was --force, i.e.
    # re-paying for the entire cache. On a measurement run that silently punches holes in the
    # sample rather than failing loudly.
    done = set() if args.force else {
        (r[0], r[1]) for r in out.execute(
            "SELECT target_lid, anchor_qid FROM verdict "
            "WHERE prompt_ver = ? AND verdict NOT IN ('error', 'parse_error')", [PROMPT_VER])}

    eligible = candidates(args.gate, args.min_support, args.senseless_only,
                          poly_only=args.poly_only)
    # Sample BEFORE the cache check, not after: the sample is then a fixed set of pairs that a
    # resumed run re-draws identically, instead of "N more rows that aren't done yet" — which
    # would quietly grow the sample every time a run was interrupted.
    if args.sample:
        eligible = sample_pairs(eligible, args.sample, args.seed)
        print(f"sample: {len(eligible):,} of the eligible pool, seed={args.seed!r} "
              f"(reproducible; --sample is nested in N at a fixed seed)")
    if args.recache:
        # A prompt edit no longer destroys old verdicts, but consumers read only the current
        # version — so after one, the queue sees just what has been re-verified. This selects the
        # previously-verified set rather than a gate, because that IS the set the queue, the
        # calibration and the graders were reading before the change. Pairs that have since left
        # the candidate pool are dropped, and said so: nothing reads them, and re-verifying them
        # would be paying to refresh rows no consumer joins to.
        prior = {(r[0], r[1]) for r in out.execute(
            "SELECT DISTINCT target_lid, anchor_qid FROM verdict WHERE prompt_ver <> ?",
            [PROMPT_VER])}
        eligible = [r for r in eligible if (r[0], r[3]) in prior]
        stale = len(prior) - len(eligible)
        print(f"recache: {len(eligible):,} pairs held a verdict under an earlier prompt and are "
              f"still candidates" + (f"; {stale:,} are no longer in the pool and are left behind"
                                     if stale else ""))
    if args.create_class:
        cls = {(r[0], r[1]) for r in out.execute(
            "SELECT target_lid, anchor_qid FROM verdict WHERE prompt_ver = ? AND verdict = 'yes' "
            "AND target_sid = 'none'", [PROMPT_VER])}
        eligible = [r for r in eligible if (r[0], r[3]) in cls]
        print(f"create class: {len(eligible):,} pairs")
    # a vote re-verifies cached pairs on purpose; its resumability is the sample table's
    pool = eligible if args.samples > 1 else [r for r in eligible if (r[0], r[3]) not in done]
    if not args.all and not args.sample and not args.recache and not args.create_class:
        pool = pool[: args.limit]
    # The live verifier's concept comes ONLY from item_lang (the +multi format) — de/en/sv is
    # no longer a grading fallback. So every anchor we intend to grade must have been fetched
    # by fetch_anchor_langs.py, else it has no concept and the availability gate below silently
    # drops it. Fail loud on a stale fetch instead.
    unfetched = unfetched_anchors({r[3] for r in pool})
    if unfetched and not args.allow_degraded:
        ap.error(
            f"{len(unfetched)} of {len({r[3] for r in pool}):,} anchors to verify are not in "
            f"item_lang — they have no +multi concept and would be skipped. Refresh the cache "
            f"first:\n    uv run fetch_anchor_langs.py\n"
            f"  (or pass --allow-degraded to run anyway, skipping them — e.g. a smoke test).")
    il_map = load_item_lang({r[3] for r in pool})   # the all-language concept (+multi) — the
    rows = [r for r in pool if has_evidence(r, il_map)]        # ONLY source the grader reads
    skipped = len(pool) - len(rows)
    gmap = load_glosses({r[0] for r in rows})
    smap = load_senses({r[0] for r in rows})    # poly targets: the numbered inventory
    print(f"candidates to verify: {len(rows):,}  ({len(gmap):,} with a CC0 gloss, "
          f"{len(smap):,} with a sense inventory"
          + (f"; {skipped:,} skipped — no item_lang concept" if skipped else "")
          + f"; batch={args.batch}, model={args.model})")
    # Sense-count breakdown, so the polysemous rows that the action-level admissibility newly
    # admits (gates.clean_clause no longer filters them out) are visible BEFORE the run spends
    # anything, rather than showing up on the bill.
    print(f"  target shape: {sense_shape({r[0] for r in rows})}")

    if args.samples == 1:
        recs = verify_rows(rows, args.model, gmap, smap, il_map, args.batch)
        out.executemany(
            "INSERT OR REPLACE INTO verdict (target_lid, anchor_qid, target_lemma, target_lang, "
            "verdict, reason, model, prompt_ver, target_sid) VALUES (?,?,?,?,?,?,?,?,?)",
            [(l, q, lem, lg, v, r, args.model, PROMPT_VER, sid) for l, q, lem, lg, v, r, sid in recs])
        out.commit()
    else:
        for n in range(args.samples):
            have = {(r[0], r[1]) for r in out.execute(
                "SELECT target_lid, anchor_qid FROM sample WHERE prompt_ver = ? AND model = ? "
                "AND n = ? AND verdict NOT IN ('error', 'parse_error')", [PROMPT_VER, args.model, n])}
            todo = [r for r in rows if (r[0], r[3]) not in have]
            print(f"sample {n + 1}/{args.samples}: {len(todo):,} to verify")
            recs = verify_rows(todo, args.model, gmap, smap, il_map, args.batch)
            out.executemany(
                "INSERT OR REPLACE INTO sample VALUES (?,?,?,?,?,?,?,?,?)",
                [(l, q, PROMPT_VER, args.model, n, v, r, sid, llm.usage_summary()["model"])
                 for l, q, _lem, _lg, v, r, sid in recs])
            out.commit()
        voted = collections.Counter()
        for lid, lang, lemma, qid, *_ in rows:
            got = out.execute(
                "SELECT verdict, reason, target_sid, model_id FROM sample WHERE target_lid = ? "
                "AND anchor_qid = ? AND prompt_ver = ? AND model = ? AND n BETWEEN 0 AND ? "
                "AND verdict NOT IN ('error', 'parse_error')",
                [lid, qid, PROMPT_VER, args.model, args.samples - 1]).fetchall()
            if len(got) < args.samples:
                continue                     # a failed batch: re-run to complete the vote
            v, r, sid = vote([g[:3] for g in got])
            out.execute(                     # keep the single-sample verdict the vote replaces
                "INSERT OR IGNORE INTO sample SELECT target_lid, anchor_qid, prompt_ver, model, -1, "
                "verdict, reason, target_sid, NULL FROM verdict WHERE target_lid = ? AND "
                "anchor_qid = ? AND prompt_ver = ? AND model NOT LIKE '%×%'", [lid, qid, PROMPT_VER])
            out.execute(
                "INSERT OR REPLACE INTO verdict (target_lid, anchor_qid, target_lemma, target_lang, "
                "verdict, reason, model, prompt_ver, target_sid) VALUES (?,?,?,?,?,?,?,?,?)",
                [lid, qid, lemma, lang, v, r, f"{got[0][3] or args.model}×{args.samples}",
                 PROMPT_VER, sid])
            voted[v] += 1
        out.commit()
        print(f"voted: {dict(voted)}")

    print("\nverdict distribution (this prompt):")
    for v, n in out.execute("SELECT verdict, count(*) FROM verdict WHERE prompt_ver = ? "
                            "GROUP BY verdict ORDER BY 2 DESC", [PROMPT_VER]):
        print(f"  {v:<12} {n:,}")
    others = out.execute("SELECT count(*) FROM verdict WHERE prompt_ver <> ?",
                         [PROMPT_VER]).fetchone()[0]
    if others:
        print(f"  ({others:,} verdicts from earlier prompt versions kept for comparison — "
              f"validate_prompt.py --baseline-ver)")

    u = llm.usage_summary()
    if u["calls"]:
        print(f"\nclaude -p usage: {u['calls']:,} calls  "
              f"in {u['input_tokens']:,} + {u['cache_read_input_tokens']:,} cached-read "
              f"(+{u['cache_creation_input_tokens']:,} cache-write) / out {u['output_tokens']:,} tok"
              f"  ~${u['cost_usd']:.2f}  (~${u['cost_usd'] / u['calls']:.4f}/call)")

    if args.csv:
        cols = ["target_lid", "anchor_qid", "target_lemma", "target_lang",
                "verdict", "reason", "model", "prompt_ver", "target_sid"]  # several versions
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(out.execute(
                f"SELECT {','.join(cols)} FROM verdict ORDER BY target_lang, target_lemma"))
        print(f"\nwrote audit CSV -> {args.csv}")
    out.close()


if __name__ == "__main__":
    main()
