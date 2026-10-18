#!/usr/bin/env python3
"""Human review for verified P5137 candidates — the authorization gate.

The verifier proposes; a human approves; only then may write.py act. The human
decisions are the ONE irreplaceable thing in the pipeline (everything else is
regenerable from the dump + infer.sqlite3), so they are the single source of
truth: an append-only CSV log at config.DECISIONS (committed, diffable). There is
no review SQLite DB — "pending" is computed as the verifier-'yes' High-tier
candidate pool MINUS whatever the log already covers.

    uv run review_queue.py serve       # http://localhost:8731  approve/reject/defer buttons
    uv run review_queue.py status       # counts (decided from the log; pending computed)

Status flow: a (lexeme, anchor) starts undecided (= pending). An approve/reject/defer
appends one event; write.py appends a 'written' event with the revid. Replaying
('deferred' = a hold: not written, and excluded from precision/validation scoring —
distinct from a confident 'rejected').
the log, last-event-per-pair wins. The log lives in git, so the human judgements
survive a data/ wipe and every change shows up as a reviewable diff.
"""

import argparse
import collections
import csv
import datetime
import html
import os
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import config
import gates
import verify  # for PROMPT_VER/current_ver — the verdict cache is keyed by prompt version

DECISIONS = config.DECISIONS
PORT = 8731

# Review LANE: which population this process reviews. One process serves one lane; the HTTP
# Handler (which can't take params) reads this, set by serve()/status() from the CLI flag.
#   "mono"       n_senses = 1   the pilot write stream (445 written decisions)
#   "senseless"  n_senses = 0   the sense-creation lane
#   "poly"       n_senses >= 2  MATCH on a polysemous target — the verifier picks WHICH sense,
#                               and the reviewer approves that sense, not the lexeme
# The lanes PARTITION the admissible pool (gates.py --check asserts it), so a pair is
# reviewable in exactly one queue and can never be decided two ways.
LANE = "mono"
LANE_N_SENSES = {"mono": 1, "senseless": 0, "poly": "poly"}


LANE_TITLE = {"mono": "", "senseless": " — senseless (creating first sense)",
              "poly": " — polysemous (matching one of several senses)"}
LANE_COL = {"mono": "(its CC0 sense gloss)", "senseless": "(sense to CREATE)",
            "poly": "(which sense to MATCH)"}
LANE_BANNER = {
    "mono": "",
    "senseless": "   [MODE: senseless — first-sense creation]",
    "poly": "   [MODE: polysemous — MATCH one of several existing senses]",
}


def lane_from_args(args):
    """CLI flags -> lane name. --senseless and --poly are mutually exclusive (argparse
    enforces it); neither means the monosemous write stream."""
    if getattr(args, "poly", False):
        return "poly"
    return "senseless" if getattr(args, "senseless", False) else "mono"


def _lane(v=None):
    """Coerce a lane selector: None -> the module mode; a bool -> the legacy senseless flag
    (kept because callers outside this module still pass senseless=True/False)."""
    if v is None:
        return LANE
    if isinstance(v, bool):
        return "senseless" if v else "mono"
    if v not in LANE_N_SENSES:
        raise ValueError(f"unknown review lane {v!r}; expected one of {sorted(LANE_N_SENSES)}")
    return v

# The anchor languages shown in the review UI live in config.CONCEPT_LANGS_DISPLAY — the
# SAME spec/resolver the verifier uses (the verifier just feeds a wider set). Cross-checking
# a scope call against several languages — a demonym vs its ethnic-group concept, say —
# often reads differently across them. The data is the all-language CC0 cache (item_lang,
# fetch_anchor_langs.py); if it is absent the UI falls back to the de/en/sv `item` columns.
# The same fetch also caches aliases (item_alias); the UI surfaces a NEUTRAL hint when the
# lemma exactly matches one of the anchor's 'also known as' names; absent → no hint shown.

# Append-only log columns. Beyond the (lid, anchor) key and the decision itself,
# we snapshot what the reviewer saw (lemma, verdict, reason, model) so each line
# is a self-contained audit record that does not depend on the regenerable caches.
LOG_COLS = ["ts", "target_lid", "anchor_qid", "target_lemma", "target_lang",
            "target_n_senses", "target_sid", "verdict", "reason", "model", "status",
            "reviewer", "revid"]
# `target_sid` is WHICH sense the decision is about, and it exists because the poly lane's
# decision is not about a lexeme. Empty in the mono and senseless lanes, where the sense is
# unambiguous by construction (the one sense, or none yet) — and empty in every row written
# before 2026-08-16, all of which are one of those two lanes.

# The statuses meaning "the human said the verifier-'yes' was correct" (write.py folds
# 'written' into approval) — shared by the audit/validation consumers of the log.
CONFIRMED = ("approved", "written")
# `create` (poly lane only): the assigner abstained and the human says the lemma has this
# meaning but no recorded sense — write.py's create_add action. Deliberately NOT in CONFIRMED
# and not counted by calibrate/auto-defer: it says nothing about the MATCH action's precision.
# The verifier's non-sense answers on a poly row, the only state in which `create` is offered.
ABSTAINED = ("none", "error", "out_of_range")


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_decisions(path=DECISIONS):
    """Replay the append-only log; the last event per (lid, anchor) wins.

    Returns {(target_lid, anchor_qid): {col: value, ...}}."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out[(row["target_lid"], row["anchor_qid"])] = row
    return out


def append_decision(rec, path=DECISIONS):
    """Append one decision event (a dict over LOG_COLS); write the header if new.

    The header is written ONCE, at file creation, so appending a row with more fields than
    the header lists does not fail — it silently writes a wider row that every later
    DictReader hands back with the extra value under the None key. On an append-only log
    that is unrecoverable by design, so a column added to LOG_COLS without migrating the
    file raises here instead."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    if not new:
        with open(path, newline="") as f:
            have = next(csv.reader(f), [])
        if have != LOG_COLS:
            raise RuntimeError(
                f"{path} header does not match LOG_COLS — migrate the file before appending.\n"
                f"  file: {have}\n  code: {LOG_COLS}")
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS)
        if new:
            w.writeheader()
        w.writerow({c: rec.get(c, "") for c in LOG_COLS})


def pool(lane=None, key=None):
    """The reviewable set: verifier-'yes' High-tier candidates (clean ∩ A: >=2
    anchors ∩ B ∩ D: concrete/P31 ∩ in-scope), each with the verdict's reason/model and
    the anchor's CC0 de/en/sv descriptions (the scope-nudge input; the displayed concept
    comes from item_lang). 'pending' = this minus whatever the decision log already covers.
    The trailing support_clusters/prompt_ver columns exist for game_export.py's feed
    metadata — the UI consumers here star-capture past them.

    `senseless` picks the B gate: False → monosemous targets (n_senses=1, the write
    stream); True → senseless targets (n_senses=0, the phase-2 sense-creation lane).
    Defaults to the module SENSELESS mode so the HTTP Handler can call it param-free.
    `key`=(lid, qid) narrows to that one pair (the POST handler's membership check)
    without recomputing the full join or duplicating the gate SQL."""
    lane = _lane(lane)
    cn = sqlite3.connect(config.CANDIDATES)
    cn.execute("ATTACH DATABASE ? AS it", [config.ITEMS])
    cn.execute("ATTACH DATABASE ? AS vf", [config.VERIFY])
    cn.execute("ATTACH DATABASE ? AS wd", [config.WIKIDATA])  # gates.free_sense_clause reads this
    rows = cn.execute(
        """
        SELECT c.target_lid, c.anchor_qid, c.target_lemma, c.target_lang,
               c.target_n_senses, v.verdict, v.reason, v.model,
               c.support_clusters, v.prompt_ver, v.target_sid
        FROM candidate c
        JOIN vf.verdict v ON v.target_lid = c.target_lid AND v.anchor_qid = c.anchor_qid
                         AND """ + verify.current_ver("v") + """
        JOIN it.item i ON i.qid = c.anchor_qid AND NOT i.missing
        WHERE """ + gates.high_tier_where("c", "it", n_senses=LANE_N_SENSES[lane])
        + """ AND v.verdict = 'yes'"""
        + (" AND c.target_lid = ? AND c.anchor_qid = ?" if key else "")
        + """
        ORDER BY c.target_lang, c.target_lemma
        """, list(key) if key else []).fetchall()
    cn.close()
    return rows


def decided_lane(d):
    """Which review lane a DECIDED log row belongs to: True = senseless (the sense-creation
    lane, target_n_senses 0), False = monosemous (the write stream, 1), None = neither.

    Read from the count FROZEN in the log row, never recomputed from the current dump: a
    senseless target that was approved and written HAS a sense now, but the verdict on it
    calibrates the lane it was judged in. Single-sourced here because the status counts, the
    auto-defer gate and calibrate all have to agree on what lane a row belongs to."""
    n = (d.get("target_n_senses") or "").strip()
    if n in ("0", "1"):
        return "senseless" if n == "0" else "mono"
    return "poly" if n.isdigit() else None


def pending_view(lane=None):
    """The shared review pipeline, computed ONCE per invocation: (decisions, undecided,
    ctx, auto-defer classes, verify cache). counts/status/render all consume this — it
    is a 3-DB join plus a full log replay and a verify-cache scan, so each caller runs
    it once and the call sites can't drift apart."""
    lane = _lane(lane)
    decisions = load_decisions()
    vcache = load_verify_cache()
    # `decisions` stays whole: the pending dedup must see BOTH lanes (a decided pair must not
    # resurface, and n_senses moves once a senseless target is written). Only the gate below and
    # the counts() display scope to `senseless`.
    undecided = [r for r in pool(lane) if (r[0], r[1]) not in decisions]
    ctx = demonym_context([(k[0], k[1], d.get("target_lang")) for k, d in decisions.items()]
                          + [(r[0], r[1], r[3]) for r in undecided])
    ad = auto_defer_classes(decisions, ctx, vcache, lane)
    return decisions, undecided, ctx, ad, vcache


