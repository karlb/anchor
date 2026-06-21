#!/usr/bin/env python3
"""Human review queue for verified P5137 candidates — the authorization gate.

The verifier proposes; a human approves; only then may write.py act. This holds
that boundary in a tiny SQLite table and a dependency-free approve/reject UI
(stdlib http.server — no Flask).

    uv run review_queue.py seed        # load verifier-"yes" High-tier rows as 'pending'
    uv run review_queue.py serve       # http://localhost:8731  approve/reject buttons
    uv run review_queue.py status      # counts by status
    uv run review_queue.py export      # committed CSV audit trail of all decisions

Status flow: pending -> approved | rejected ; write.py sets approved -> written.
The queue lives in data/review.sqlite3 (gitignored); `export` mirrors decisions
to notes/runs/review_decisions.csv so the human judgements survive a data/ wipe.
"""

import argparse
import csv
import html
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import config

QUEUE = config.DATA + "/review.sqlite3"
EXPORT_CSV = "notes/runs/review_decisions.csv"
PORT = 8731

SCHEMA = """
CREATE TABLE IF NOT EXISTS review(
    target_lid TEXT, anchor_qid TEXT,
    target_lemma TEXT, target_lang TEXT, target_n_senses INT,
    verdict TEXT, reason TEXT,
    status TEXT DEFAULT 'pending',
    reviewer TEXT, ts TEXT, revid TEXT,
    PRIMARY KEY (target_lid, anchor_qid)
) WITHOUT ROWID;
"""


def connect():
    db = sqlite3.connect(QUEUE)
    db.executescript(SCHEMA)
    return db


def pick(lang, vals):
    """First non-empty value, target language preferred then en/de/sv."""
    for code in [lang] + [x for x in ("en", "de", "sv") if x != lang]:
        if vals.get(code):
            return vals[code], code
    return "", None


def seed(args):
    """Insert verifier-'yes' High-tier (clean ∩ A ∩ B ∩ D) candidates as pending."""
    db = connect()
    src = sqlite3.connect(config.CANDIDATES)
    src.execute("ATTACH DATABASE ? AS it", [config.ITEMS])
    src.execute("ATTACH DATABASE ? AS vf", [config.VERIFY])
    rows = src.execute(
        """
        SELECT c.target_lid, c.anchor_qid, c.target_lemma, c.target_lang,
               c.target_n_senses, v.verdict, v.reason
        FROM candidate c
        JOIN vf.verdict v ON v.target_lid = c.target_lid AND v.anchor_qid = c.anchor_qid
        JOIN it.item i ON i.qid = c.anchor_qid AND NOT i.missing
        WHERE c.homograph_n_lid = 1 AND NOT c.cross_pivot_conflict
          AND c.support_clusters >= 2 AND c.target_n_senses = 1
          AND EXISTS(SELECT 1 FROM it.item_type t WHERE t.qid = c.anchor_qid AND t.prop='P31')
          AND v.verdict = 'yes'
        """).fetchall()
    src.close()
    db.executemany(
        "INSERT OR IGNORE INTO review(target_lid,anchor_qid,target_lemma,target_lang,"
        "target_n_senses,verdict,reason) VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()
    print(f"seeded {len(rows):,} verifier-'yes' High-tier candidates "
          f"(new rows added; existing decisions kept)")
    status(args, db)


def status(args, db=None):
    db = db or connect()
    print("status:")
    for s, n in db.execute("SELECT status, count(*) FROM review GROUP BY status ORDER BY 2 DESC"):
        print(f"  {s:<10} {n:,}")


def export(args):
    db = connect()
    cols = ["target_lid", "anchor_qid", "target_lemma", "target_lang",
            "verdict", "reason", "status", "reviewer", "ts", "revid"]
    with open(EXPORT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(db.execute(
            f"SELECT {','.join(cols)} FROM review ORDER BY status, target_lang, target_lemma"))
    n = db.execute("SELECT count(*) FROM review").fetchone()[0]
    print(f"exported {n:,} rows -> {EXPORT_CSV}")


# ---- minimal approve/reject web UI (stdlib only) ----------------------------

def anchor_text(qid):
    it = sqlite3.connect(config.ITEMS)
    r = it.execute("SELECT label_de,label_en,label_sv,desc_de,desc_en,desc_sv "
                   "FROM item WHERE qid=? AND NOT missing", [qid]).fetchone()
    it.close()
    return r


def render(db, limit):
    rows = db.execute(
        "SELECT target_lid,anchor_qid,target_lemma,target_lang,reason FROM review "
        "WHERE status='pending' ORDER BY target_lang, target_lemma LIMIT ?", [limit]).fetchall()
    counts = dict(db.execute("SELECT status, count(*) FROM review GROUP BY status"))
    head = " · ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "empty"
    trs = []
    for lid, qid, lemma, lang, reason in rows:
        a = anchor_text(qid)
        labels = {"de": a[0], "en": a[1], "sv": a[2]} if a else {}
        descs = {"de": a[3], "en": a[4], "sv": a[5]} if a else {}
        lab, llang = pick(lang, labels)
        dsc, dlang = pick(lang, descs)
        ltag = f" [{llang}]" if llang and llang != lang else ""
        dtag = f" [{dlang}]" if dlang and dlang != lang else ""
        form = (f"<form method=post action=/act style='display:inline'>"
                f"<input type=hidden name=lid value='{html.escape(lid)}'>"
                f"<input type=hidden name=qid value='{html.escape(qid)}'>"
                f"<button name=action value=approved class=ok>✓ approve</button> "
                f"<button name=action value=rejected class=no>✗ reject</button></form>")
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
.ok{{color:#197a19}} .no{{color:#b00}} a{{color:#36c;text-decoration:none}}</style>
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
        db = connect()
        self._send(200, render(db, 50))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = urllib.parse.parse_qs(self.rfile.read(length).decode())
        lid = data.get("lid", [""])[0]
        qid = data.get("qid", [""])[0]
        action = data.get("action", [""])[0]
        if action in ("approved", "rejected"):
            db = connect()
            db.execute("UPDATE review SET status=?, reviewer='local', "
                       "ts=datetime('now') WHERE target_lid=? AND anchor_qid=?",
                       [action, lid, qid])
            db.commit()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *a):  # quiet
        pass


def serve(args):
    seeded = connect().execute("SELECT count(*) FROM review").fetchone()[0]
    if not seeded:
        print("queue empty — run `uv run review_queue.py seed` first")
        return
    print(f"review UI on http://localhost:{PORT}  ({seeded:,} in queue) — Ctrl-C to stop")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("seed", "serve", "status", "export"):
        sub.add_parser(name)
    args = ap.parse_args()
    {"seed": seed, "serve": serve, "status": status, "export": export}[args.cmd](args)


if __name__ == "__main__":
    main()
