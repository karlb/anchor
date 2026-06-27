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
API key). Each CLI call reloads ~19k tokens of agent context, so candidates are
**batched** (default 8/call) to amortise that ~10×. Verdicts are cached in
``data/verify.sqlite3`` keyed by (target_lid, anchor_qid) — re-runs skip done work, but a
changed prompt (tracked by its SHA1 fingerprint) auto-invalidates stale verdicts so they
are re-verified rather than silently trusted.

Run after fetch_items.py (defaults to a 40-candidate sample):
    uv run verify.py                       # sample the highest-support clean candidates
    uv run verify.py --senseless-only      # only candidates grounding senseless lexemes
    uv run verify.py --all --batch 8       # full run (costs add up — see note above)
    uv run verify.py --gate ABD --all --model sonnet   # the High tier
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

LANGNAME = {"de": "German", "en": "English", "sv": "Swedish"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict(
    target_lid TEXT, anchor_qid TEXT, target_lemma TEXT, target_lang TEXT,
    verdict TEXT, reason TEXT, model TEXT, prompt_ver TEXT,
    PRIMARY KEY (target_lid, anchor_qid)
) WITHOUT ROWID;
"""


def candidates(args):
    """Clean candidates joined to the anchor Q's CC0 label/description.

    --gate adds structural gates matching measure_lift/ablation: A (>=2 anchors),
    B (monosemous target, n_senses==1), D (concrete anchor with a P31). ABD is the
    measured ~95.9%-precision High tier.
    """
    cn = sqlite3.connect(config.CANDIDATES)
    cn.execute("ATTACH DATABASE ? AS it", [config.ITEMS])
    gate = args.gate.upper()
    where = "c.homograph_n_lid = 1 AND NOT c.cross_pivot_conflict AND c.support_clusters >= ?"
    params = [max(args.min_support, 2 if "A" in gate else args.min_support)]
    if args.senseless_only:
        where += " AND c.target_n_senses = 0"
    if "B" in gate:
        where += " AND c.target_n_senses = 1"
    if "D" in gate:
        # concrete anchor (has a P31) AND in-scope — the SAME 'D' gate
        # (gates.concrete_anchor_clause) that high_tier_where / review_queue / export use,
        # so --gate ABD matches their High tier and the two definitions can't drift apart.
        where += " AND " + gates.concrete_anchor_clause("c", "it")
    rows = cn.execute(
        f"""
        SELECT c.target_lid, c.target_lang, c.target_lemma, c.anchor_qid, c.support_clusters,
               i.label_de, i.label_en, i.label_sv, i.desc_de, i.desc_en, i.desc_sv
        FROM candidate c
        JOIN it.item i ON i.qid = c.anchor_qid AND NOT i.missing
        WHERE {where}
        ORDER BY c.support_clusters DESC, c.target_lid
        """,
        params,
    ).fetchall()
    cn.close()
    return rows


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


def fmt_concept(row, labels_all=False, descs_all=False, langs=None, il_map=None):
    """(qid, label, description) for the anchor concept, target-language first.

    Default: label = target-lang + English (deduped), description = single best.
    labels_all / descs_all widen each to ALL available CC0 languages (de/en/sv) —
    this surfaces cross-language disagreement so the verifier can spot an INCOHERENT
    target. Both off keeps the legacy format (so production is unchanged until an
    A/B says a wider format actually helps).

    langs (a config.CONCEPT_LANGS_* spec) selects the unified multilingual format: the
    concept is built from item_lang (il_map) across those languages, target-language
    first. This is the +multi arm. When item_lang lacks the qid we widen the 3 editing
    languages instead (labels_all=descs_all=True), so +multi degrades gracefully rather
    than dropping coverage. langs=None keeps the legacy de/en/sv path byte-identical."""
    _lid, lang, _lemma, qid, _sup, lde, len_, lsv, dde, den, dsv = row
    if langs is not None:
        entry = (il_map or {}).get(qid)
        res = config.resolve_concept_langs(entry, langs, target=lang) if entry else []
        if res:
            lab = " / ".join(dict.fromkeys(l for _d, l, _ds in res if l)) or "(no label)"
            desc = " | ".join(f"{d}: {ds}" for d, _l, ds in res if ds) or "(no description)"
            return qid, lab, desc
        labels_all = descs_all = True   # no item_lang coverage -> widen de/en/sv instead
    labels = {"de": lde, "en": len_, "sv": lsv}
    descs = {"de": dde, "en": den, "sv": dsv}
    order = list(dict.fromkeys([lang, "en", "de", "sv"]))   # target-language first
    if labels_all:
        lab = " / ".join(dict.fromkeys(labels[c] for c in order if labels[c]))
    else:
        lab = " / ".join(dict.fromkeys(x for x in ([labels[lang], len_] if lang != "en" else [len_]) if x))
    lab = lab or "(no label)"
    if descs_all:
        desc = " | ".join(f"{c}: {descs[c]}" for c in order if descs[c])
    else:
        desc = descs[lang] or den or dde or dsv
    desc = desc or "(no description)"
    return qid, lab, desc


def has_evidence(row):
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


def pick_gloss(gmap, lid, lang):
    """The lexeme's gloss, target-language preferred, else any (en/de/sv); '' if none."""
    vals = gmap.get(lid, {})
    for code in [lang, "en", "de", "sv"]:
        if vals.get(code):
            return vals[code]
    return next(iter(vals.values()), "")


def run_batch(rows, model, gmap, labels_all=False, descs_all=False,
              langs=None, il_map=None, system=None, timeout=300):
    """Verify a batch. langs/il_map select the unified +multi concept format; system
    overrides the default SYSTEM prompt (e.g. the multilingual-guided variant)."""
    lines = []
    for n, row in enumerate(rows, 1):
        lid, lang, lemma, *_ = row
        qid, lab, desc = fmt_concept(row, labels_all, descs_all, langs=langs, il_map=il_map)
        gloss = pick_gloss(gmap, lid, lang)
        gpart = f' gloss="{gloss}"' if gloss else ""
        lines.append(
            f'{n}. language={LANGNAME.get(lang, lang)} noun="{lemma}"{gpart} | '
            f'concept {qid}: label="{lab}" description="{desc}"'
        )
    return llm.batch_json(system or SYSTEM, "Items:\n" + "\n".join(lines), model, len(rows),
                          {"verdict": "parse_error", "reason": ""}, timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40, help="max candidates (default 40 sample)")
    ap.add_argument("--all", action="store_true", help="verify every eligible candidate")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--min-support", type=int, default=1)
    ap.add_argument("--senseless-only", action="store_true")
    ap.add_argument("--gate", default="", help="structural gate filter, e.g. ABD (the High tier)")
    ap.add_argument("--force", action="store_true", help="re-verify even cached candidates "
                    "(needed to apply a changed prompt/criterion; overwrites their verdicts)")
    ap.add_argument("--csv", default="", help="export the full verdict table to this CSV "
                    "on demand (the verdicts cache is data/verify.sqlite3)")
    args = ap.parse_args()
    if args.senseless_only and "B" in args.gate.upper():
        ap.error("--senseless-only (targets n_senses=0) is incompatible with gate B (requires n_senses=1)")

    out = sqlite3.connect(config.VERIFY)
    out.executescript(SCHEMA)
    try:
        out.execute("ALTER TABLE verdict ADD COLUMN prompt_ver TEXT")  # migrate a pre-fingerprint cache
        out.commit()
    except sqlite3.OperationalError:
        pass  # column already present
    # Only verdicts produced by the CURRENT prompt count as done; a prompt edit (new PROMPT_VER)
    # auto-invalidates the rest so they are re-verified without needing --force.
    done = set() if args.force else {
        (r[0], r[1]) for r in out.execute(
            "SELECT target_lid, anchor_qid FROM verdict WHERE prompt_ver = ?", [PROMPT_VER])}

    rows = [r for r in candidates(args) if (r[0], r[3]) not in done and has_evidence(r)]
    if not args.all:
        rows = rows[: args.limit]
    gmap = load_glosses({r[0] for r in rows})
    il_map = load_item_lang({r[3] for r in rows})   # all-language concept (+multi format)
    n_cov = sum(1 for r in rows if r[3] in il_map)
    print(f"candidates to verify: {len(rows):,}  ({len(gmap):,} with a CC0 gloss; "
          f"item_lang {n_cov:,}/{len(rows):,}; batch={args.batch}, model={args.model})")

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

    print("\nverdict distribution:")
    for v, n in out.execute("SELECT verdict, count(*) FROM verdict GROUP BY verdict ORDER BY 2 DESC"):
        print(f"  {v:<12} {n:,}")

    if args.csv:
        cols = ["target_lid", "anchor_qid", "target_lemma", "target_lang",
                "verdict", "reason", "model"]
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(out.execute(
                f"SELECT {','.join(cols)} FROM verdict ORDER BY target_lang, target_lemma"))
        print(f"\nwrote audit CSV -> {args.csv}")
    out.close()


if __name__ == "__main__":
    main()