def counts(view=None, lane=None):
    """(status -> n) from the log, plus computed 'pending' (human-pending) and 'auto_deferred'.

    The decided counts are scoped to the SAME lane as the pending numbers printed beside them.
    Pooled, senseless mode reported the monosemous lane's 335 writes directly above its own
    340 pending, reading as if the sense-creation lane were nearly worked through when it has
    written nothing at all."""
    want = _lane(lane)
    decisions, undecided, ctx, ad, _ = view or pending_view()
    c = collections.Counter(d["status"] for d in decisions.values()
                            if decided_lane(d) == want)
    c["auto_deferred"] = sum(1 for r in undecided if is_auto_deferred((r[0], r[1]), r[6], ctx, ad))
    c["pending"] = len(undecided) - c["auto_deferred"]
    return c


def status(args):
    global LANE
    LANE = lane_from_args(args)
    print(f"decision log: {DECISIONS}" + LANE_BANNER[LANE])
    view = pending_view(LANE)
    decided, undecided, ctx, ad, vcache = view
    c = counts(view)
    for s in ("pending", "auto_deferred", "approved", "create", "deferred", "rejected", "written"):
        if s != "create" or c.get(s):
            print(f"  {s:<13} {c.get(s, 0):,}")
    if c.get("auto_deferred"):
        print(f"  ↳ auto-deferred classes (≥85% not-written, hidden from review): "
              f"{', '.join(sorted(ad))} — a better prompt/approach re-surfaces them")
    n_scope = sum(1 for r in undecided
                  if not is_auto_deferred((r[0], r[1]), r[6], ctx, ad)
                  and action_of((r[0], r[1]), r[6], ctx) in ("defer", "reject"))
    if n_scope:
        print(f"  ⚠ of pending, {n_scope:,} are broader/narrower/lateral scope mismatches "
              "(review individually — likely defer/reject)")
    # Precision on the WRITE lane: of decided pairs the LIVE verifier rates 'keep' (EQUAL /
    # convention — the only relations that are written), how many the human confirmed. The
    # blended verifier-'yes' rate is misleading — it counts BROADER/NARROWER/LATERAL
    # rejections, which are SCOPE triage the verifier doesn't judge and which never reach a
    # write. Bucketed by the LIVE cache relation (live_rel) so pre-REL approvals still count.
    keep_ok = keep_n = 0
    for k, dec in decided.items():
        if (dec.get("status") or "").strip() not in ("approved", "written", "rejected"):
            continue
        if decided_lane(dec) != LANE:            # lane-scoped like the counts above
            continue
        rel = live_rel(k, dec.get("reason"), vcache, ctx)
        if not rel or REL_ACTION.get(rel) != "keep":
            continue
        keep_n += 1
        keep_ok += (dec.get("status") or "").strip() in ("approved", "written")
    if keep_n:
        print(f"  verifier-keep precision (write lane, EQUAL/convention): {keep_ok}/{keep_n} = "
              f"{100.0 * keep_ok / keep_n:.1f}%  — scope mismatches are triaged, not written; "
              f"see calibrate.py for the per-relation table")
    # Drift: the log snapshots the verifier verdict at decision time, but the cache can change
    # later (e.g. a prompt-version rebuild). Surface decided pairs whose CURRENT cache verdict
    # differs from what was reviewed — informational only (writes are log-driven, not cache-driven).
    if vcache:
        drift = [d["target_lemma"] for k, d in decided.items()
                 if k in vcache and d.get("verdict") and vcache[k][0] != d["verdict"]]
        if drift:
            shown = ", ".join(sorted(drift)[:6]) + ("…" if len(drift) > 6 else "")
            print(f"  ⚠ {len(drift)} decided pair(s) now disagree with the verify cache "
                  f"(prompt changed since review): {shown}")
            print("    → writes are log-driven, so this is informational; "
                  "run audit_decisions.py to inspect")


# ---- minimal approve/reject web UI (stdlib only) ----------------------------

def pick(lang, vals):
    """First non-empty value, target language preferred then en/de/sv."""
    for code in [lang] + [x for x in ("en", "de", "sv") if x != lang]:
        if vals.get(code):
            return vals[code], code
    return "", None


def has_item_lang(it):
    """Whether the all-language anchor cache (fetch_anchor_langs.py) is populated."""
    return bool(it.execute("SELECT name FROM sqlite_master "
                           "WHERE type='table' AND name='item_lang'").fetchone())


def display_spec(lang=None):
    """config.CONCEPT_LANGS_DISPLAY, with the target language prepended when it isn't in it.

    The display list is a curated eight and does not cover every review language: `ru` is absent,
    so a Russian row showed its anchor only in other languages and — worse — `anchor_gloss` could
    never pick a Russian description, giving every created Russian sense an English gloss instead.
    1,370 of the senseless lane's 1,895 Russian pairs have a Russian description available
    (notes/senseless_lane_findings.md). This is scoped to what a human reads and to what gets
    WRITTEN; the verifier's own concept spec (CONCEPT_LANGS_VERIFY) already carries ru and is
    deliberately not touched here, because its content is an input to a graded prompt."""
    if not lang or any(lang == d or lang in srcs for d, srcs in config.CONCEPT_LANGS_DISPLAY):
        return config.CONCEPT_LANGS_DISPLAY
    return [(lang, (lang,))] + config.CONCEPT_LANGS_DISPLAY


def anchor_langs(it, qid, lang=None):
    """[(display_lang, label, desc), ...] for the display languages present for qid.

    Thin wrapper over the shared resolver (Norwegian collapses nb/no/nn to the first
    available; a language is included if it has a label OR a description). `lang` is the review
    row's target language — see display_spec."""
    entry = {lg: (lab, ds) for lg, lab, ds in
             it.execute("SELECT lang, label, desc FROM item_lang WHERE qid=?", [qid])}
    return config.resolve_concept_langs(entry, display_spec(lang))


def anchor_cc0(it, qid, lang):
    """(label, desc) for the anchor Q in `lang` from the item_lang cache. Either may be None."""
    r = (it.execute("SELECT label, desc FROM item_lang WHERE qid=? AND lang=?",
                    [qid, lang]).fetchone() if has_item_lang(it) else None)
    return r or (None, None)


def anchor_gloss(it, qid, lang, lemma):
    """Glosses {gloss_lang: text} for a would-be new (senseless-target) sense: the anchor's
    CC0 DESCRIPTION, target language first, else the first other language that has one.
    Empty dict = no usable description anywhere = skip the candidate.

    A description that just restates the headword is not a gloss — "Parasit" defines nothing
    about *Parasit* — so `lemma` is required and such a description is passed over for the
    next language rather than accepted. It fires on 37 of the 8,888 senseless candidates,
    whose Wikidata description field genuinely IS the word (the other 417 headword-glosses
    used to arrive via a label fallback that no longer exists), and costs no skips: every one
    had a real description in another language. Compared on letters alone, because the
    restatement is often re-spelled — de 'Striptease-Tänzer/in' IS 'Stripteasetänzerin'.
    Only the headword itself is rejected: terse-but-real descriptions ("Holzart" for
    Ebenholz) still define, and dropping those would cost far more than it saves.

    DESCRIPTIONS ONLY — never a label. A label is a NAME, a gloss must DEFINE, so field type
    outranks language: a French description beats a German label. The old rule resolved the
    other way (`desc or lab` per language, target then en) and it cost real damage — of the
    769 senseless candidates whose anchor lacks a target-language description, 417 would have
    created a sense glossed with its own headword (de Parasit -> "Parasit") and 325 more with
    a bare one-word synonym. Those anchors are not undescribed: 654 of the 769 carry
    descriptions in 5+ OTHER languages, 518 in 10+. The code was discarding a real definition
    to reach for a name in the right language. Dropping the label branch also finally makes
    write.py's "never fall back to the headword (degenerate)" guard true — it only ever fired
    on total absence, while the headword walked in through this fallback.

    Keyed by the language the text is ACTUALLY in — the fix for the old bug where an
    en-fallback string was written under a `de`/`fr` key — plus an extra en gloss when the
    chosen one isn't English, so a non-target-language patroller can read the created sense.
    CC0 only; a DBnary gloss is never read. This is the single source of the gloss text: the
    review UI previews exactly what write.py will write, both calling here."""
    def bare(s):   # compare on letters alone: de 'Striptease-Tänzer/in' IS 'Stripteasetänzerin'
        return "".join(ch for ch in (s or "").casefold() if ch.isalnum())

    headword = bare(lemma)

    def usable(ds):
        return ds and bare(ds) != headword

    if has_item_lang(it):
        entry = {lg: (lab, ds) for lg, lab, ds in
                 it.execute("SELECT lang, label, desc FROM item_lang WHERE qid=?", [qid])}
        described = [(d, ds) for d, _lab, ds in config.resolve_concept_langs(
            entry, display_spec(lang), target=lang) if usable(ds)]
    else:   # pre-fetch / fixture: only the de/en/sv columns of `item` exist
        pairs = [(lg, anchor_cc0(it, qid, lg)[1]) for lg in dict.fromkeys([lang, "en"])]
        described = [(lg, ds) for lg, ds in pairs if usable(ds)]
    if not described:
        return {}
    out = {described[0][0]: described[0][1]}
    for lg, ds in described:
        if lg == "en":
            out.setdefault("en", ds)   # legibility gloss; no-op when en was already chosen
            break
    return out


