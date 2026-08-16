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
    "specific sense being linked), and a candidate concept described by a Wikidata CC0 label and "
    "description. "
    "Use the GLOSS as the primary signal for WHICH sense of the noun is meant. If no gloss is shown, "
    "or it is a bare one-word synonym or written in an unexpected language, fall back to judging from "
    "the noun itself and the concept. "
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
    'Reply with ONLY a JSON array (no prose, no code fences), one object per item in order: '
    '[{"i":1,"verdict":"yes|no|uncertain","reason":"<REL>: <=10 words"}], where <REL> is the '
    "relation you chose (EQUAL/BROADER/NARROWER/LATERAL/INCOHERENT/DIFFERENT)."
)

# Fingerprint of the prompt: a changed SYSTEM yields a new PROMPT_VER, which automatically
# invalidates verdicts cached under an older prompt (they are re-verified, not trusted).
PROMPT_VER = hashlib.sha1(SYSTEM.encode()).hexdigest()[:8]

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict(
    target_lid TEXT, anchor_qid TEXT, target_lemma TEXT, target_lang TEXT,
    verdict TEXT, reason TEXT, model TEXT, prompt_ver TEXT,
    PRIMARY KEY (target_lid, anchor_qid, prompt_ver)
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
    try:
        cn.execute("ALTER TABLE verdict ADD COLUMN prompt_ver TEXT")  # pre-fingerprint cache
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
    """Clean candidates joined to the anchor Q's CC0 label/description.

    `gate` adds structural gates (the same SQL fragments gates.high_tier_where composes):
    A (>=2 anchors), B (monosemous target, n_senses==1), D (concrete in-scope anchor).
    ABD is the measured ~95.9%-precision High tier. (The measurement harnesses' Python
    gate D, precision.typed_qids, applies the same P31 + scope-exclusion definition.)

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
        SELECT c.target_lid, c.target_lang, c.target_lemma, c.anchor_qid, c.support_clusters,
               i.label_de, i.label_en, i.label_sv, i.desc_de, i.desc_en, i.desc_sv
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


def fmt_concept(row, langs=None, il_map=None):
    """(qid, label, description) for the anchor concept, target-language first.

    langs (a config.CONCEPT_LANGS_* spec) selects the unified multilingual format: the
    concept is built from item_lang (il_map) across those languages, target-language
    first. This is the +multi arm, and the ONLY concept the live verifier grades on — its
    has_evidence gate now keys on item_lang, so a live-run row always has an entry here.
    The de/en/sv fallback below (when item_lang lacks the qid) is therefore reached only by
    the frozen measurement harnesses' +multi arm and by --allow-degraded smoke runs; it is
    the base availability tier, not a grading source — and it widens to ALL of de/en/sv so
    cross-language disagreement still surfaces. langs=None keeps the legacy de/en/sv base
    format (target-lang + English label, single best description) byte-identical for the
    measurement callers."""
    _lid, lang, _lemma, qid, _sup, lde, len_, lsv, dde, den, dsv = row
    widen = False
    if langs is not None:
        entry = (il_map or {}).get(qid)
        res = config.resolve_concept_langs(entry, langs, target=lang) if entry else []
        if res:
            lab = " / ".join(dict.fromkeys(l for _d, l, _ds in res if l)) or "(no label)"
            desc = " | ".join(f"{d}: {ds}" for d, _l, ds in res if ds) or "(no description)"
            return qid, lab, desc
        widen = True   # no item_lang coverage -> widen de/en/sv instead
    # Only de/en/sv anchor labels live in the wide `item` table; a target lang outside
    # that set (fr/no) simply has no own-language column here, so use .get and fall back
    # to en/de/sv. (The +multi path above already supplies fr/no from item_lang.)
    labels = {"de": lde, "en": len_, "sv": lsv}
    descs = {"de": dde, "en": den, "sv": dsv}
    order = list(dict.fromkeys([lang, "en", "de", "sv"]))   # target-language first
    if widen:
        lab = " / ".join(dict.fromkeys(labels[c] for c in order if labels.get(c)))
        desc = " | ".join(f"{c}: {descs[c]}" for c in order if descs.get(c))
    else:
        lab = " / ".join(dict.fromkeys(x for x in ([labels.get(lang), len_] if lang != "en" else [len_]) if x))
        desc = descs.get(lang) or den or dde or dsv
    return qid, lab or "(no label)", desc or "(no description)"


def has_evidence(row, il_map=None):
    """True if the anchor has a gradable CC0 concept (some label or description).

    With il_map (the all-language item_lang cache), evidence is judged from item_lang — the
    SAME source the live verifier grades on — so an anchor is kept iff it can actually be
    formatted as +multi. Without il_map it falls back to the de/en/sv base columns: the
    pre-fetch availability tier used by fetch_anchor_langs.needed_qids (item_lang doesn't
    exist yet at that point) and by the frozen measurement harnesses."""
    if il_map is not None:
        e = il_map.get(row[3])
        return bool(e) and any((lab or desc) for lab, desc in e.values())
    qid, lab, desc = fmt_concept(row)
    return lab != "(no label)" or desc != "(no description)"


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


def run_batch(rows, model, gmap, langs=None, il_map=None, timeout=300):
    """Verify a batch. langs/il_map select the unified +multi concept format."""
    lines = []
    for n, row in enumerate(rows, 1):
        lid, lang, lemma, *_ = row
        qid, lab, desc = fmt_concept(row, langs=langs, il_map=il_map)
        gloss = pick_gloss(gmap, lid, lang)
        gpart = f' gloss="{gloss}"' if gloss else ""
        lines.append(
            f'{n}. language={config.LANGNAME.get(lang, lang)} noun="{lemma}"{gpart} | '
            f'concept {qid}: label="{lab}" description="{desc}"'
        )
    return llm.batch_json(SYSTEM, "Items:\n" + "\n".join(lines), model, len(rows),
                          {"verdict": "parse_error", "reason": ""}, timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40, help="max candidates (default 40 sample)")
    ap.add_argument("--all", action="store_true", help="verify every eligible candidate")
    ap.add_argument("--sample", type=int, help="verify a reproducible RANDOM subset of N eligible "
                    "candidates instead of the top --limit by support — the draw a precision "
                    "measurement needs (see sample_pairs). Resumable and nested in N.")
    ap.add_argument("--seed", default="", help="sample seed (any string): picks WHICH random "
                    "subset --sample draws. Record it with the result.")
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
    pool = [r for r in eligible if (r[0], r[3]) not in done]
    if not args.all and not args.sample:
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
    print(f"candidates to verify: {len(rows):,}  ({len(gmap):,} with a CC0 gloss"
          + (f"; {skipped:,} skipped — no item_lang concept" if skipped else "")
          + f"; batch={args.batch}, model={args.model})")
    # Sense-count breakdown, so the polysemous rows that the action-level admissibility newly
    # admits (gates.clean_clause no longer filters them out) are visible BEFORE the run spends
    # anything, rather than showing up on the bill.
    print(f"  target shape: {sense_shape({r[0] for r in rows})}")

    counts = {}
    for i in range(0, len(rows), args.batch):
        batch = rows[i : i + args.batch]
        try:
            verdicts = run_batch(batch, args.model, gmap,
                                 langs=config.CONCEPT_LANGS_VERIFY, il_map=il_map)
        except Exception as e:  # noqa: BLE001 — one bad batch shouldn't kill the run
            print(f"  batch {i // args.batch} failed: {e}")
            verdicts = [{"verdict": "error", "reason": ""}] * len(batch)
        recs = []
        for row, v in zip(batch, verdicts):
            lid, lang, lemma, qid = row[0], row[1], row[2], row[3]
            verdict = v.get("verdict", "parse_error")
            counts[verdict] = counts.get(verdict, 0) + 1
            recs.append((lid, qid, lemma, lang, verdict, v.get("reason", ""), args.model, PROMPT_VER))
        out.executemany(
            "INSERT OR REPLACE INTO verdict VALUES (?,?,?,?,?,?,?,?)", recs)
        out.commit()
        print(f"  {min(i + args.batch, len(rows)):,}/{len(rows):,}  {counts}")

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
                "verdict", "reason", "model", "prompt_ver"]  # ver: the cache holds several
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(out.execute(
                f"SELECT {','.join(cols)} FROM verdict ORDER BY target_lang, target_lemma"))
        print(f"\nwrote audit CSV -> {args.csv}")
    out.close()


if __name__ == "__main__":
    main()
