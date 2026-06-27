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

# The anchor languages shown in the review UI live in config.CONCEPT_LANGS_DISPLAY — the
# SAME spec/resolver the verifier uses (the verifier just feeds a wider set). Cross-checking
# a scope call against several languages — a demonym vs its ethnic-group concept, say —
# often reads differently across them. The data is the all-language CC0 cache (item_lang,
# fetch_anchor_langs.py); if it is absent the UI falls back to the de/en/sv `item` columns.

# Append-only log columns. Beyond the (lid, anchor) key and the decision itself,
# we snapshot what the reviewer saw (lemma, verdict, reason, model) so each line
# is a self-contained audit record that does not depend on the regenerable caches.
LOG_COLS = ["ts", "target_lid", "anchor_qid", "target_lemma", "target_lang",
            "target_n_senses", "verdict", "reason", "model", "status", "reviewer", "revid"]


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


def pool():
    """The reviewable set: verifier-'yes' High-tier candidates (clean ∩ A: >=2
    anchors ∩ B: monosemous ∩ D: concrete/P31 ∩ in-scope), each with the anchor's
    own CC0 label/description and the verdict's reason/model. 'pending' = this
    minus whatever the decision log already covers."""
    cn = sqlite3.connect(config.CANDIDATES)
    cn.execute("ATTACH DATABASE ? AS it", [config.ITEMS])
    cn.execute("ATTACH DATABASE ? AS vf", [config.VERIFY])
    rows = cn.execute(
        """
        SELECT c.target_lid, c.anchor_qid, c.target_lemma, c.target_lang,
               c.target_n_senses, v.verdict, v.reason, v.model,
               i.label_de, i.label_en, i.label_sv, i.desc_de, i.desc_en, i.desc_sv
        FROM candidate c
        JOIN vf.verdict v ON v.target_lid = c.target_lid AND v.anchor_qid = c.anchor_qid
        JOIN it.item i ON i.qid = c.anchor_qid AND NOT i.missing
        WHERE """ + gates.high_tier_where("c", "it") + """ AND v.verdict = 'yes'
        ORDER BY c.target_lang, c.target_lemma
        """).fetchall()
    cn.close()
    return rows


def counts():
    """(status -> n) from the log, plus computed 'pending' (human-pending) and 'auto_deferred'."""
    decisions = load_decisions()
    c = collections.Counter(d["status"] for d in decisions.values())
    undecided = [r for r in pool() if (r[0], r[1]) not in decisions]
    ctx = demonym_context([(k[0], k[1], d.get("target_lang")) for k, d in decisions.items()]
                          + [(r[0], r[1], r[3]) for r in undecided])
    ad = auto_defer_classes(decisions, ctx)
    c["auto_deferred"] = sum(1 for r in undecided if rel_of((r[0], r[1]), r[6], ctx) in ad)
    c["pending"] = len(undecided) - c["auto_deferred"]
    return c


def status(args):
    print(f"decision log: {DECISIONS}")
    c = counts()
    for s in ("pending", "auto_deferred", "approved", "deferred", "rejected", "written"):
        print(f"  {s:<13} {c.get(s, 0):,}")
    decided = load_decisions()
    undecided = [r for r in pool() if (r[0], r[1]) not in decided]
    ctx = demonym_context([(k[0], k[1], d.get("target_lang")) for k, d in decided.items()]
                          + [(r[0], r[1], r[3]) for r in undecided])
    ad = auto_defer_classes(decided, ctx)
    if c.get("auto_deferred"):
        print(f"  ↳ auto-deferred classes (≥85% not-written, hidden from review): "
              f"{', '.join(sorted(ad))} — a better prompt/approach re-surfaces them")
    n_scope = sum(1 for r in undecided
                  if rel_of((r[0], r[1]), r[6], ctx) not in ad
                  and scope_hint(r[6], *ctx.get((r[0], r[1]), (None, None)))[1]
                  .startswith(("defer", "reject")))
    if n_scope:
        print(f"  ⚠ of pending, {n_scope:,} are broader/narrower/lateral scope mismatches "
              "(review individually — likely defer/reject)")
    # Precision on the WRITE lane: of decided pairs the LIVE verifier rates 'keep' (EQUAL /
    # convention — the only relations that are written), how many the human confirmed. The
    # blended verifier-'yes' rate is misleading — it counts BROADER/NARROWER/LATERAL
    # rejections, which are SCOPE triage the verifier doesn't judge and which never reach a
    # write. Bucketed by the LIVE cache relation (live_rel) so pre-REL approvals still count.
    vcache = load_verify_cache()
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