def has_item_alias(it):
    """Whether the anchor alias cache (fetch_anchor_langs.py) is populated."""
    return bool(it.execute("SELECT name FROM sqlite_master "
                           "WHERE type='table' AND name='item_alias'").fetchone())


def alias_match(it, qid, lang, lemma):
    """The concept's target-language alias equal to the lemma, if any (case-insensitive); '' else.

    A hit means Wikidata officially lists the lemma as one of Q's 'also known as' names (its
    aliases EXCLUDE the label, so this is a non-label name). Shown as NEUTRAL reviewer context,
    never acted on: on the gold an exact alias match is ~50/50 keep-vs-reject-acceptable —
    genuine extra evidence the reviewer weighs against the scope relation, not an auto-keep.
    Data is the item_alias cache (fetch_anchor_langs.py)."""
    row = it.execute("SELECT aliases FROM item_alias WHERE qid=? AND lang=?", [qid, lang]).fetchone()
    if not row or not row[0]:
        return ""
    want = (lemma or "").strip().lower()
    return next((a for a in row[0].split("; ") if a.strip().lower() == want), "")


def create_evidence(it, wd, lid, qid, lang, lemma):
    """('named' | 'owned' | '', [(lid, lemma)]) — the two create-row signals, measured on
    Karl's 87 create-class rulings (2026-10-08): 'named' = the item's own `lang` label or
    alias IS the lemma, case-exact (22/26 created); 'owned' = not named, and another `lang`
    lexeme already links the item or is named after it (7/30 created — mostly a polysemous
    foreign pivot word, krets <- Kreis = circle). Case-exact is what separates fr Croate
    (person) from croate (language). Advisory only: neither gates anything."""
    names = set()
    r = it.execute("SELECT label FROM item_lang WHERE qid=? AND lang=?", [qid, lang]).fetchone()
    if r and r[0]:
        names.add(r[0])
    r = it.execute("SELECT aliases FROM item_alias WHERE qid=? AND lang=?", [qid, lang]).fetchone()
    if r and r[0]:
        names |= {a.strip() for a in r[0].split("; ")}
    if lemma in names:
        return "named", []
    if lemma.casefold() in {n.casefold() for n in names}:
        return "", []
    owners = {l for (l,) in wd.execute(
        "SELECT DISTINCT s.lid FROM sense_item s JOIN lexeme x ON x.lid = s.lid "
        "WHERE s.qid = ? AND x.lang = ? AND s.lid <> ?", [qid, lang, lid])}
    for n in names:
        owners |= {l for (l,) in wd.execute(
            "SELECT DISTINCT m.lid FROM lemma m JOIN lexeme x ON x.lid = m.lid "
            "WHERE m.lemma = ? AND x.lang = ? AND x.lexcat = ? AND m.lid <> ?",
            [n, lang, config.NOUN_QID, lid])}
    if not owners:
        return "", []
    return "owned", [(l, (wd.execute("SELECT lemma FROM lemma WHERE lid=?", [l]).fetchone()
                          or [""])[0]) for l in sorted(owners)[:3]]


# ---- scope triage -----------------------------------------------------------
# The verifier's TOLERANT criterion maps EQUAL/BROADER/NARROWER/LATERAL -> 'yes', so a
# scope-mismatched pair still reaches review on a surface-plausible read. But P5137
# asserts the sense denotes that EXACT concept, so a broader/narrower link is usually a
# hold (defer), and a LATERAL one (a process vs. its system, an attribute vs. the whole —
# adjacent, not a true is-a) usually a reject — UNLESS the scope difference is one the link
# standard blesses as EQUAL: a demonym individual vs the people-collective, or a
# gender-specific form (the relation lives on the lexeme, not the sense). We surface the
# verifier's relation token and an ADVISORY suggestion so these aren't bulk-approved unseen;
# the human still decides via the buttons. We deliberately do NOT factor in whether a
# better-scoped Wikidata item exists — its absence is usually just a gap in Wikidata, not a
# signal about this link. See notes/p5137_anchor_propagation.md "Reviewer guidance"
# (dominant-referent vs lateral/wrong-rank near-synonym).
_CONVENTION = ("collective", "demonym", "citizen", "resident", "ethnic",
               "nationals", " people", "gender", "masculine", "feminine", "female", "male")

# Demonym→ethnic-group split (user call 2026-06-26, see broader-narrower-triage): a demonym
# lexeme (gloss 'person from X') linked to an ETHNIC-GROUP anchor is a contested keep — a
# person-by-residence is not the ethnic group — so don't auto-keep it. A demonym linked to a
# NATIONALITY anchor ('citizens/residents of X') stays a keep. The discriminator is whether
# the anchor's own description carries an ethnic-group marker (vs only a citizen/resident one).
# Validated on the 118-case gold: turns +multi's only safety weakness into a strict win.
# Gloss patterns per pilot language, grounded in the actual sense_gloss phrasing:
# en/sv/de 'person from/från/aus X', nb 'person fra X',
# fr 'habitant(e) ou originaire de X' / 'personne|citoyenne ou résident(e) de X'.
_DEMONYM_GLOSS = ("person from", "person från", "person aus", "person av",
                  "person fra", "ou originaire", "ou résident")
_ETHNIC_ANCHOR = ("ethnic group", "etnisk grupp", "etnisk gruppe", "ethnic", "ethnie",
                  "ethnique", "slavic", "folkgrupp", "folkeslag", "people native",
                  "indigenous to", "kaukasisk folk", "südkaukasisches volk")
# The anchor_desc these markers scan is deliberately de/en/sv-narrow — NOT the verifier's
# ~13-language +multi concept — and that is the RIGHT scope, not a leftover. A/B on the 118-case
# gold (2026-07-05): widening anchor_desc to every item_lang language changes ZERO outcomes
# (de/en/sv already carries the ethnic signal, so extra text adds no marker hits); and broadening
# the markers to match other languages' etn-root terms REGRESSES — it over-fires on nationality
# demonyms (luxemburgare: keep -> needless defer) while fixing no false-keep. So keep it narrow.
# Composed in ONE place, demonym_context, so every consumer feeds is_ethnic_demonym identical text.


def is_ethnic_demonym(gloss, anchor_desc):
    """A demonym lexeme linked to an ethnic-group anchor (not a nationality one)."""
    if not gloss or not anchor_desc:
        return False
    if not any(p in gloss.lower() for p in _DEMONYM_GLOSS):
        return False
    return any(m in anchor_desc.lower() for m in _ETHNIC_ANCHOR)


# kind -> (action, reviewer-facing suggestion, css class). The ACTION token is what policy
# consumers branch on (rel_class, the queue sort/counters);
# the suggestion is display copy only — so rewording a hint can never silently change
# triage behaviour again (it used to: the prose was parsed back by substring).
HINTS = {
    "lateral":        ("defer", "defer? (lateral — adjacent, not is-a)", "hold"),
    "incoherent":     ("defer", "defer? (incoherent anchor — no basis to judge)", "hold"),
    "demonym-ethnic": ("defer", "defer? (demonym vs ethnic group)", "hold"),
    "convention":     ("keep", "keep? (demonym/gender convention)", "ok"),
    "scope":          ("defer", "defer? (scope mismatch)", "hold"),
    "missing":        ("create", "create? (the noun has this meaning; no recorded sense does)", "ok"),
    "":               ("", "", ""),
}


def parse_reason(reason):
    """(relation, tags) from a verdict reason head. tags is None for a LEGACY prose reason.

    A structured reason (verify.compose_reason, from the model's own boolean fields) has the shape
    '<REL>·<tag>·<tag>: prose' and always carries at least '·plain', so the presence of any tag is
    what distinguishes it from the pre-2026-08-16 '<REL>: prose' rows. Both shapes stay readable
    and both stay in the cache — it is keyed by prompt version — so this has to classify either."""
    head = (reason or "").split(":", 1)[0].strip()
    parts = [p.strip().lower() for p in head.split("·")]
    rel = parts[0].upper()
    return rel, (set(parts[1:]) if len(parts) > 1 else None)


def scope_kind(reason, gloss=None, anchor_desc=None):
    """(relation, kind) for a verdict reason — the ONE scope classifier.

    relation is the verifier's leading REL token (EQUAL/BROADER/NARROWER/LATERAL/INCOHERENT/…);
    kind keys HINTS ('' = no scope flag: EQUAL or untagged).

    STRUCTURED rows carry the model's own gendered_form / demonym / anchor_is_ethnic_group
    booleans, and the triage reads those. LEGACY rows fall through to the original path: scrape
    _CONVENTION out of the prose, and use gloss + anchor_desc for the demonym→ethnic-group nudge.
    The legacy path is kept verbatim rather than retired because the cache still holds those rows
    and `validate_prompt --baseline-ver` grades against them.

    The two paths deliberately disagree in one place, and that disagreement is the point: without
    a target gloss the legacy nudge cannot fire, so a demonym→ethnic-group pair fell through to
    _CONVENTION, matched the word 'ethnic' there, and came out a *keep* — the inverse of the
    intended action, and the largest error class measured in the senseless lane. The structured
    path decides it from anchor_is_ethnic_group, which needs no gloss."""
    rel, tags = parse_reason(reason)
    if tags is not None:
        if "missing-sense" in tags:     # no listed sense is this meaning: create, whatever the rel
            return rel, "missing"
        if rel == "LATERAL":
            return rel, "lateral"
        if rel == "INCOHERENT":
            return rel, "incoherent"
        if "demonym" in tags and "ethnic-anchor" in tags:
            return rel, "demonym-ethnic"      # contested keep → defer, at ANY relation
        if rel not in ("BROADER", "NARROWER"):
            return rel, ""
        # Same asymmetry as the legacy path: the convention excuses a NARROWER lexeme (a female
        # form, a national of X) denoting the neutral/collective concept, never a BROADER one.
        if rel == "NARROWER" and ({"gendered", "demonym"} & tags):
            return rel, "convention"
        return rel, "scope"
    if rel == "LATERAL":
        # Adjacent / not-is-a is a JUDGMENT CALL, not an auto-drop: the verifier can't tell a
        # lateral-that-should-reject (arrangemang) from one that should go to a human
        # (barf=substance-vs-act, bekvämlighet=amenity-vs-state) by the relation alone, so we
        # defer rather than reject — surfacing for review never silently drops a good link.
        return rel, "lateral"
    if rel == "INCOHERENT":
        return rel, "incoherent"
    if is_ethnic_demonym(gloss, anchor_desc):
        return rel, "demonym-ethnic"
    if rel not in ("BROADER", "NARROWER"):
        return rel, ""
    # Demonym/gender convention: a gendered or demonymic lexeme still denotes the neutral
    # concept (sex/scope recorded separately) — but ONLY when the lexeme is NARROWER than (or
    # equal to) the concept. A BROADER lexeme is a real scope mismatch even if the reason
    # mentions a gender word: a generic word ('hog' = adult pig) is NOT the sex-specific
    # concept ('female pig'), so it must not be auto-kept by the convention nudge.
    if rel == "NARROWER" and any(k in (reason or "").lower() for k in _CONVENTION):
        return rel, "convention"
    return rel, "scope"


