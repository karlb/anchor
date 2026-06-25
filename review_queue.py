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
    """(status -> n) from the log, plus a computed 'pending'."""
    decisions = load_decisions()
    c = collections.Counter(d["status"] for d in decisions.values())
    c["pending"] = sum(1 for r in pool() if (r[0], r[1]) not in decisions)
    return c


def status(args):
    print(f"decision log: {DECISIONS}")
    c = counts()
    for s in ("pending", "approved", "deferred", "rejected", "written"):
        print(f"  {s:<10} {c.get(s, 0):,}")
    decided = load_decisions()
    n_scope = sum(1 for r in pool() if (r[0], r[1]) not in decided
                  and scope_hint(r[6])[1].startswith("defer"))
    if n_scope:
        print(f"  ⚠ of pending, {n_scope:,} are broader/narrower scope mismatches "
              "(review individually — likely defer/reject)")
    # Every decided row was a verifier-'yes' candidate (the pool is verdict='yes'),
    # so the human verdict on it directly measures verifier-'yes' precision on the
    # real (fresh) write stream — the number held-out precision can only approximate.
    confirmed = c.get("approved", 0) + c.get("written", 0)
    decided = confirmed + c.get("rejected", 0)
    if decided:
        print(f"  verifier-'yes' precision (human-confirmed): {confirmed}/{decided} = "
              f"{100.0 * confirmed / decided:.1f}%")
    # Drift: the log snapshots the verifier verdict at decision time, but the cache can change
    # later (e.g. a prompt-version rebuild). Surface decided pairs whose CURRENT cache verdict
    # differs from what was reviewed — informational only (writes are log-driven, not cache-driven).
    if os.path.exists(config.VERIFY):
        decisions = load_decisions()
        cn = sqlite3.connect(config.VERIFY)
        cache = {(a, b): v for a, b, v in
                 cn.execute("SELECT target_lid, anchor_qid, verdict FROM verdict")}
        cn.close()
        drift = [d["target_lemma"] for (lid, qid), d in decisions.items()
                 if (lid, qid) in cache and d.get("verdict") and cache[(lid, qid)] != d["verdict"]]
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


# ---- scope triage -----------------------------------------------------------
# The verifier's TOLERANT criterion maps EQUAL/BROADER/NARROWER -> 'yes', so a
# scope-mismatched pair still reaches review on a surface-plausible read. But P5137
# asserts the sense denotes that EXACT concept, so a broader/narrower link is usually
# a hold (defer) or reject — UNLESS the scope difference is one the link standard
# blesses as EQUAL: a demonym individual vs the people-collective, or a gender-specific
# form (the relation lives on the lexeme, not the sense). We surface the verifier's
# relation token and an ADVISORY suggestion so these aren't bulk-approved unseen; the
# human still decides via the buttons. See notes/p5137_anchor_propagation.md "Reviewer
# guidance" (dominant-referent vs lateral/wrong-rank near-synonym).
_CONVENTION = ("collective", "demonym", "citizen", "resident", "ethnic",
               "nationals", " people", "gender", "masculine", "feminine")


def scope_hint(reason):
    """(relation, suggestion, css_class) for a verdict reason.

    relation is the verifier's leading REL token (EQUAL/BROADER/NARROWER/…); an empty
    suggestion means no scope flag (EQUAL or untagged)."""
    rel = (reason or "").split(":", 1)[0].strip().upper()
    if rel not in ("BROADER", "NARROWER"):
        return rel, "", ""
    if any(k in (reason or "").lower() for k in _CONVENTION):
        return rel, "keep? (demonym/gender convention)", "ok"
    return rel, "defer? (scope mismatch)", "hold"


