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

DECISIONS = config.DECISIONS
PORT = 8731

# Review mode: the monosemous write stream (n_senses=1) or the phase-2 senseless
# sense-creation lane (n_senses=0). One process serves one mode; the HTTP Handler
# (which can't take params) reads this, set by serve()/status() from --senseless.
SENSELESS = False

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
            "target_n_senses", "verdict", "reason", "model", "status", "reviewer", "revid"]

# The statuses meaning "the human said the verifier-'yes' was correct" (write.py folds
# 'written' into approval) — shared by the audit/validation consumers of the log.
CONFIRMED = ("approved", "written")


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
    """Append one decision event (a dict over LOG_COLS); write the header if new."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS)
        if new:
            w.writeheader()
        w.writerow({c: rec.get(c, "") for c in LOG_COLS})


def pool(senseless=None, key=None):
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
    if senseless is None:
        senseless = SENSELESS
    cn = sqlite3.connect(config.CANDIDATES)
    cn.execute("ATTACH DATABASE ? AS it", [config.ITEMS])
    cn.execute("ATTACH DATABASE ? AS vf", [config.VERIFY])
    rows = cn.execute(
        """
        SELECT c.target_lid, c.anchor_qid, c.target_lemma, c.target_lang,
               c.target_n_senses, v.verdict, v.reason, v.model,
               i.desc_de, i.desc_en, i.desc_sv,
               c.support_clusters, v.prompt_ver
        FROM candidate c
        JOIN vf.verdict v ON v.target_lid = c.target_lid AND v.anchor_qid = c.anchor_qid
        JOIN it.item i ON i.qid = c.anchor_qid AND NOT i.missing
        WHERE """ + gates.high_tier_where("c", "it", n_senses=0 if senseless else 1)
        + """ AND v.verdict = 'yes'"""
        + (" AND c.target_lid = ? AND c.anchor_qid = ?" if key else "")
        + """
        ORDER BY c.target_lang, c.target_lemma
        """, list(key) if key else []).fetchall()
    cn.close()
    return rows


def pending_view(senseless=None):
    """The shared review pipeline, computed ONCE per invocation: (decisions, undecided,
    ctx, auto-defer classes, verify cache). counts/status/render all consume this — it
    is a 3-DB join plus a full log replay and a verify-cache scan, so each caller runs
    it once and the call sites can't drift apart."""
    decisions = load_decisions()
    vcache = load_verify_cache()
    undecided = [r for r in pool(senseless) if (r[0], r[1]) not in decisions]
    ctx = demonym_context([(k[0], k[1], d.get("target_lang")) for k, d in decisions.items()]
                          + [(r[0], r[1], r[3]) for r in undecided])
    ad = auto_defer_classes(decisions, ctx, vcache)
    return decisions, undecided, ctx, ad, vcache


def counts(view=None):
    """(status -> n) from the log, plus computed 'pending' (human-pending) and 'auto_deferred'."""
    decisions, undecided, ctx, ad, _ = view or pending_view()
    c = collections.Counter(d["status"] for d in decisions.values())
    c["auto_deferred"] = sum(1 for r in undecided if rel_of((r[0], r[1]), r[6], ctx) in ad)
    c["pending"] = len(undecided) - c["auto_deferred"]
    return c