def scope_hint(reason, gloss=None, anchor_desc=None):
    """(relation, suggestion, css_class) — the display form of scope_kind."""
    rel, kind = scope_kind(reason, gloss, anchor_desc)
    _action, sug, cls = HINTS[kind]
    return rel, sug, cls


# ---- polysemy-collapse advisory ---------------------------------------------
# The verifier is keyed (target_lid, anchor_qid) — it never sees that the SOURCE headword is
# polysemous. High-tier guarantees target_n_senses=1, so P5137 lands on the target's single WD
# sense. When that sense is UNDIFFERENTIATED (gloss == the bare lemma/translation) and the
# target-language WikDict headword splits into >=2 senses that collapse onto this same anchor (a
# homograph leak in propagation), the anchor may over-specify the single sense. koncentration:
# WikDict 01 'mental focus' + 02 '(chemistry)' both -> Q3686031, and L476208's only sense is
# glossed just 'concentration'. NEUTRAL reviewer context (like alias_match) — the human weighs it;
# we deliberately DON'T feed it to the verifier or auto-defer (would be noisy — the benign majority,
# metonymy / species-vs-individual, is filtered out by the undifferentiated-gloss gate).
# In the SENSELESS lane there is no gloss, so that gate is vacuous: the flag fired on every
# collapse row and predicted nothing, so render() turns it off there. (Creating S1 for a minor
# meaning breaks no rule anyway — sense ids are never renumbered, so rank is a display artifact.)
_LANG3TO2 = {"swe": "sv", "fra": "fr", "deu": "de", "eng": "en", "nob": "no",
             "nno": "no", "nor": "no", "nld": "nl", "dan": "da", "epo": "eo"}


_POLY_IDX = None


def polysemy_index(cand):
    """{(target_lid, anchor_qid): {lang3_prefix: max_sense_count}} for source headwords whose
    >=2 WikDict senses collapse onto a (target, anchor). candidate_raw is unindexed (83k rows)
    and immutable while a server runs (match.py rebuilds the whole DB), so scan it ONCE per
    process — not per render, which happens on every GET and after every decision click."""
    global _POLY_IDX
    if _POLY_IDX is None:
        idx = collections.defaultdict(dict)
        for tlid, qid, lexentry, n in cand.execute(
                "SELECT target_lid, anchor_qid, lexentry, COUNT(DISTINCT sense_num) "
                "FROM candidate_raw GROUP BY target_lid, anchor_qid, lexentry "
                "HAVING COUNT(DISTINCT sense_num) >= 2"):
            pref = lexentry.split("/")[0]
            d = idx[(tlid, qid)]
            d[pref] = max(d.get(pref, 0), n)
        _POLY_IDX = idx
    return _POLY_IDX


def undifferentiated_gloss(gloss, lemma):
    """The target's single CC0 sense gloss can't disambiguate: empty, == the lemma, or a bare
    1-2 word stub (the bare translation, e.g. 'concentration')."""
    g = (gloss or "").strip()
    return (not g) or g.lower() == (lemma or "").strip().lower() or len(g.split()) <= 2


def polysemy_hint(idx, lid, qid, lang, lemma, gloss):
    """n_source_senses when the over-specification signal fires: the TARGET-LANGUAGE source
    headword is polysemous (>=2 senses collapse here) AND the target's single WD sense gloss is
    undifferentiated; else 0. Neutral advisory — see section note."""
    n = max((cnt for pref, cnt in idx.get((lid, qid), {}).items() if _LANG3TO2.get(pref) == lang),
            default=0)
    return n if n >= 2 and undifferentiated_gloss(gloss, lemma) else 0


# ---- anchor-independence advisory -------------------------------------------
# Gate A ('>=2 agreeing anchor clusters') counts CLUSTERS, not anchors — and several clusters
# can all trace back to ONE anchoring lexeme, i.e. one editor's single P5137 statement. A
# polysemous source headword even manufactures its own support: de Chronologie → Q67439188
# passed gate A with 4 clusters that are 3 collapsed senses of deu/Chronologie plus one Polish
# one, every last one anchored by sv L1529730 alone. So support_clusters >= 2 does NOT mean two
# independent statements agreed: 43% of the High tier (616/1448) and 35% of the senseless lane
# (3078/8888) rest on a single anchor lexeme. The decided log separates on it — sole-anchor
# pairs run 53% approved / 18% rejected against 74% / 8% for two-or-more (n=753, 2026-08-02).
# It is NEUTRAL on its own — EQUAL is 93% approved with one anchor or five, so this never floats a
# row or re-sorts the queue (a third of the pool would swamp the concern band). What it does key is
# the auto-defer gate, as a REFINEMENT of the scope classes, where the same split is decisive: see
# SOLE_ANCHOR / rel_tokens below.
_ANCHOR_IDX = None


# ---- crowd advisory ----------------------------------------------------------
# What the distributed game already saw. NEUTRAL and display-only: it floats nothing and
# feeds no gate, because the crowd's two sides are not equally good — measured on trap
# tiles (2026-09-12), non-spammers run 93% on approved traps but 54% on rejected ones, so
# a lone "no" is barely evidence while agreement on "yes" is strong. import_crowd.py owns
# the tiering and the promotion rule; this only shows what it filtered, in particular the
# pairs the service will never serve again — read as "once the current mute list is
# deployed", since a spammer found at import only stops counting there on the next push.
_CROWD = None


def crowd_index():
    """{(target_lid, anchor_qid): (yes_users, no_users, stranded)} from the vote archive,
    spammer votes and skips dropped. `stranded` marks a pair the game has stopped serving
    without our threshold being met — the local queue is where it can still be finished.

    Imported lazily: import_crowd imports THIS module, so a top-level import would cycle."""
    global _CROWD
    if _CROWD is not None:
        return _CROWD
    _CROWD = {}
    try:
        import import_crowd as ic
        by_sid, _newest = ic.load_feeds()
        latest = ic.latest_votes(ic.load_archive())
        tiers = ic.classify(ic.trap_scores(latest, by_sid, ic.first_decisions()),
                            {u for _s, u in latest})
    except (SystemExit, OSError, ValueError):
        return _CROWD          # no feeds or no archive yet — the queue works without it
    honest = collections.defaultdict(lambda: ([], []))
    for (sid, user), r in latest.items():
        if by_sid.get(sid, {}).get("gold") or r["decision"] == "skip":
            continue           # trap tiles never reach the queue as themselves
        if tiers[user] != "spammer":
            honest[ic.sid_key(sid)][r["decision"] == "no"].append(user)
    for key, (yes, no) in honest.items():
        met = (sum(ic.PROMOTE_WEIGHT[tiers[u]] for u in yes) >= 2
               or sum(ic.PROMOTE_WEIGHT[tiers[u]] for u in no) >= 2)
        stranded = not met and (len(yes) >= ic.SERVE_K or len(no) >= ic.SERVE_K
                                or len(yes) + len(no) >= ic.SERVE_CAP)
        _CROWD[key] = (sorted(yes), sorted(no), stranded)
    return _CROWD


def anchor_support_index():
    """{(target_lid, anchor_qid): n distinct anchoring LEXEMES} — how many separate P5137-bearing
    lexemes actually back a pair, as against candidate.support_clusters, which counts clusters and
    so can count one anchor several times. Scanned ONCE per process (a 125k-row join over 706k
    clean_anchor rows, ~4s) like polysemy_index, and cached on the same premise: candidates.sqlite3
    is immutable while a server runs — match.py rebuilds the whole DB."""
    global _ANCHOR_IDX
    if _ANCHOR_IDX is None:
        cn = sqlite3.connect(config.CANDIDATES)
        _ANCHOR_IDX = {(lid, qid): n for lid, qid, n in cn.execute(
            "SELECT r.target_lid, r.anchor_qid, COUNT(DISTINCT a.lid) "
            "FROM candidate_raw r JOIN clean_anchor a "
            "  ON a.lexentry = r.lexentry AND a.sense_num = r.sense_num AND a.qid = r.anchor_qid "
            "GROUP BY r.target_lid, r.anchor_qid")}
        cn.close()
    return _ANCHOR_IDX