def render(limit):
    decisions = load_decisions()
    rows = [r for r in pool() if (r[0], r[1]) not in decisions]
    # Float scope mismatches to the top: defer-suggested first, then convention
    # broader/narrower, then clean EQUAL — so a scope case is never pushed past `limit`.
    rows.sort(key=lambda r: ((lambda s: 0 if s.startswith("defer") else 1 if s else 2)
                             (scope_hint(r[6])[1]), r[3], r[2]))
    wd = sqlite3.connect(config.WIKIDATA)
    c = collections.Counter(d["status"] for d in decisions.values())
    c["pending"] = len(rows)
    n_scope = sum(1 for r in rows if scope_hint(r[6])[1].startswith("defer"))
    head = " · ".join(f"{k}: {v}" for k, v in sorted(c.items())) or "empty"
    if n_scope:
        head += f" · ⚠ {n_scope} scope-mismatch"
    trs = []
    for (lid, qid, lemma, lang, _ns, _v, reason, _m,
         lde, len_, lsv, dde, den, dsv) in rows[:limit]:
        rel, sug, scls = scope_hint(reason)
        scope_html = (f"<span class=rel>{rel}</span><br><span class={scls}>{html.escape(sug)}</span>"
                      if sug else f"<span class=d>{rel.title()}</span>")
        lab, llang = pick(lang, {"de": lde, "en": len_, "sv": lsv})
        dsc, dlang = pick(lang, {"de": dde, "en": den, "sv": dsv})
        ltag = f" [{llang}]" if llang and llang != lang else ""
        dtag = f" [{dlang}]" if dlang and dlang != lang else ""
        gl, glang = lexeme_gloss(wd, lid, lang)
        gtag = f" [{glang}]" if glang and glang != lang else ""
        gloss_html = f"“{html.escape(gl)}”{gtag}" if gl else "<i>(no sense gloss)</i>"
        form = (f"<form method=post action=/act style='display:inline'>"
                f"<input type=hidden name=lid value='{html.escape(lid)}'>"
                f"<input type=hidden name=qid value='{html.escape(qid)}'>"
                f"<button name=action value=approved class=ok>✓ approve</button> "
                f"<button name=action value=rejected class=no>✗ reject</button> "
                f"<button name=action value=deferred class=hold>? defer</button></form>")
        trs.append(
            f"<tr><td class=lg>{html.escape(lang)}</td>"
            f"<td class=lm><a href='https://www.wikidata.org/wiki/Lexeme:{lid}' target=_blank>"
            f"{html.escape(lemma)}</a><br><span class=d>{gloss_html}</span></td>"
            f"<td><a href='https://www.wikidata.org/wiki/{qid}' target=_blank>{qid}</a><br>"
            f"<b>{html.escape(lab)}</b>{ltag}<br><span class=d>{html.escape(dsc)}{dtag}</span></td>"
            f"<td>{scope_html}</td>"
            f"<td class=d>{html.escape(reason or '')}</td>"
            f"<td>{form}</td></tr>")
    return f"""<!doctype html><meta charset=utf-8><title>P5137 review</title>
<style>body{{font:14px/1.4 system-ui,sans-serif;margin:1.5rem}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #e3e3e3;padding:7px 9px;vertical-align:top;text-align:left}}
.lg{{color:#888;text-transform:uppercase;font-size:.8rem}} .lm{{font-weight:600}} .d{{color:#666;font-size:.86rem}}
button{{font-size:.85rem;padding:3px 8px;cursor:pointer;border:1px solid #ccc;border-radius:4px;background:#fafafa}}
.ok{{color:#197a19}} .no{{color:#b00}} .hold{{color:#a60}} a{{color:#36c;text-decoration:none}}
.rel{{font-weight:600;font-size:.78rem;letter-spacing:.02em}}</style>
<h2>P5137 review queue</h2><p class=d>{head} — showing up to {limit} pending</p>
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
        self._send(200, render(50))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = urllib.parse.parse_qs(self.rfile.read(length).decode())
        lid = data.get("lid", [""])[0]
        qid = data.get("qid", [""])[0]
        action = data.get("action", [""])[0]
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
        self.send_header("Location", "/")
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