def status(args):
    global SENSELESS
    SENSELESS = getattr(args, "senseless", False)
    print(f"decision log: {DECISIONS}"
          + ("   [MODE: senseless — first-sense creation]" if SENSELESS else ""))
    view = pending_view(SENSELESS)
    decided, undecided, ctx, ad, vcache = view
    c = counts(view)
    for s in ("pending", "auto_deferred", "approved", "deferred", "rejected", "written"):
        print(f"  {s:<13} {c.get(s, 0):,}")
    if c.get("auto_deferred"):
        print(f"  ↳ auto-deferred classes (≥85% not-written, hidden from review): "
              f"{', '.join(sorted(ad))} — a better prompt/approach re-surfaces them")
    n_scope = sum(1 for r in undecided
                  if rel_of((r[0], r[1]), r[6], ctx) not in ad
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


def anchor_langs(it, qid):
    """[(display_lang, label, desc), ...] for config.CONCEPT_LANGS_DISPLAY present for qid.

    Thin wrapper over the shared resolver (Norwegian collapses nb/no/nn to the first
    available; a language is included if it has a label OR a description)."""
    entry = {lg: (lab, ds) for lg, lab, ds in
             it.execute("SELECT lang, label, desc FROM item_lang WHERE qid=?", [qid])}
    return config.resolve_concept_langs(entry, config.CONCEPT_LANGS_DISPLAY)


def anchor_cc0(it, qid, lang):
    """(label, desc) for the anchor Q in `lang` — from the all-language item_lang cache
    when populated, else the de/en/sv `item` columns. Either field may be None/empty."""
    if has_item_lang(it):
        r = it.execute("SELECT label, desc FROM item_lang WHERE qid=? AND lang=?",
                       [qid, lang]).fetchone()
        if r:
            return r[0], r[1]
    if lang in ("de", "en", "sv"):
        r = it.execute(f"SELECT label_{lang}, desc_{lang} FROM item "
                       "WHERE qid=? AND NOT missing", [qid]).fetchone()
        if r:
            return r[0], r[1]
    return None, None


def anchor_gloss(it, qid, lang):
    """Glosses {gloss_lang: text} for a would-be new (senseless-target) sense, per the
    decided policy (sense_creation_plan.md): the anchor's CC0 DESCRIPTION, falling back to
    its LABEL only when no description exists in that language; empty dict = skip the
    candidate (no usable CC0 text). Keyed by the language the text is ACTUALLY in — the
    fix for the old bug where an en-fallback string was written under a `de`/`fr` key — and,
    when the target language isn't en, an extra en gloss so a non-target-language patroller
    can still read the created sense. CC0 only; a DBnary gloss is never read. This is the
    single source of the gloss text: the review UI previews exactly what write.py will
    write, both calling here."""
    out = {}
    lab, desc = anchor_cc0(it, qid, lang)
    text = desc or lab                      # description-primary, label fallback (same language)
    if text:
        out[lang] = text
    if lang != "en":
        elab, edesc = anchor_cc0(it, qid, "en")
        etext = edesc or elab
        if etext and etext not in out.values():
            out["en"] = etext
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


def anchor_desc(dde, den, dsv):
    """The demonym nudge's anchor_desc input: the anchor's de/en/sv CC0 descriptions
    joined. ONE composer, so the queue and every measurement harness feed
    is_ethnic_demonym identical text (a change here reaches all of them at once)."""
    return " | ".join(d for d in (dde, den, dsv) if d)


def is_ethnic_demonym(gloss, anchor_desc):
    """A demonym lexeme linked to an ethnic-group anchor (not a nationality one)."""
    if not gloss or not anchor_desc:
        return False
    if not any(p in gloss.lower() for p in _DEMONYM_GLOSS):
        return False
    return any(m in anchor_desc.lower() for m in _ETHNIC_ANCHOR)


# kind -> (action, reviewer-facing suggestion, css class). The ACTION token is what policy
# consumers branch on (rel_class, ab_concept.predicted_action, the queue sort/counters);
# the suggestion is display copy only — so rewording a hint can never silently change
# triage behaviour again (it used to: the prose was parsed back by substring).
HINTS = {
    "lateral":        ("defer", "defer? (lateral — adjacent, not is-a)", "hold"),
    "incoherent":     ("defer", "defer? (incoherent anchor — no basis to judge)", "hold"),
    "demonym-ethnic": ("defer", "defer? (demonym vs ethnic group)", "hold"),
    "convention":     ("keep", "keep? (demonym/gender convention)", "ok"),
    "scope":          ("defer", "defer? (scope mismatch)", "hold"),
    "":               ("", "", ""),
}


def scope_kind(reason, gloss=None, anchor_desc=None):
    """(relation, kind) for a verdict reason — the ONE scope classifier.

    relation is the verifier's leading REL token (EQUAL/BROADER/NARROWER/LATERAL/INCOHERENT/…);
    kind keys HINTS ('' = no scope flag: EQUAL or untagged). gloss + anchor_desc (when given)
    enable the demonym→ethnic-group nudge: a person-from-X linked to an ethnic-group concept is
    downgraded keep→defer (the split convention)."""
    rel = (reason or "").split(":", 1)[0].strip().upper()
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
# In the SENSELESS lane there is no gloss, so that gate is vacuous and the flag fires on every
# collapse row. It does NOT mean "reject a minor sense": creating S1 for a minor meaning breaks no
# rule (lexeme-data-model.md — sense ids auto-increment and are never renumbered, so rank is a
# display artifact). The reviewer's actual job there is gloss specificity, which is what it asks.
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


# ---- data-gated auto-defer (enforcement) ------------------------------------
# A relation CLASS whose decided history is >=85% "not written" (reject+defer) on a big enough
# sample is one the human almost never approves — so we stop bothering the human with it: auto-defer
# HIDES it from the pending queue WITHOUT logging a decision (user call 2026-06-27). This is
# reversible BY CONSTRUCTION: the filter keys on the live verdict relation, so when a better prompt
# re-verifies a pair into a different class it reappears in review on its own — no un-defer step. The
# cost is the small fraction of these the human WOULD have approved (LATERAL ~7%); they sit unwritten
# until a prompt/approach improvement re-surfaces them. The instrument calibrate.py reports the SAME
# gate by importing these names, so the readiness table and the live filter can't drift apart.
GATE_MIN_SAMPLES = 30
AUTO_DEFER_NOWRITE = 0.85
KNOWN_RELS = ("EQUAL", "BROADER", "NARROWER", "LATERAL", "INCOHERENT", "DIFFERENT")
# class -> the action scope_hint suggests for it (which gate that class faces). Only a 'defer' class
# can auto-defer; a 'keep'/'reject' class never does, even if its not-written rate is high.
REL_ACTION = {
    "EQUAL": "keep", "BROADER·convention": "keep", "NARROWER·convention": "keep",
    "demonym-ethnic": "defer",   # demonym lexeme vs an ethnic-group anchor: a contested keep → defer
    "BROADER": "defer", "NARROWER": "defer", "LATERAL": "defer", "INCOHERENT": "defer",
    "DIFFERENT": "reject",
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


def action_of(key, reason, ctx=None):
    """The triage action ('', 'keep', 'defer', 'reject') scope_kind suggests for a pair,
    gloss-aware via a demonym_context map — the token the queue sort and counters key on."""
    gloss, _glang, adesc = (ctx or {}).get(key, (None, None, None))
    return HINTS[scope_kind(reason, gloss, adesc)[1]][0]


def load_verify_cache(path=config.VERIFY):
    """{(target_lid, anchor_qid): (verdict, reason)} from the live verify cache."""
    if not os.path.exists(path):
        return {}
    cn = sqlite3.connect(path)
    out = {(lid, qid): (verd, reason) for lid, qid, verd, reason in
           cn.execute("SELECT target_lid, anchor_qid, verdict, reason FROM verdict")}
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
    descs = {q: anchor_desc(de, en, sv) for q, de, en, sv in it.execute(
        "SELECT qid, desc_de, desc_en, desc_sv FROM item WHERE qid IN (%s)"
        % ",".join("?" * len(qids)), list(qids))}
    it.close()
    return {(lid, qid): (*pick(lang, glosses.get(lid, {})), descs.get(qid, ""))
            for lid, qid, lang in pairs}


def live_rel(key, log_reason, cache, ctx=None):
    """Relation to bucket a decided pair by, preferring the LIVE verify cache (the current
    prompt's rating of the pair) over the frozen decision-log reason — so pairs reviewed
    before the relation taxonomy was emitted still bucket correctly instead of piling into
    '(legacy, pre-REL)'. The human verdict judges the (lemma, anchor) pair itself, not the
    prompt, so pairing it with the live relation is sound. ctx (from demonym_context) supplies
    the gloss + anchor_desc so the demonym→ethnic defer is applied here exactly as in the render
    queue — same classifier, same inputs. Returns None when the cache now rates the pair NOT 'yes'
    (the verifier would reject it — not write-eligible, so it must not inform the keep/defer/reject
    gates)."""
    hit = cache.get(key)
    if hit is not None:
        verdict, reason = hit
        if verdict != "yes":
            return None
        return rel_of(key, reason, ctx)
    return rel_of(key, log_reason, ctx)


def auto_defer_classes(decisions=None, ctx=None, cache=None):
    """{rel_class}: defer-classes the log proves are >=85% not-written on >=GATE_MIN_SAMPLES pairs.

    These are hidden from the pending queue. Buckets each decided pair by its LIVE verify-cache
    relation (live_rel) — the SAME basis calibrate uses — so the gate that HIDES pairs and the
    readiness table that justifies it can't drift apart: a prompt re-verify that moves a pair's
    relation moves it in both at once. (Bucketing by the frozen log reason silently diverged once
    a re-verify changed the live relations — e.g. NARROWER read 90% not-written frozen but 73%
    live.) A pair the cache now rates not-'yes' drops out (not write-eligible, not a defer-class
    member). ctx (from demonym_context) keeps it gloss-aware so a demonym→ethnic pair buckets as
    its own class rather than hiding inside EQUAL."""
    if decisions is None:
        decisions = load_decisions()
    if cache is None:
        cache = load_verify_cache()
    buckets = collections.defaultdict(collections.Counter)
    for key, d in decisions.items():
        st = (d.get("status") or "").strip()
        if st not in ("approved", "rejected", "deferred", "written"):
            continue
        cls = live_rel(key, d.get("reason"), cache, ctx)
        if cls is None:                 # verifier no longer says 'yes' — not a defer-class member
            continue
        buckets[cls][st] += 1
    out = set()
    for cls, c in buckets.items():
        if REL_ACTION.get(cls) != "defer":
            continue
        n = c["approved"] + c["written"] + c["rejected"] + c["deferred"]
        if n >= GATE_MIN_SAMPLES and (c["rejected"] + c["deferred"]) / n >= AUTO_DEFER_NOWRITE:
            out.add(cls)
    return out


def lang_selector(lang_counts, active):
    """An 'all · de · en · sv' filter bar over the lemma's language (target_lang).

    Counts come from the full pending set so they stay stable as you switch filters."""
    total = sum(lang_counts.values())

    def link(code, label, n):
        sel = " sel" if (code or "") == (active or "") else ""
        href = "/" if code is None else "/?lang=" + urllib.parse.quote(code)
        return f"<a class='lsel{sel}' href='{href}'>{label} <span class=d>{n}</span></a>"

    links = [link(None, "all", total)] + [link(c, c, lang_counts[c]) for c in sorted(lang_counts)]
    return "<p class=bar>lemma language: " + " · ".join(links) + "</p>"


def render(limit, lang_filter=None, senseless=None):
    if senseless is None:
        senseless = SENSELESS
    decisions, undecided, ctx, ad, _ = pending_view(senseless)
    auto_def = [r for r in undecided if rel_of((r[0], r[1]), r[6], ctx) in ad]
    pending = [r for r in undecided if rel_of((r[0], r[1]), r[6], ctx) not in ad]
    # Selector counts are over the unfiltered (human-)pending set, so they don't change as you filter.
    lang_counts = collections.Counter(r[3] for r in pending)
    # Lexeme-centric review (senseless lane, scope_roadmap amendment): a senseless lexeme can
    # have 2–4 candidate items → 2–4 senses we'd create. Count candidate items per lexeme over
    # the FULL pending set so the flag/grouping is stable under language filtering, and sort
    # same-lexeme rows adjacent so the reviewer approves/prunes the inventory as a set, not blind.
    lid_counts = collections.Counter(r[0] for r in pending)
    rows = [r for r in pending if not lang_filter or r[3] == lang_filter]
    it = sqlite3.connect(config.ITEMS)
    cand = sqlite3.connect(config.CANDIDATES)
    have_il = has_item_lang(it)
    have_al = has_item_alias(it)
    # Target sense gloss (from ctx — already batch-loaded by pending_view) + polysemy-collapse
    # flag, computed once per row and reused by both the sort below and the render loop.
    poly_idx = polysemy_index(cand)
    meta = {}
    for r in rows:
        gl, glang, _ad = ctx.get((r[0], r[1]), ("", None, ""))
        meta[(r[0], r[1])] = (gl, glang, polysemy_hint(poly_idx, r[0], r[1], r[3], r[2], gl))
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
    if lang_filter:
        head += f" · filtered to {html.escape(lang_filter)} ({len(rows)})"
    trs = []
    for (lid, qid, lemma, lang, _ns, _v, reason, _m, *_descs) in rows[:limit]:
        gl, glang, n_poly_row = meta[(lid, qid)]
        rel, sug, scls = scope_hint(reason, gl, ctx.get((lid, qid), ("", None, ""))[2])
        scope_html = (f"<span class=rel>{rel}</span><br><span class={scls}>{html.escape(sug)}</span>"
                      if sug else f"<span class=d>{rel.title()}</span>")
        # Anchor concept in the curated languages (item_lang) — the ONLY concept source now
        # (fetch_anchor_langs.py fills it for every anchor; verify enforces coverage).
        al = anchor_langs(it, qid) if have_il else []
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
        if senseless:
            # No existing sense to show — preview the sense we would CREATE (approval covers
            # this gloss text, not just the link), using the exact policy write.py will apply.
            glosses = anchor_gloss(it, qid, lang)
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
        else:
            gtag = f" [{glang}]" if glang and glang != lang else ""
            gloss_html = f"“{html.escape(gl)}”{gtag}" if gl else "<i>(no sense gloss)</i>"
        if n_poly_row:
            gloss_html += (f"<div class=poly>⚠ source word is polysemous ({n_poly_row} WikDict "
                           "senses collapse here) — S1 may be a minor sense, which is fine; "
                           "check the gloss pins THIS meaning so a later editor can’t mistake "
                           "it for the dominant one</div>"
                           if senseless else
                           f"<div class=poly>⚠ source word is polysemous ({n_poly_row} WikDict "
                           "senses collapse here) — this sense gloss is undifferentiated, so "
                           "verify the anchor isn’t over-specifying it</div>")
        form = (f"<form method=post action=/act style='display:inline'>"
                f"<input type=hidden name=lid value='{html.escape(lid)}'>"
                f"<input type=hidden name=qid value='{html.escape(qid)}'>"
                f"<input type=hidden name=lang value='{html.escape(lang_filter or '')}'>"
                f"<button name=action value=approved class=ok>✓ approve</button> "
                f"<button name=action value=rejected class=no>✗ reject</button> "
                f"<button name=action value=deferred class=hold>? defer</button></form>")
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
.akahit{{margin-top:3px;color:#36c;font-size:.78rem}}
.poly{{margin-top:3px;color:#a60;font-size:.78rem}}
.create{{color:#197a19;font-weight:600;font-size:.82rem}} .multi{{margin-top:3px;color:#a60;font-size:.78rem}}
.bar{{margin:.4rem 0 1rem}} .lsel{{padding:1px 4px;border-radius:3px}} .lsel.sel{{background:#eef;font-weight:700;color:#000}}</style>
<h2>P5137 review queue{" — senseless (creating first sense)" if senseless else ""}</h2><p class=d>{head} — showing up to {limit} pending</p>
{lang_selector(lang_counts, lang_filter)}
<table><tr><th>lang</th><th>lemma → Lexeme<br>{"(sense to CREATE)" if senseless else "(its CC0 sense gloss)"}</th><th>anchor concept (CC0)</th><th>scope</th><th>verifier reason</th><th></th></tr>
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
        self._send(200, render(50, lang))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = urllib.parse.parse_qs(self.rfile.read(length).decode())
        lid = data.get("lid", [""])[0]
        qid = data.get("qid", [""])[0]
        action = data.get("action", [""])[0]
        lang = data.get("lang", [""])[0]
        if action in ("approved", "rejected", "deferred"):
            row = next(iter(pool(key=(lid, qid))), None)
            decided = load_decisions()
            if row and (lid, qid) not in decided:  # ignore replays / stale double-clicks
                # row_lang, NOT lang: `lang` is the reviewer's active filter (hidden form
                # field) and must survive to the redirect below
                (l, q, lemma, row_lang, ns, v, reason, model, *_rest) = row
                append_decision({
                    "ts": now(), "target_lid": l, "anchor_qid": q,
                    "target_lemma": lemma, "target_lang": row_lang, "target_n_senses": ns,
                    "verdict": v, "reason": reason, "model": model,
                    "status": action, "reviewer": "local", "revid": "",
                })
        self.send_response(303)
        self.send_header("Location", "/?lang=" + urllib.parse.quote(lang) if lang else "/")
        self.end_headers()

    def log_message(self, *a):  # quiet
        pass


def serve(args):
    global SENSELESS
    SENSELESS = getattr(args, "senseless", False)
    n = len(pool(SENSELESS))
    if not n:
        gate = "AD --senseless-only" if SENSELESS else "ABD"
        print(f"no verifier-'yes' High-tier candidates — run verify.py --gate {gate} first")
        return
    c = counts()
    mode = "  [MODE: senseless — first-sense creation]" if SENSELESS else ""
    print(f"review UI on http://localhost:{PORT}  ({c.get('pending', 0):,} pending of "
          f"{n:,} candidates){mode} — Ctrl-C to stop")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "status"):
        p = sub.add_parser(name)
        p.add_argument("--senseless", action="store_true",
                       help="review the senseless first-sense-creation lane (n_senses=0) "
                            "instead of the monosemous write stream")
    args = ap.parse_args()
    {"serve": serve, "status": status}[args.cmd](args)


if __name__ == "__main__":
    main()