# ---- data-gated auto-defer (enforcement) ------------------------------------
# A relation CLASS whose decided history is >=85% "not written" (reject+defer) on a big enough
# sample is one the human almost never approves — so we stop bothering the human with it: auto-defer
# HIDES it from the pending queue WITHOUT logging a decision (user call 2026-06-27). This is
# reversible BY CONSTRUCTION: the filter keys on the live verdict relation, so when a better prompt
# re-verifies a pair into a different class it reappears in review on its own — no un-defer step. The
# cost is the small fraction of these the human WOULD have approved (LATERAL ~7%); they sit unwritten
# until a prompt/approach improvement re-surfaces them. The instrument calibrate.py reports the SAME
# gate by importing these names, so the readiness table and the live filter can't drift apart —
# `calibrate.py --lane X` is the matching view now that the gate is measured per review lane
# (auto_defer_classes); calibrate's pooled default deliberately corresponds to no live gate.
#
# A class can also gate on its ·sole-anchor REFINEMENT (anchor_support_index: every gate-A support
# cluster traces to one anchoring lexeme). BROADER pooled is 78% not-written and stays manual, but
# its sole-anchor half is 90% on 42 pairs while the two-or-more half approves 21 of 73 — one class
# token hiding two populations. Refinements are NESTED, not a partition: a pair counts under both
# its class and its refinement, and is hidden if EITHER cleared the gate. So enforcement only ever
# grows by splitting — a refinement too small to clear (LATERAL·sole-anchor, 18) never un-hides
# what its parent already gates, which a partition would have done.
GATE_MIN_SAMPLES = 30
AUTO_DEFER_NOWRITE = 0.85
SOLE_ANCHOR = "·sole-anchor"
KNOWN_RELS = ("EQUAL", "BROADER", "NARROWER", "LATERAL", "INCOHERENT", "DIFFERENT")
# class -> the action scope_hint suggests for it (which gate that class faces). Only a 'defer' class
# can auto-defer; a 'keep'/'reject' class never does, even if its not-written rate is high.
REL_ACTION = {
    "EQUAL": "keep", "BROADER·convention": "keep", "NARROWER·convention": "keep",
    "demonym-ethnic": "defer",   # demonym lexeme vs an ethnic-group anchor: a contested keep → defer
    "BROADER": "defer", "NARROWER": "defer", "LATERAL": "defer", "INCOHERENT": "defer",
    "DIFFERENT": "reject",
    # the verifier saw the sense inventory and said no listed sense is this meaning, but the
    # noun has it: the create_add action (a new sense), never a write to an existing sense
    "MISSING": "create",
}


def rel_class(reason, gloss=None, anchor_desc=None):
    """Bucket a verdict by its relation token, matching scope_hint's splits.

    Pass gloss + anchor_desc so the demonym→ethnic-group nudge buckets as its own defer class
    HERE too. scope_hint is the single classifier, but rel_class used to call it gloss-blind, so
    the gloss-aware HTML queue and the gloss-blind replay paths (calibrate, auto-defer) drifted on
    demonym pairs (litauer/norrman read EQUAL→keep in calibrate while the queue already deferred
    them). Feeding the same inputs everywhere removes that drift. The nudge is one deterministic
    'contested keep → defer' rule that OVERRIDES the verifier's EQUAL/BROADER guess, so all such
    pairs collapse into a single 'demonym-ethnic' class regardless of the overridden relation.
    Pre-REL free-text reasons group as '(legacy, pre-REL)' and never auto-defer."""
    rel, kind = scope_kind(reason, gloss, anchor_desc)
    if rel not in KNOWN_RELS:
        return "(legacy, pre-REL)"
    if kind == "missing":                          # inventory shown, no sense fits → create class
        return "MISSING"
    if kind == "demonym-ethnic":                   # demonym→ethnic-group nudge → single defer class
        return "demonym-ethnic"
    if kind == "convention":
        return rel + "·convention"
    return rel


def rel_of(key, reason, ctx=None):
    """rel_class for a pair, pulling its (gloss, anchor_desc) from a demonym_context map so the
    one gloss-aware classifier is reused at every call site."""
    gloss, _glang, adesc = (ctx or {}).get(key, (None, None, None))
    return rel_class(reason, gloss, adesc)


def rel_tokens(key, cls):
    """The class token(s) a pair counts under: its relation class, plus the '·sole-anchor'
    refinement when a single lexeme anchors it. Nested, so the decided counts a gate reads and
    the pending rows it hides are keyed identically at every site (auto_defer_classes, the
    queue filter, calibrate)."""
    return (cls, cls + SOLE_ANCHOR) if anchor_support_index().get(key) == 1 else (cls,)


def rel_action(cls):
    """The gate a class token faces. A ·sole-anchor refinement faces the SAME gate as its parent
    class — the refinement says who is in the bucket, not what may be done with it."""
    return REL_ACTION.get(cls[:-len(SOLE_ANCHOR)] if cls.endswith(SOLE_ANCHOR) else cls)


def is_auto_deferred(key, reason, ctx, ad):
    """Whether the gate hides this pending pair: its class OR its ·sole-anchor refinement cleared."""
    return any(t in ad for t in rel_tokens(key, rel_of(key, reason, ctx)))


def action_of(key, reason, ctx=None):
    """The triage action ('', 'keep', 'defer', 'reject') scope_kind suggests for a pair,
    gloss-aware via a demonym_context map — the token the queue sort and counters key on."""
    gloss, _glang, adesc = (ctx or {}).get(key, (None, None, None))
    return HINTS[scope_kind(reason, gloss, adesc)[1]][0]


def load_verify_cache(path=config.VERIFY, ver=verify.PROMPT_VER):
    """{(target_lid, anchor_qid): (verdict, reason)} from a verify cache.

    `ver` selects ONE prompt version — the live one by default, since every caller here
    (auto-defer, calibrate, the drift warning) means "what does the current prompt say".
    Pass ver=None to read a cache regardless of version, which is what an old single-prompt
    backup file needs; on the live cache that would silently mix versions per pair."""
    if not os.path.exists(path):
        return {}
    cn = sqlite3.connect(path)
    sql = "SELECT target_lid, anchor_qid, verdict, reason FROM verdict"
    out = {(lid, qid): (verd, reason) for lid, qid, verd, reason in
           (cn.execute(sql) if ver is None else
            cn.execute(sql + " WHERE prompt_ver = ?", [ver]))}
    cn.close()
    return out


def demonym_context(pairs):
    """{(lid, qid): (gloss, gloss_lang, anchor_desc)} — the demonym→ethnic-group nudge's inputs
    (the lexeme's CC0 sense gloss + the anchor's de/en/sv description), joined ONCE in two
    batched queries so every relation-classification site can feed rel_of/rel_class identical
    data. Without this the replay paths (calibrate, auto_defer) and the render filter classify
    gloss-blind and drift from the gloss-aware HTML queue. gloss_lang rides along so render can
    show the gloss without re-querying per row. `pairs`: iterable of (lid, qid, lang)."""
    pairs = [p for p in pairs if p[0] and p[1]]
    if not pairs:
        return {}
    lids = {p[0] for p in pairs}
    qids = {p[1] for p in pairs}
    wd = sqlite3.connect(config.WIKIDATA)
    glosses = collections.defaultdict(dict)
    for lid, lg, g in wd.execute(
            "SELECT lid, lang, gloss FROM sense_gloss WHERE lid IN (%s)"
            % ",".join("?" * len(lids)), list(lids)):
        glosses[lid][lg] = g
    wd.close()
    it = sqlite3.connect(config.ITEMS)
    parts = collections.defaultdict(list)
    for q, d in it.execute(
            "SELECT qid, desc FROM item_lang WHERE lang IN ('de','en','sv') AND desc IS NOT NULL "
            "AND qid IN (%s) ORDER BY qid, lang" % ",".join("?" * len(qids)), list(qids)):
        parts[q].append(d)
    it.close()
    descs = {q: " | ".join(ds) for q, ds in parts.items()}
    return {(lid, qid): (*pick(lang, glosses.get(lid, {})), descs.get(qid, ""))
            for lid, qid, lang in pairs}


def occupancy_context(pairs):
    """{(lid, qid): bool} — True when the anchor item ALREADY carries a P5137 sense from a
    DIFFERENT lexeme in the target's OWN language whose lemma is that item's label in that
    language. The canonical same-language slot is filled by an exact match, so the candidate
    is a second, redundant bid rather than the only one (de Benehmen → Q9332, where
    L3386-S2 Verhalten already anchors it).

    MEASUREMENT ONLY — calibrate.py --occupancy splits its buckets on this to test whether a
    scope-mismatch class is safer to auto-defer when the slot is taken. Nothing live keys on
    it: occupancy is NOT a reject signal by itself (EQUAL·occupied still runs 122/137 = 89%
    approved — same-concept synonymy is exactly what the item-as-pivot model expects).
    `pairs`: iterable of (lid, qid, lang), same shape demonym_context takes."""
    pairs = [p for p in pairs if p[0] and p[1] and p[2]]
    if not pairs:
        return {}
    qids = {p[1] for p in pairs}
    langs = {p[2] for p in pairs}
    hole = ",".join("?" * len(qids))
    wd = sqlite3.connect(config.WIKIDATA)
    incumbent = collections.defaultdict(set)          # (lang, qid) -> {(lid, lemma)}
    for lid, lang, lemma, qid in wd.execute(
            "SELECT si.lid, l.lang, lm.lemma, si.qid FROM sense_item si "
            "JOIN lexeme l ON l.lid = si.lid JOIN lemma lm ON lm.lid = si.lid "
            "WHERE si.qid IN (%s)" % hole, list(qids)):
        if lang in langs:
            incumbent[(lang, qid)].add((lid, lemma))
    wd.close()
    it = sqlite3.connect(config.ITEMS)
    label = {(q, lg): lb for q, lg, lb in it.execute(
        "SELECT qid, lang, label FROM item_lang WHERE qid IN (%s)" % hole, list(qids))
        if lg in langs and lb}
    it.close()
    return {(lid, qid): any(other != lid and lemma == label.get((qid, lang))
                            for other, lemma in incumbent.get((lang, qid), ()))
            for lid, qid, lang in pairs}


