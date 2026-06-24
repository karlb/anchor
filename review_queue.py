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
    # Every decided row was a verifier-'yes' candidate (the pool is verdict='yes'),
    # so the human verdict on it directly measures verifier-'yes' precision on the
    # real (fresh) write stream — the number held-out precision can only approximate.
    confirmed = c.get("approved", 0) + c.get("written", 0)
    decided = confirmed + c.get("rejected", 0)
    if decided:
        print(f"  verifier-'yes' precision (human-confirmed): {confirmed}/{decided} = "
              f"{100.0 * confirmed / decided:.1f}%")


# ---- minimal approve/reject web UI (stdlib only) ----------------------------

def pick(lang, vals):
    """First non-empty value, target language preferred then en/de/sv."""
    for code in [lang] + [x for x in ("en", "de", "sv") if x != lang]:
        if vals.get(code):
            return vals[code], code
    return "", None


def render(limit):
    decisions = load_decisions()
    rows = [r for r in pool() if (r[0], r[1]) not in decisions]
    c = collections.Counter(d["status"] for d in decisions.values())
    c["pending"] = len(rows)
    head = " · ".join(f"{k}: {v}" for k, v in sorted(c.items())) or "empty"
    trs = []
    for (lid, qid, lemma, lang, _ns, _v, reason, _m,
         lde, len_, lsv, dde, den, dsv) in rows[:limit]:
        lab, llang = pick(lang, {"de": lde, "en": len_, "sv": lsv})
        dsc, dlang = pick(lang, {"de": dde, "en": den, "sv": dsv})
        ltag = f" [{llang}]" if llang and llang != lang else ""
        dtag = f" [{dlang}]" if dlang and dlang != lang else ""
        form = (f"<form method=post action=/act style='display:inline'>"
                f"<input type=hidden name=lid value='{html.escape(lid)}'>"
                f"<input type=hidden name=qid value='{html.escape(qid)}'>"
                f"<button name=action value=approved class=ok>✓ approve</button> "
                f"<button name=action value=rejected class=no>✗ reject</button> "
                f"<button name=action value=deferred class=hold>? defer</button></form>")
        trs.append(
            f"<tr><td class=lg>{html.escape(lang)}</td>"
            f"<td class=lm><a href='https://www.wikidata.org/wiki/Lexeme:{lid}' target=_blank>"
            f"{html.escape(lemma)}</a></td>"
            f"<td><a href='https://www.wikidata.org/wiki/{qid}' target=_blank>{qid}</a><br>"
            f"<b>{html.escape(lab)}</b>{ltag}<br><span class=d>{html.escape(dsc)}{dtag}</span></td>"
            f"<td class=d>{html.escape(reason or '')}</td>"
            f"<td>{form}</td></tr>")
    return f"""<!doctype html><meta charset=utf-8><title>P5137 review</title>
<style>body{{font:14px/1.4 system-ui,sans-serif;margin:1.5rem}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #e3e3e3;padding:7px 9px;vertical-align:top;text-align:left}}
.lg{{color:#888;text-transform:uppercase;font-size:.8rem}} .lm{{font-weight:600}} .d{{color:#666;font-size:.86rem}}
button{{font-size:.85rem;padding:3px 8px;cursor:pointer;border:1px solid #ccc;border-radius:4px;background:#fafafa}}
.ok{{color:#197a19}} .no{{color:#b00}} .hold{{color:#a60}} a{{color:#36c;text-decoration:none}}</style>
<h2>P5137 review queue</h2><p class=d>{head} — showing up to {limit} pending</p>
<table><tr><th>lang</th><th>lemma → Lexeme</th><th>anchor concept (CC0)</th><th>verifier reason</th><th></th></tr>
{chr(10).join(trs) or '<tr><td colspan=5>nothing pending 🎉</td></tr>'}</table>"""


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