def lexeme_gloss(wd, lid, lang):
    """The lexeme's own CC0 Wikidata sense gloss (target-language preferred).

    Lets the reviewer compare the lexeme's actual sense against the anchor concept —
    e.g. a monosemous 'sheet' glossed 'a piece of paper' should NOT link to a nautical
    line. Glosses are CC0 (Wikidata's own), loaded by load_lexemes.py into sense_gloss."""
    vals = {lg: g for lg, g in
            wd.execute("SELECT lang, gloss FROM sense_gloss WHERE lid=?", [lid])}
    return pick(lang, vals)


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
_DEMONYM_GLOSS = ("person from", "person från", "person aus", "person av")
_ETHNIC_ANCHOR = ("ethnic group", "etnisk grupp", "etnisk gruppe", "ethnic", "ethnie",
                  "ethnique", "slavic", "folkgrupp", "folkeslag", "people native",
                  "indigenous to", "kaukasisk folk", "südkaukasisches volk")


def is_ethnic_demonym(gloss, anchor_desc):
    """A demonym lexeme linked to an ethnic-group anchor (not a nationality one)."""
    if not gloss or not anchor_desc:
        return False
    if not any(p in gloss.lower() for p in _DEMONYM_GLOSS):
        return False
    return any(m in anchor_desc.lower() for m in _ETHNIC_ANCHOR)


def scope_hint(reason, gloss=None, anchor_desc=None):
    """(relation, suggestion, css_class) for a verdict reason.

    relation is the verifier's leading REL token (EQUAL/BROADER/NARROWER/LATERAL/INCOHERENT/…);
    an empty suggestion means no scope flag (EQUAL or untagged). gloss + anchor_desc (when given)
    enable the demonym→ethnic-group nudge: a person-from-X linked to an ethnic-group concept is
    downgraded keep→defer (the split convention)."""
    rel = (reason or "").split(":", 1)[0].strip().upper()
    if rel == "LATERAL":
        # Adjacent / not-is-a is a JUDGMENT CALL, not an auto-drop: the verifier can't tell a
        # lateral-that-should-reject (arrangemang) from one that should go to a human
        # (barf=substance-vs-act, bekvämlighet=amenity-vs-state) by the relation alone, so we
        # defer rather than reject — surfacing for review never silently drops a good link.
        return rel, "defer? (lateral — adjacent, not is-a)", "hold"
    if rel == "INCOHERENT":
        return rel, "defer? (incoherent anchor — no basis to judge)", "hold"
    if is_ethnic_demonym(gloss, anchor_desc):
        return rel, "defer? (demonym vs ethnic group)", "hold"
    if rel not in ("BROADER", "NARROWER"):
        return rel, "", ""
    # Demonym/gender convention: a gendered or demonymic lexeme still denotes the neutral
    # concept (sex/scope recorded separately) — but ONLY when the lexeme is NARROWER than (or
    # equal to) the concept. A BROADER lexeme is a real scope mismatch even if the reason
    # mentions a gender word: a generic word ('hog' = adult pig) is NOT the sex-specific
    # concept ('female pig'), so it must not be auto-kept by the convention nudge.
    if rel == "NARROWER" and any(k in (reason or "").lower() for k in _CONVENTION):
        return rel, "keep? (demonym/gender convention)", "ok"
    return rel, "defer? (scope mismatch)", "hold"


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
    rel, suggestion, _ = scope_hint(reason, gloss, anchor_desc)
    if rel not in KNOWN_RELS:
        return "(legacy, pre-REL)"
    if "demonym vs ethnic" in suggestion:          # demonym→ethnic-group nudge → single defer class
        return "demonym-ethnic"
    if rel in ("BROADER", "NARROWER") and "convention" in suggestion:
        return rel + "·convention"
    return rel