def live_rel(key, log_reason, cache, ctx=None, require_yes=True):
    """Relation to bucket a decided pair by, preferring the LIVE verify cache (the current
    prompt's rating of the pair) over the frozen decision-log reason — so pairs reviewed
    before the relation taxonomy was emitted still bucket correctly instead of piling into
    '(legacy, pre-REL)'. The human verdict judges the (lemma, anchor) pair itself, not the
    prompt, so pairing it with the live relation is sound. ctx (from demonym_context) supplies
    the gloss + anchor_desc so the demonym→ethnic defer is applied here exactly as in the render
    queue — same classifier, same inputs. Returns None when the cache now rates the pair NOT 'yes'
    (the verifier would reject it — not write-eligible, so it must not inform the keep/defer/reject
    gates).

    `require_yes=False` lifts that last rule, and exactly one caller wants it: calibrate's
    readiness table. The auto-hide REJECT gate is defined over reject-rated classes, i.e.
    verifier-'no' pairs — and dropping them here meant that gate could never leave "hold", however
    much review was spent on them (fixed 2026-08-16). The queue's own use of
    live_rel keeps the default: a pair the verifier now rejects is not write-eligible and must not
    feed the keep/defer gates."""
    hit = cache.get(key)
    if hit is not None:
        verdict, reason = hit
        if verdict != "yes" and require_yes:
            return None
        return rel_of(key, reason, ctx)
    return rel_of(key, log_reason, ctx)


def auto_defer_classes(decisions=None, ctx=None, cache=None, lane=None):
    """{rel_class}: defer-classes the log proves are >=85% not-written on >=GATE_MIN_SAMPLES pairs
    IN THE LANE BEING REVIEWED (senseless — sense creation — or the monosemous write stream).

    These are hidden from the pending queue. Buckets each decided pair by its LIVE verify-cache
    relation (live_rel) — the SAME basis calibrate uses — so the gate that HIDES pairs and the
    readiness table that justifies it can't drift apart: a prompt re-verify that moves a pair's
    relation moves it in both at once. (Bucketing by the frozen log reason silently diverged once
    a re-verify changed the live relations — e.g. NARROWER read 90% not-written frozen but 73%
    live.) A pair the cache now rates not-'yes' drops out (not write-eligible, not a defer-class
    member). ctx (from demonym_context) keeps it gloss-aware so a demonym→ethnic pair buckets as
    its own class rather than hiding inside EQUAL.

    Lane scoping: pooling the lanes let a class hide pairs in a lane that had never voted on
    it — LATERAL cleared the gate 3/41 written with all 41 monosemous and zero senseless, and
    hid 11 senseless pairs on that basis. The gate now speaks only for the lane it measured, so
    a lane with fewer than GATE_MIN_SAMPLES decided pairs auto-defers nothing and is reviewed by
    hand until it has voted. `calibrate.py --lane X` reproduces exactly this set for lane X; the
    pooled default table there matches no live gate — it is the readiness view, not the filter.

    Returns class tokens, which include the ·sole-anchor refinements (rel_tokens) — a pair is
    hidden when EITHER its class or its refinement is in the set (is_auto_deferred)."""
    if decisions is None:
        decisions = load_decisions()
    if cache is None:
        cache = load_verify_cache()
    want = _lane(lane)
    buckets = collections.defaultdict(collections.Counter)
    for key, d in decisions.items():
        st = (d.get("status") or "").strip()
        if st not in ("approved", "rejected", "deferred", "written"):
            continue
        if decided_lane(d) != want:      # another lane's votes don't gate this one
            continue
        cls = live_rel(key, d.get("reason"), cache, ctx)
        if cls is None:                 # verifier no longer says 'yes' — not a defer-class member
            continue
        for t in rel_tokens(key, cls):  # the class AND its ·sole-anchor refinement, if any
            buckets[t][st] += 1
    out = set()
    for cls, c in buckets.items():
        if rel_action(cls) != "defer":
            continue
        n = c["approved"] + c["written"] + c["rejected"] + c["deferred"]
        if n >= GATE_MIN_SAMPLES and (c["rejected"] + c["deferred"]) / n >= AUTO_DEFER_NOWRITE:
            out.add(cls)
    return out


FILTERS = ("lang", "act", "hint", "blind", "set")   # the query keys every link, form and redirect carries


def filter_bar(state, dims):
    """One line per filter dimension, e.g. 'language: all · de 12 · sv 3', plus a hints on/off
    toggle. `state` is the active {key: value}; each link changes one key and keeps the rest.
    Counts come from the full pending set so they stay stable as you switch filters."""
    def href(**change):
        new = {k: v for k, v in {**state, **change}.items() if v}
        return "/?" + urllib.parse.urlencode(new) if new else "/"

    lines = []
    for key, label, counts in dims:
        if not counts:
            continue
        def link(val, text, n):
            sel = " sel" if (val or "") == (state.get(key) or "") else ""
            return (f"<a class='lsel{sel}' href='{html.escape(href(**{key: val}))}'>{text}"
                    f" <span class=d>{n}</span></a>")
        links = [link(None, "all", sum(counts.values()))] + [
            link(v, html.escape(v), counts[v]) for v in sorted(counts)]
        lines.append(f"{label}: " + " · ".join(links))
    hidden = bool(state.get("blind"))
    lines.append(f"hints: <a class='lsel' href='{html.escape(href(blind=None if hidden else '1'))}'>"
                 + ("hidden (blind) — show" if hidden else "shown — hide (blind)") + "</a>")
    return "<p class=bar>" + "<br>".join(lines) + "</p>"


def sense_inventory(lid, lang, _cache={}):
    """[(sid, gloss, taken_qid)] — EVERY recorded sense of the target lexeme, with the anchor
    already sitting on it if there is one.

    This is the poly lane's whole safety story. The verifier picks one sense at ~93-95%, and the
    ~9.5% it gets wrong it gets wrong by naming a DIFFERENT sense — which is invisible to a
    reviewer shown only the chosen one, and obvious to a reviewer shown the alternatives.
    So the inventory is not decoration: approving a poly row without it is approving a claim
    nobody checked. Taken senses are marked because they are not writable targets (the free
    -sense gate already excluded fully-linked lexemes, but a lexeme can be part-linked).
    Cached per lexeme for the lifetime of the process — candidates/wikidata are immutable
    while a server runs, the same premise polysemy_index and anchor_support_index rely on."""
    if lid in _cache:
        return _cache[lid]
    cn = sqlite3.connect("file:" + config.WIKIDATA + "?mode=ro", uri=True)
    by = {}
    for sid, lg, gloss in cn.execute(
            "SELECT sid, lang, gloss FROM sense_gloss WHERE lid = ?", [lid]):
        by.setdefault(sid, {})[lg] = gloss
    taken = dict(cn.execute("SELECT sid, qid FROM sense_item WHERE lid = ?", [lid]))
    all_sids = [r[0] for r in cn.execute("SELECT sid FROM sense WHERE lid = ?", [lid])]
    cn.close()
    out = []
    for sid in sorted(set(all_sids) | set(by)):
        g = by.get(sid, {})
        text = next((g[c] for c in (lang, "en", "de", "sv") if g.get(c)),
                    next(iter(g.values()), ""))
        out.append((sid, text, taken.get(sid)))
    _cache[lid] = out
    return out