def rel_of(key, reason, ctx=None):
    """rel_class for a pair, pulling its (gloss, anchor_desc) from a demonym_context map so the
    one gloss-aware classifier is reused at every call site."""
    gloss, anchor_desc = (ctx or {}).get(key, (None, None))
    return rel_class(reason, gloss, anchor_desc)


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
    """{(lid, qid): (gloss, anchor_desc)} — the demonym→ethnic-group nudge's inputs (the lexeme's
    CC0 sense gloss + the anchor's de/en/sv description), joined ONCE in two batched queries so
    every relation-classification site can feed rel_of/rel_class identical data. Without this the
    replay paths (calibrate, auto_defer) and the render filter classify gloss-blind and drift from
    the gloss-aware HTML queue. `pairs`: iterable of (lid, qid, lang)."""
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
    descs = {q: " | ".join(d for d in (de, en, sv) if d) for q, de, en, sv in it.execute(
        "SELECT qid, desc_de, desc_en, desc_sv FROM item WHERE qid IN (%s)"
        % ",".join("?" * len(qids)), list(qids))}
    it.close()
    return {(lid, qid): (pick(lang, glosses.get(lid, {}))[0], descs.get(qid, ""))
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


def auto_defer_classes(decisions=None, ctx=None):
    """{rel_class}: defer-classes the log proves are >=85% not-written on >=GATE_MIN_SAMPLES pairs.

    These are hidden from the pending queue. Computed live from the decision log so it tracks the
    data; currently only LATERAL clears it. ctx (from demonym_context) keeps the bucketing
    gloss-aware, so a demonym→ethnic class can become a candidate on its own rather than hiding
    inside EQUAL."""
    if decisions is None:
        decisions = load_decisions()
    buckets = collections.defaultdict(collections.Counter)
    for key, d in decisions.items():
        st = (d.get("status") or "").strip()
        if st in ("approved", "rejected", "deferred", "written"):
            buckets[rel_of(key, d.get("reason"), ctx)][st] += 1
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


def render(limit, lang_filter=None):
    decisions = load_decisions()
    undecided = [r for r in pool() if (r[0], r[1]) not in decisions]
    ctx = demonym_context([(k[0], k[1], d.get("target_lang")) for k, d in decisions.items()]
                          + [(r[0], r[1], r[3]) for r in undecided])
    ad = auto_defer_classes(decisions, ctx)
    auto_def = [r for r in undecided if rel_of((r[0], r[1]), r[6], ctx) in ad]
    pending = [r for r in undecided if rel_of((r[0], r[1]), r[6], ctx) not in ad]
    # Selector counts are over the unfiltered (human-)pending set, so they don't change as you filter.
    lang_counts = collections.Counter(r[3] for r in pending)
    rows = [r for r in pending if not lang_filter or r[3] == lang_filter]
    # Float scope mismatches to the top: defer-suggested first, then convention
    # broader/narrower, then clean EQUAL — so a scope case is never pushed past `limit`.
    rows.sort(key=lambda r: ((lambda s: 0 if s.startswith(("defer", "reject")) else 1 if s else 2)
                             (scope_hint(r[6], *ctx.get((r[0], r[1]), (None, None)))[1]), r[3], r[2]))
    wd = sqlite3.connect(config.WIKIDATA)
    it = sqlite3.connect(config.ITEMS)
    have_il = has_item_lang(it)
    c = collections.Counter(d["status"] for d in decisions.values())
    c["pending"] = len(pending)
    n_scope = sum(1 for r in rows if scope_hint(r[6], *ctx.get((r[0], r[1]), (None, None)))[1]
                  .startswith(("defer", "reject")))
    head = " · ".join(f"{k}: {v}" for k, v in sorted(c.items())) or "empty"
    if auto_def:
        head += (f" · {len(auto_def)} auto-deferred "
                 f"({', '.join(sorted(ad))} ≥85% not-written, hidden)")
    if n_scope:
        head += f" · ⚠ {n_scope} scope-mismatch"
    if lang_filter:
        head += f" · filtered to {html.escape(lang_filter)} ({len(rows)})"
    trs = []
    for (lid, qid, lemma, lang, _ns, _v, reason, _m,
         lde, len_, lsv, dde, den, dsv) in rows[:limit]:
        gl, glang = lexeme_gloss(wd, lid, lang)
        anchor_desc = " | ".join(d for d in (dde, den, dsv) if d)
        rel, sug, scls = scope_hint(reason, gl, anchor_desc)
        scope_html = (f"<span class=rel>{rel}</span><br><span class={scls}>{html.escape(sug)}</span>"
                      if sug else f"<span class=d>{rel.title()}</span>")
        # Anchor concept in the curated languages (item_lang), falling back to the
        # de/en/sv `item` columns when the all-language cache isn't populated.
        al = anchor_langs(it, qid) if have_il else []
        if al:
            anchor_html = "".join(
                f"<div class=al><span class=alc>{disp}</span> <b>{html.escape(lab or '')}</b>"
                + (f" <span class=d>{html.escape(ds)}</span>" if ds else "") + "</div>"
                for disp, lab, ds in al)
        else:
            lab, llang = pick(lang, {"de": lde, "en": len_, "sv": lsv})
            dsc, dlang = pick(lang, {"de": dde, "en": den, "sv": dsv})
            ltag = f" [{llang}]" if llang and llang != lang else ""
            dtag = f" [{dlang}]" if dlang and dlang != lang else ""
            anchor_html = (f"<b>{html.escape(lab)}</b>{ltag}"
                           f"<br><span class=d>{html.escape(dsc)}{dtag}</span>")
        gtag = f" [{glang}]" if glang and glang != lang else ""
        gloss_html = f"“{html.escape(gl)}”{gtag}" if gl else "<i>(no sense gloss)</i>"
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
.bar{{margin:.4rem 0 1rem}} .lsel{{padding:1px 4px;border-radius:3px}} .lsel.sel{{background:#eef;font-weight:700;color:#000}}</style>
<h2>P5137 review queue</h2><p class=d>{head} — showing up to {limit} pending</p>
{lang_selector(lang_counts, lang_filter)}
<table><tr><th>lang</th><th>lemma → Lexeme<br>(its CC0 sense gloss)</th><th>anchor concept (CC0)</th><th>scope</th><th>verifier reason</th><th></th></tr>
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
            row = next((r for r in pool() if r[0] == lid and r[1] == qid), None)
            decided = load_decisions()
            if row and (lid, qid) not in decided:  # ignore replays / stale double-clicks
                (l, q, lemma, lang, ns, v, reason, model, *_rest) = row
                append_decision({
                    "ts": now(), "target_lid": l, "anchor_qid": q,
                    "target_lemma": lemma, "target_lang": lang, "target_n_senses": ns,
                    "verdict": v, "reason": reason, "model": model,
                    "status": action, "reviewer": "local", "revid": "",
                })
        self.send_response(303)
        self.send_header("Location", "/?lang=" + urllib.parse.quote(lang) if lang else "/")
        self.end_headers()

    def log_message(self, *a):  # quiet
        pass


def serve(args):
    n = len(pool())
    if not n:
        print("no verifier-'yes' High-tier candidates — run verify.py --gate ABD first")
        return
    c = counts()
    print(f"review UI on http://localhost:{PORT}  ({c.get('pending', 0):,} pending of "
          f"{n:,} candidates) — Ctrl-C to stop")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "status"):
        sub.add_parser(name)
    args = ap.parse_args()
    {"serve": serve, "status": status}[args.cmd](args)


if __name__ == "__main__":
    main()