def render(limit, lang_filter=None, lane=None, act_filter=None, blind=False, hint_filter=None,
           set_filter=None):
    """act_filter: show only rows whose triage action is this ('create', 'defer', 'keep'…) —
    `/?act=create` is how the missing-sense rows are reviewed as a set. hint_filter: only
    create rows whose create_evidence is this ('named', 'owned', '-'). blind (`&blind=1`)
    hides the create_evidence hints, so rulings made under it can still validate them. set_filter
    (`&set=named150`): only the pairs listed in notes/runs/<set>.txt — a fixed review sample."""
    lane = _lane(lane)
    senseless = lane == "senseless"
    decisions, undecided, ctx, ad, _ = pending_view(lane)
    auto_def = [r for r in undecided if is_auto_deferred((r[0], r[1]), r[6], ctx, ad)]
    pending = [r for r in undecided if not is_auto_deferred((r[0], r[1]), r[6], ctx, ad)]
    # Selector counts are over the unfiltered (human-)pending set, so they don't change as you filter.
    lang_counts = collections.Counter(r[3] for r in pending)
    # Lexeme-centric review (senseless lane, per sense_creation_plan): a senseless lexeme can
    # have several candidate items → several senses we'd create. Count candidate items per lexeme over
    # the FULL pending set so the flag/grouping is stable under language filtering, and sort
    # same-lexeme rows adjacent so the reviewer approves/prunes the inventory as a set, not blind.
    lid_counts = collections.Counter(r[0] for r in pending)
    it = sqlite3.connect(config.ITEMS)
    cand = sqlite3.connect(config.CANDIDATES)
    have_il = has_item_lang(it)
    have_al = has_item_alias(it)
    wd = sqlite3.connect(config.WIKIDATA)
    acts = {(r[0], r[1]): action_of((r[0], r[1]), r[6], ctx) or "keep" for r in pending}
    hints = ({(r[0], r[1]): create_evidence(it, wd, r[0], r[1], r[3], r[2])[0] or "-"
              for r in pending if senseless or acts[(r[0], r[1])] == "create"}
             if have_il and have_al else {})
    act_counts = collections.Counter(acts.values())
    hint_counts = collections.Counter(hints.values())
    in_set = set()
    if set_filter:
        path = os.path.join(config.HERE, "notes", "runs", os.path.basename(set_filter) + ".txt")
        in_set = {tuple(l.split()[:2]) for l in open(path) if l.strip() and not l.startswith("#")}
    rows = [r for r in pending if (not set_filter or (r[0], r[1]) in in_set)
            and (not lang_filter or r[3] == lang_filter)
            and (not act_filter or acts[(r[0], r[1])] == act_filter)
            and (not hint_filter or hints.get((r[0], r[1])) == hint_filter)]
    # Target sense gloss (from ctx — already batch-loaded by pending_view) + polysemy-collapse
    # flag, computed once per row and reused by both the sort below and the render loop.
    poly_idx = polysemy_index(cand)
    sup = anchor_support_index()   # neutral: how many distinct lexemes anchor each pair
    crowd = crowd_index()          # neutral: what the game's players already said
    meta = {}
    for r in rows:
        gl, glang, _ad = ctx.get((r[0], r[1]), ("", None, ""))
        # senseless: no gloss to be undifferentiated, so the flag fired on every collapse row and
        # predicted nothing (44/44 flagged rows approved, 2026-10-18) — off there
        meta[(r[0], r[1])] = (gl, glang, None if senseless else
                              polysemy_hint(poly_idx, r[0], r[1], r[3], r[2], gl))
    # Float scope mismatches AND polysemy-collapse flags to the top: either concern first, then
    # convention broader/narrower, then clean EQUAL — so neither is ever pushed past `limit`.
    def _rank(r):
        act = action_of((r[0], r[1]), r[6], ctx)
        if act in ("defer", "reject") or meta[(r[0], r[1])][2]:
            return 0
        return 1 if act else 2
    # lid, qid tie-breakers keep a lexeme's several candidate senses adjacent (lexeme-centric).
    rows.sort(key=lambda r: (_rank(r), r[3], r[2], r[0], r[1]))
    c = collections.Counter(d["status"] for d in decisions.values())
    c["pending"] = len(pending)
    n_scope = sum(1 for r in rows if action_of((r[0], r[1]), r[6], ctx) in ("defer", "reject"))
    n_poly = sum(1 for r in rows if meta[(r[0], r[1])][2])
    n_multi = sum(1 for r in rows if lid_counts[r[0]] > 1)  # rows on a multi-candidate lexeme
    n_sole = sum(1 for r in rows if sup.get((r[0], r[1])) == 1)
    head = " · ".join(f"{k}: {v}" for k, v in sorted(c.items())) or "empty"
    if senseless:
        head = "MODE: senseless (creating first sense) · " + head
    if auto_def:
        head += (f" · {len(auto_def)} auto-deferred "
                 f"({', '.join(sorted(ad))} ≥85% not-written, hidden)")
    if n_scope:
        head += f" · ⚠ {n_scope} scope-mismatch"
    if n_poly:
        head += f" · ⚠ {n_poly} polysemy-collapse"
    if senseless and n_multi:
        head += f" · ⚠ {n_multi} on multi-candidate lexemes (review as a set)"
    if n_sole:
        head += f" · {n_sole} on a single anchor lexeme"
    n_stranded = sum(1 for r in rows if crowd.get((r[0], r[1]), ((), (), False))[2])
    if n_stranded:
        head += f" · ⚠ {n_stranded} stranded by the game (it will not serve them again)"
    if lang_filter:
        head += f" · filtered to {html.escape(lang_filter)} ({len(rows)})"
    trs = []
    for (lid, qid, lemma, lang, _ns, _v, reason, _m, n_clusters, *_rest) in rows[:limit]:
        gl, glang, n_poly_row = meta[(lid, qid)]
        can_create = False   # poly rows where the assigner abstained offer "+ create sense"
        rel, sug, scls = scope_hint(reason, gl, ctx.get((lid, qid), ("", None, ""))[2])
        scope_html = (f"<span class=rel>{rel}</span><br><span class={scls}>{html.escape(sug)}</span>"
                      if sug else f"<span class=d>{rel.title()}</span>")
        # Anchor concept in the curated languages (item_lang) — the ONLY concept source now
        # (fetch_anchor_langs.py fills it for every anchor; verify enforces coverage).
        al = anchor_langs(it, qid, lang) if have_il else []
        anchor_html = "".join(
            f"<div class=al><span class=alc>{disp}</span> <b>{html.escape(lab or '')}</b>"
            + (f" <span class=d>{html.escape(ds)}</span>" if ds else "") + "</div>"
            for disp, lab, ds in al) or "<i>(no item_lang concept — run fetch_anchor_langs.py)</i>"
        # NEUTRAL evidence: Wikidata officially lists the lemma as one of this item's 'also
        # known as' names. Not a verdict — weigh it against the scope relation (alias + EQUAL/
        # convention → likely keep; alias + a real scope gap → still defer/reject).
        aka = alias_match(it, qid, lang, lemma) if have_al else ""
        if aka:
            anchor_html += (f"<div class=akahit>“{html.escape(aka)}” is a listed "
                            "“also known as” of this item</div>")
        # NEUTRAL evidence, the other way: gate A passed on cluster count, but all of those
        # clusters can hang off ONE anchoring lexeme — one editor's single P5137 statement.
        if sup.get((lid, qid)) == 1:
            anchor_html += (f"<div class=sole>one anchor lexeme backs this — its {n_clusters} "
                            "gate-A support clusters all trace to that single P5137 statement</div>")
        # NEUTRAL: what the game's players already said. Spammer votes are gone before this
        # point, and the "no" side is weak by measurement, so it never floats or sorts a row.
        c_yes, c_no, stranded = crowd.get((lid, qid), ((), (), False))
        if c_yes or c_no:
            said = " · ".join(f"{len(v)} {w} ({', '.join(html.escape(u) for u in v)})"
                              for v, w in ((c_yes, "yes"), (c_no, "no")) if v)
            why = (" — the game will not serve it again, so it ends here" if stranded
                   else " — contested there" if c_yes and c_no else " — still in the game")
            anchor_html += f"<div class=crowd>crowd: {said}{why}</div>"
        if senseless:
            # No existing sense to show — preview the sense we would CREATE (approval covers
            # this gloss text, not just the link), using the exact policy write.py will apply.
            glosses = anchor_gloss(it, qid, lang, lemma)
            if glosses:
                primary_lang = lang if lang in glosses else next(iter(glosses))
                parts = [f"[{g}] “{html.escape(t)}”" for g, t in
                         sorted(glosses.items(), key=lambda kv: kv[0] != primary_lang)]
                gloss_html = ("<span class=create>will CREATE sense</span><br>gloss: "
                              + " · ".join(parts))
            else:
                gloss_html = ("<span class=create>will CREATE sense</span> "
                              "<i>(no CC0 gloss source — would be skipped)</i>")
            if lid_counts[lid] > 1:
                gloss_html += (f"<div class=multi>⚠ this lexeme has {lid_counts[lid]} candidate "
                               "senses in the queue — approve/prune them as one inventory</div>")
        elif lane == "poly":
            inv = sense_inventory(lid, lang)
            # The verifier saw the numbered inventory and chose (since 2026-09-03); the
            # relation and reason beside it are about THAT sense. A NULL sid is a row
            # verified before the inventory was shown — re-verify, don't guess.
            sid, why = _rest[-1], ""
            n_off = sum(1 for _s, g, _t in inv if g)
            if sid is None:
                gloss_html = ("<span class=noassign>no sense chosen — verified before the "
                              "inventory was shown; run <code>verify.py --recache --poly-only "
                              "--gate AD</code> before deciding this row</span>")
            elif sid in ABSTAINED:
                # The abstention is the assigner's strongest signal (60:1), and it used to be
                # a dead end: defer or reject. `create` records the third answer — the lemma
                # HAS this meaning, Wikidata just has no sense for it yet — as the create_add
                # action write.py builds under --include-senseless. Preview the gloss the new
                # sense would get, as the senseless lane does: approval covers that text.
                can_create = True
                glosses = anchor_gloss(it, qid, lang, lemma)
                parts = [f"[{g}] “{html.escape(t)}”" for g, t in
                         sorted(glosses.items(), key=lambda kv: kv[0] != lang)]
                gloss_html = ("<span class=noassign>⚠ no sense fits: the verifier read all "
                              f"{n_off} glosses and said none is this meaning (it agrees with "
                              "the measured assigner on 82% of poly rows, and the assigner "
                              "abstains 60:1 on missing vs present senses) — do not approve; "
                              "“+ create sense” instead if the lemma has this meaning.</span>"
                              + ("<br><span class=create>would CREATE sense</span> gloss: "
                                 + " · ".join(parts) if parts else
                                 "<br><i>(no CC0 gloss source — a create would be skipped)</i>"))
            else:
                gloss_html = (f"<span class=match>will MATCH sense {html.escape(sid)}</span>"
                              + (f" <span class=d>— {html.escape(why)}</span>" if why else ""))
            rowsi = []
            for s_id, s_gl, s_taken in inv:
                mark = "›" if s_id == sid else "&nbsp;"
                cls = " class=chosen" if s_id == sid else ""
                tk = (f" <span class=taken>[already linked to {html.escape(s_taken)}]</span>" if s_taken else "")
                rowsi.append(f"<div{cls}>{mark} <b>{html.escape(s_id)}</b> "
                             f"“{html.escape(s_gl or '')}”{tk}</div>")
            gloss_html += ("<div class=inv>" + "".join(rowsi)
                           + "<div class=d>the whole inventory is shown because a wrong "
                             "assignment is a wrong SENSE, not a wrong lexeme — check the "
                             "alternatives before approving</div></div>")
        else:
            gtag = f" [{glang}]" if glang and glang != lang else ""
            gloss_html = f"“{html.escape(gl)}”{gtag}" if gl else "<i>(no sense gloss)</i>"
            if _rest[-1] in ABSTAINED:
                # mono, and the verifier says the one recorded sense is NOT this meaning while
                # the noun has it: same create_add offer as the poly lane, with the gloss preview
                can_create = True
                glosses = anchor_gloss(it, qid, lang, lemma)
                parts = [f"[{g}] “{html.escape(t)}”" for g, t in
                         sorted(glosses.items(), key=lambda kv: kv[0] != lang)]
                gloss_html = (f"<span class=d>existing sense, <b>not</b> this meaning:</span> "
                              f"{gloss_html}<br><span class=noassign>⚠ the verifier says the "
                              "noun has this meaning but no recorded sense does — “+ create "
                              "sense” if so, defer if the gloss below is wrong</span>"
                              + ("<br><span class=create>would CREATE sense</span> gloss: "
                                 + " · ".join(parts) if parts else
                                 "<br><i>(no CC0 gloss source — a create would be skipped)</i>"))
        if (can_create or senseless) and have_il and have_al and not blind:
            kind, owners = create_evidence(it, wd, lid, qid, lang, lemma)
            if kind == "named":
                gloss_html += (f"<div class=named>✓ Wikidata’s own {html.escape(lang)} name for "
                               "this item is this word — such creates were right 22/26</div>")
            elif kind == "owned":
                links = ", ".join(
                    f"<a href='https://www.wikidata.org/wiki/Lexeme:{o}' target=_blank>"
                    f"{html.escape(m or o)}</a>" for o, m in owners)
                gloss_html += (f"<div class=owned>⚠ another {html.escape(lang)} lexeme owns this "
                               f"item ({links}) — such creates were right 7/30, usually a "
                               "polysemous foreign word bridging two meanings</div>")
        if n_poly_row:
            gloss_html += (f"<div class=poly>⚠ source word is polysemous ({n_poly_row} WikDict "
                           "senses collapse here) — this sense gloss is undifferentiated, so "
                           "verify the anchor isn’t over-specifying it</div>")
        form = (f"<form method=post action=/act style='display:inline'>"
                f"<input type=hidden name=lid value='{html.escape(lid)}'>"
                f"<input type=hidden name=qid value='{html.escape(qid)}'>"
                + "".join(f"<input type=hidden name=f_{k} value='{html.escape(v)}'>" for k, v in
                          (("lang", lang_filter), ("act", act_filter), ("hint", hint_filter),
                           ("blind", "1" if blind else None), ("set", set_filter)) if v)
                + ("" if can_create else
                   f"<button name=action value=approved class=ok>✓ approve</button> ") +
                f"<button name=action value=rejected class=no>✗ reject</button> "
                f"<button name=action value=deferred class=hold>? defer</button>"
                + (" <button name=action value=create class=ok>+ create sense</button>"
                   if can_create else "") + "</form>")
        trs.append(
            f"<tr><td class=lg>{html.escape(lang)}</td>"
            f"<td class=lm><a href='https://www.wikidata.org/wiki/Lexeme:{lid}' target=_blank>"
            f"{html.escape(lemma)}</a><br><span class=d>{gloss_html}</span></td>"
            f"<td><a href='https://www.wikidata.org/wiki/{qid}' target=_blank>{qid}</a>"
            f"{anchor_html}</td>"
            f"<td>{scope_html}</td>"
            f"<td class=d>{html.escape(reason or '')}</td>"
            f"<td>{form}</td></tr>")
    return f"""<!doctype html><meta charset=utf-8><title>P5137 review</title>
<style>body{{font:14px/1.4 system-ui,sans-serif;margin:1.5rem}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #e3e3e3;padding:7px 9px;vertical-align:top;text-align:left}}
.lg{{color:#888;text-transform:uppercase;font-size:.8rem}} .lm{{font-weight:600}} .d{{color:#666;font-size:.86rem}}
button{{font-size:.85rem;padding:3px 8px;cursor:pointer;border:1px solid #ccc;border-radius:4px;background:#fafafa}}
.ok{{color:#197a19}} .no{{color:#b00}} .hold{{color:#a60}} a{{color:#36c;text-decoration:none}}
.rel{{font-weight:600;font-size:.78rem;letter-spacing:.02em}}
.al{{margin:1px 0}} .alc{{display:inline-block;min-width:1.5rem;color:#999;font-size:.72rem;text-transform:uppercase}}
.akahit,.sole,.crowd{{margin-top:3px;color:#36c;font-size:.78rem}}
.poly{{margin-top:3px;color:#a60;font-size:.78rem}}
.named{{margin-top:3px;color:#197a19;font-size:.78rem}} .owned{{margin-top:3px;color:#b00;font-size:.78rem}}
.create{{color:#197a19;font-weight:600;font-size:.82rem}} .multi{{margin-top:3px;color:#a60;font-size:.78rem}}
.match{{color:#197a19;font-weight:600;font-size:.82rem}} .noassign{{color:#b00;font-weight:600;font-size:.8rem}}
.inv{{margin-top:5px;border-left:2px solid #ddd;padding-left:7px;font-size:.8rem;color:#555}}
.inv .chosen{{color:#197a19;font-weight:600}} .taken{{color:#999}}
.bar{{margin:.4rem 0 1rem}} .lsel{{padding:1px 4px;border-radius:3px}} .lsel.sel{{background:#eef;font-weight:700;color:#000}}</style>
<h2>P5137 review queue{LANE_TITLE[lane]}</h2><p class=d>{head} — showing up to {limit} pending</p>
{filter_bar(dict(lang=lang_filter, act=act_filter, hint=hint_filter, blind="1" if blind else None,
                 set=set_filter),
            [("lang", "language", lang_counts), ("act", "action", {} if senseless else act_counts),
             ("hint", "create hint", hint_counts)])}
<table><tr><th>lang</th><th>lemma → Lexeme<br>{LANE_COL[lane]}</th><th>anchor concept (CC0)</th><th>scope</th><th>verifier reason</th><th></th></tr>
{chr(10).join(trs) or '<tr><td colspan=6>nothing pending 🎉</td></tr>'}</table>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", ctype)
        self.end_headers()
        if body:
            self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/act"):
            return self._send(405, "use POST")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        lang = qs.get("lang", [None])[0] or None
        act = qs.get("act", [None])[0] or None
        self._send(200, render(50, lang, act_filter=act, blind=bool(qs.get("blind")),
                               hint_filter=qs.get("hint", [None])[0] or None,
                               set_filter=qs.get("set", [None])[0] or None))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = urllib.parse.parse_qs(self.rfile.read(length).decode())
        lid = data.get("lid", [""])[0]
        qid = data.get("qid", [""])[0]
        action = data.get("action", [""])[0]
        if action in ("approved", "rejected", "deferred", "create"):
            row = next(iter(pool(key=(lid, qid))), None)
            decided = load_decisions()
            # A poly decision is about a SENSE, so it records which one — target_sid, frozen
            # into the log at approval time rather than re-derived later from the assignment
            # cache. Re-deriving would mean a prompt change silently rewrites what the human
            # approved, which on an append-only log is exactly the kind of damage that cannot
            # be repaired afterwards.
            target_sid = ""
            if row and _lane() in ("poly", "mono"):
                sid = row[-1]      # the verifier's chosen sense, frozen into the log below
                if sid not in (None, *ABSTAINED):
                    target_sid = sid
                    if action == "create":
                        row = None   # create is only offered where no sense fits
                elif action == "approved" and (sid is not None or _lane() == "poly"):
                    row = None       # an approval must name a sense; reject/defer need not
            elif action == "create":
                row = None           # senseless: nothing recorded for a sense to be missing from
            if row and (lid, qid) not in decided:  # ignore replays / stale double-clicks
                # row_lang: the row's own language, not the reviewer's f_lang filter
                (l, q, lemma, row_lang, ns, v, reason, model, *_rest) = row
                append_decision({
                    "ts": now(), "target_lid": l, "anchor_qid": q,
                    "target_lemma": lemma, "target_lang": row_lang, "target_n_senses": ns,
                    "target_sid": target_sid,
                    "verdict": v, "reason": reason, "model": model,
                    "status": action, "reviewer": "local", "revid": "",
                })
        q = urllib.parse.urlencode({k: data[f"f_{k}"][0] for k in FILTERS if data.get(f"f_{k}")})
        self.send_response(303)
        self.send_header("Location", "/?" + q if q else "/")
        self.end_headers()

    def log_message(self, *a):  # quiet
        pass


def serve(args):
    global LANE
    LANE = lane_from_args(args)
    n = len(pool(LANE))
    if not n:
        gate = {"senseless": "AD --senseless-only", "poly": "AD --poly-only",
                "mono": "ABD"}[LANE]
        print(f"no verifier-'yes' High-tier candidates — run verify.py --gate {gate} first")
        return
    c = counts()
    mode = LANE_BANNER[LANE]
    print(f"review UI on http://localhost:{PORT}  ({c.get('pending', 0):,} pending of "
          f"{n:,} candidates){mode} — Ctrl-C to stop")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "status"):
        p = sub.add_parser(name)
        lane = p.add_mutually_exclusive_group()
        lane.add_argument("--senseless", action="store_true",
                          help="review the senseless first-sense-creation lane (n_senses=0) "
                               "instead of the monosemous write stream")
        lane.add_argument("--poly", action="store_true",
                          help="review the polysemous MATCH lane (n_senses>=2): the candidate "
                               "is shown against the lexeme's whole sense inventory, with "
                               "the verifier's per-sense choice marked")
    args = ap.parse_args()
    {"serve": serve, "status": status}[args.cmd](args)


if __name__ == "__main__":
    main()
