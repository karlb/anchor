"""Build the multi-candidate adjudication sample — gold for choose-best vs pairwise.

notes/problematic_cases.csv cannot score a choose-best verifier: all 119 of its lexemes
carry exactly ONE candidate, so "pick the best of N" degenerates to yes/no on every row.
This samples the lexemes where a choice actually exists — monosemous targets with >1
candidate anchor once gate A is relaxed to support >= 1 — and lays them out for a human
to mark the right target.

Restricted to lexemes with at least one candidate at support >= 2, i.e. ones the pipeline
already answers today. That keeps the A/B PAIRED — both arms name a target for every
lexeme, so the measurement is which target gets chosen, not the different question of
whether anything gets emitted at all. The all-support-1 lexemes are the recall side of
widening and need their own sheet and their own metric; they are deliberately not here.

Sampling is deterministic (fixed seed, sorted input) so the sheet can be regenerated and
diffed — but only while the underlying databases are unchanged; a grown pool reshuffles
the sample. The build prints the live pool and strata counts. Stratified by target language (floor of MIN_PER_LANG so de/no aren't crowded out
by the fr/sv/en bulk) and, primarily, by whether the lexeme has a support-1 alternative:

  all-strong  every candidate already passes gate A, so the choice exists TODAY and the
              pairwise verifier has usually said yes to several (aube, carabine, Lilie).
              Isolates the choose-best prompt with widening held out.
  mixed       at least one support-1 alternative only widening exposes. Tests the
              combination.

All-strong is a small minority of the pool, so sampling it proportionally would leave too
few rows to read a control off. BUCKET_TARGET oversamples it deliberately; the strata are
reported separately, never pooled into one accuracy number.

Adjudication convention, matching problematic_cases.csv's correct-verdict + acceptable-set
shape: exactly one anchor `best`, any defensible alternative `ok`, the rest `wrong`. A
lexeme whose rows are ALL `wrong` is the none-of-these case — the escape a choose-best
verifier needs too — so it needs no fourth value, and it stays distinguishable from
not-yet-judged (which is the empty string). A lexeme counts as done when no row is empty.

Rows are written UNFILLED; build refuses to clobber a sheet already being worked on.

    uv run build_multi_gold.py                # writes the sheet + a readable HTML
    uv run build_multi_gold.py --serve        # adjudicate by clicking; writes back to the CSV
"""
import argparse
import collections
import csv
import html
import os
import random
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import config
import gates

PORT = 8766          # review_queue.py owns its own port; these two can run side by side

SEED = 20260802
SKIP_LANGS = {"ru"}  # the adjudicator doesn't read Russian; ru stays in the review lanes
N_TOTAL = 50
MIN_PER_LANG = 5
BUCKET_TARGET = {"all-strong": 15, "mixed": 35}   # sums to N_TOTAL
SHEET = os.path.join("notes", "multi_candidate_gold.csv")
PAGE = os.path.join("notes", "review", "multi_candidate_gold.html")
COLS = ["lid", "lang", "lemma", "stratum", "sense_gloss", "anchor_qid", "anchor_label",
        "anchor_desc", "support", "n_anchors", "cur_verdict", "cur_reason", "decided",
        "verdict", "note"]


def connect():
    cn = sqlite3.connect("file:" + config.CANDIDATES + "?mode=ro", uri=True)
    for alias, path in (("it", config.ITEMS), ("wd", config.WIKIDATA), ("vf", config.VERIFY)):
        cn.execute("ATTACH DATABASE ? AS " + alias, ["file:" + path + "?mode=ro"])
    return cn


def pool(cn):
    """{(lid, lang, lemma): [(qid, support), ...]} — monosemous targets with >1 candidate
    anchor at support >= 1, restricted to lexemes whose anchors are ALL in the item cache
    (an unfetched anchor has no label or description, so it cannot be adjudicated)."""
    rows = cn.execute(
        f"""SELECT c.target_lid, c.target_lang, c.target_lemma, c.anchor_qid, c.support_clusters,
                   EXISTS(SELECT 1 FROM it.item i WHERE i.qid = c.anchor_qid AND NOT i.missing)
            FROM candidate c
            WHERE {gates.clean_clause('c')} AND {gates.gate_b('c', 1)}
              AND {gates.free_sense_clause('c', 'wd')}
              AND c.support_clusters >= 1""").fetchall()
    per = collections.defaultdict(list)
    cached = collections.defaultdict(list)
    for lid, lang, lemma, qid, sup, is_cached in rows:
        if lang in SKIP_LANGS:
            continue
        per[(lid, lang, lemma)].append((qid, sup))
        cached[(lid, lang, lemma)].append(is_cached)
    return {k: sorted(v) for k, v in per.items()
            if len(v) > 1 and all(cached[k]) and any(s >= 2 for _, s in v)}


def bucket_of(anchors):
    """Which stratum a lexeme belongs to — see the module docstring."""
    return "mixed" if any(s == 1 for _, s in anchors) else "all-strong"


def quotas(keys, total):
    """Per-language sample sizes for one stratum: a floor per language (so de/no are not
    crowded out), then the remainder shared proportionally by largest remainder. The floor
    scales with the stratum — MIN_PER_LANG across 5 languages would overrun a 15-row
    stratum — and is capped by what each language actually has."""
    by_lang = collections.Counter(lang for _, lang, _ in keys)
    langs = sorted(by_lang)
    floor = max(1, min(MIN_PER_LANG, total // max(1, len(langs))))
    n_lang = {lg: min(floor, by_lang[lg]) for lg in langs}
    left = total - sum(n_lang.values())
    room = {lg: by_lang[lg] - n_lang[lg] for lg in langs}
    total_room = sum(room.values())
    for lg in langs:                       # largest-remainder, deterministic on sorted langs
        if left <= 0 or not total_room:
            break
        take = min(room[lg], round(left * room[lg] / total_room))
        n_lang[lg] += take
    short = total - sum(n_lang.values())
    for lg in sorted(langs, key=lambda l: (-room[l], l)):
        while short > 0 and n_lang[lg] < by_lang[lg]:
            n_lang[lg] += 1
            short -= 1
    return n_lang


def sample(cands):
    """BUCKET_TARGET lexemes per stratum, balanced across languages within each. A stratum
    short of its target gives its remainder to the other, so the sheet still totals
    N_TOTAL."""
    rng = random.Random(SEED)
    by_bucket = {b: sorted(k for k in cands if bucket_of(cands[k]) == b)
                 for b in BUCKET_TARGET}
    targets = dict(BUCKET_TARGET)
    for b in sorted(targets):                      # reallocate any shortfall, deterministically
        spare = targets[b] - len(by_bucket[b])
        if spare > 0:
            targets[b] = len(by_bucket[b])
            for other in sorted(k for k in targets if k != b):
                add = min(spare, len(by_bucket[other]) - targets[other])
                targets[other] += add
                spare -= add
    picked = []
    for b in sorted(targets):
        keys = by_bucket[b]
        n_lang = quotas(keys, targets[b])
        for lg in sorted({k[1] for k in keys}):
            want = n_lang.get(lg, 0)
            if want:
                picked += rng.sample(sorted(k for k in keys if k[1] == lg), want)
    return sorted(picked)


def gloss_of(cn, lid, lang):
    """The existing sense's glosses — what the anchor actually has to match. Multi-language
    like the anchor side: target language first, then English, then the rest; [lg]-prefixed,
    max 3, ' · '-joined."""
    rows = cn.execute(
        """SELECT sg.lang, sg.gloss FROM wd.sense s JOIN wd.sense_gloss sg ON sg.sid = s.sid
           WHERE s.lid = ?""", [lid]).fetchall()
    by = {lg: g for lg, g in rows if g}
    order = sorted(by, key=lambda lg: (lg != lang, lg != "en", lg))
    return " · ".join(f"[{lg}] {by[lg]}" for lg in order[:3])


def anchor_text(cn, qid, lang):
    """(labels, descs) for the anchor, target language first — the signal a human needs to
    judge it. Multi-language on both sides: labels deduped across display languages (max 4),
    descriptions [lg]-prefixed (max 3), each joined with ' · '."""
    entry = {lg: (lab, ds) for lg, lab, ds in
             cn.execute("SELECT lang, label, desc FROM it.item_lang WHERE qid = ?", [qid])}
    if entry:
        resolved = config.resolve_concept_langs(entry, config.CONCEPT_LANGS_DISPLAY, target=lang)
    else:
        # anchor never reached fetch_anchor_langs — fall back to the de/en/sv columns of
        # `item`, target language first. Reading only label_en left Q108554349/Q2268687
        # (German-only) blank, and an unlabelled anchor cannot be adjudicated at all.
        r = cn.execute("SELECT label_de, label_en, label_sv, desc_de, desc_en, desc_sv "
                       "FROM it.item WHERE qid = ?", [qid]).fetchone() or [None] * 6
        by = dict(zip(("de", "en", "sv"), zip(r[:3], r[3:])))
        order = [lang] + [lg for lg in ("en", "de", "sv") if lg != lang]
        resolved = [(lg, *by[lg]) for lg in order if lg in by and any(by[lg])]
    labels, descs, seen = [], [], set()
    for lg, lab, ds in resolved:
        if lab and lab.casefold() not in seen and len(labels) < 4:
            seen.add(lab.casefold())
            labels.append(lab)
        if ds and len(descs) < 3:
            descs.append(f"[{lg}] {ds}")
    return " · ".join(labels), " · ".join(descs)


def build(cn, keys, cands, decisions):
    out = []
    for lid, lang, lemma in keys:
        gl = gloss_of(cn, lid, lang)
        anchors = cands[(lid, lang, lemma)]
        for qid, sup in sorted(anchors, key=lambda t: (-t[1], t[0])):
            lab, desc = anchor_text(cn, qid, lang)
            v = cn.execute("SELECT verdict, reason FROM vf.verdict "
                           "WHERE target_lid = ? AND anchor_qid = ?", [lid, qid]).fetchone()
            out.append({
                "lid": lid, "lang": lang, "lemma": lemma,
                "stratum": bucket_of(anchors), "sense_gloss": gl,
                "anchor_qid": qid, "anchor_label": lab, "anchor_desc": desc,
                "support": sup, "n_anchors": len(anchors),
                "cur_verdict": (v or ["", ""])[0] or "",
                "cur_reason": ((v or ["", ""])[1] or "")[:120],
                "decided": (decisions.get((lid, qid)) or {}).get("status", ""),
                "verdict": "", "note": "",
            })
    return out


CSS = ("body{font:14px/1.5 system-ui,sans-serif;margin:2rem;max-width:62rem}"
       ".lx{border:1px solid #ccc;border-radius:6px;padding:.8rem 1rem;margin:1rem 0}"
       ".lx.done{opacity:.55}.hd{font-size:1.15rem;font-weight:600}"
       ".gl{color:#444;margin:.2rem 0 .6rem}"
       "table{border-collapse:collapse;width:100%}td,th{border-top:1px solid #eee;"
       "padding:.35rem .5rem;text-align:left;vertical-align:top}"
       ".q{font-family:ui-monospace,monospace;white-space:nowrap}"
       "a{color:#36c}"
       ".d{color:#555}.y{color:#0a0}.n{color:#a00}"
       "form{display:inline;margin:0}"
       "button{font-size:.8rem;padding:2px 7px;margin-right:3px;cursor:pointer;"
       "border:1px solid #ccc;border-radius:4px;background:#fafafa}"
       ".v{font-weight:700;font-size:.8rem;text-transform:uppercase}"
       ".v.best{color:#197a19}.v.ok{color:#36c}.v.wrong{color:#b00}"
       ".bar{position:sticky;top:0;background:#fff;padding:.5rem 0;border-bottom:1px solid #ddd}"
       "@media(prefers-color-scheme:dark){body{background:#111;color:#ddd}"
       ".lx{border-color:#444}.gl,.d{color:#aaa}td,th{border-color:#333}"
       ".bar{background:#111;border-color:#444}button{background:#222;color:#ddd;"
       "border-color:#555}a{color:#8ab4f8}}")


def _group(rows):
    seen = []
    for r in rows:
        key = (r["lid"], r["lemma"], r["lang"])
        if not seen or seen[-1][0] != key:
            seen.append((key, []))
        seen[-1][1].append(r)
    return seen


def _buttons(r):
    """best / ok / wrong for one anchor row. `best` also fills the lexeme's still-empty
    rows with `wrong`, since that is the common shape and makes the usual lexeme one click;
    anything deserving `ok` can be upgraded afterwards."""
    cur = r["verdict"]
    out = []
    for act in ("best", "ok", "wrong"):
        mark = "•" if cur == act else ""
        out.append(
            f"<form method=post action=/act><input type=hidden name=lid value='{r['lid']}'>"
            f"<input type=hidden name=qid value='{r['anchor_qid']}'>"
            f"<input type=hidden name=action value='{act}'>"
            f"<button>{mark}{act}</button></form>")
    return "".join(out)


def render(rows, interactive=False):
    by_lex = _group(rows)
    done = sum(1 for _, rs in by_lex if all(r["verdict"] for r in rs))
    if interactive:      # unjudged first, original order within each half
        by_lex.sort(key=lambda kr: all(r["verdict"] for r in kr[1]))
    blocks = []
    for (lid, lemma, lang), rs in by_lex:
        complete = all(r["verdict"] for r in rs)
        tr = "".join(
            f"<tr><td class=q><a href='https://www.wikidata.org/wiki/"
            f"{html.escape(r['anchor_qid'])}' target=_blank>{html.escape(r['anchor_qid'])}</a></td>"
            f"<td><b>{html.escape(r['anchor_label'] or '?')}</b></td>"
            f"<td class=d>{html.escape(r['anchor_desc'])}</td>"
            f"<td>sup {r['support']}</td>"
            f"<td class={'y' if r['cur_verdict'] == 'yes' else 'n'}>"
            f"{html.escape(r['cur_verdict'] or '—')}</td>"
            + (f"<td>{_buttons(r)}</td>" if interactive else "")
            + f"<td class='v {r['verdict']}'>{html.escape(r['verdict'] or '')}</td></tr>"
            for r in rs)
        lex_actions = ""
        if interactive:
            lex_actions = "".join(
                f"<form method=post action=/act><input type=hidden name=lid value='{lid}'>"
                f"<input type=hidden name=action value='{act}'><button>{label}</button></form>"
                for act, label in (("none", "none fit"), ("clear", "reset")))
        blocks.append(
            f"<div class='lx{" done" if complete and interactive else ""}'>"
            f"<div class=hd>{html.escape(lemma)} "
            f"<span class=d>({lang}) {lid} · {rs[0]['stratum']} · {len(rs)} candidates</span>"
            f"</div><div class=gl>{html.escape(rs[0]['sense_gloss'])}</div>"
            f"<table><tr><th>anchor<th>label<th>description<th>sup<th>verifier"
            + ("<th>mark" if interactive else "") + "<th>verdict</tr>"
            + f"{tr}</table>{lex_actions}</div>")
    head = (f"<div class=bar><b>{done}/{len(by_lex)}</b> lexemes adjudicated · "
            f"{len(rows)} candidate rows · writing to {html.escape(SHEET)}</div>"
            if interactive else
            f"<p class=d>{len(by_lex)} lexemes, {len(rows)} candidate anchors. "
            f"Run <code>--serve</code> to adjudicate by clicking, or fill the "
            f"<code>verdict</code> column in {html.escape(SHEET)} by hand.</p>")
    return (f"<!doctype html><meta charset=utf-8><title>multi-candidate adjudication</title>"
            f"<style>{CSS}</style><h1>Multi-candidate adjudication</h1>{head}"
            f"{''.join(blocks)}")


def load_sheet():
    with open(SHEET, newline="") as f:
        return list(csv.DictReader(f))


def save_sheet(rows):
    with open(SHEET, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)


def apply_action(rows, lid, qid, action):
    """Mutate the sheet in place. Actions are idempotent, so a double-click or a browser
    replay cannot corrupt a lexeme."""
    lex = [r for r in rows if r["lid"] == lid]
    if not lex:
        return
    if action == "clear":
        for r in lex:
            r["verdict"] = ""
    elif action == "none":
        for r in lex:
            r["verdict"] = "wrong"
    elif action == "best":
        for r in lex:
            if r["anchor_qid"] == qid:
                r["verdict"] = "best"
            elif r["verdict"] in ("", "best"):   # one best per lexeme; fill the rest
                r["verdict"] = "wrong"
    elif action in ("ok", "wrong"):
        for r in lex:
            if r["anchor_qid"] == qid:
                r["verdict"] = action


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(body.encode())

    def do_GET(self):
        self._send(200, render(load_sheet(), interactive=True))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = urllib.parse.parse_qs(self.rfile.read(n).decode())
        rows = load_sheet()
        apply_action(rows, data.get("lid", [""])[0], data.get("qid", [""])[0],
                     data.get("action", [""])[0])
        save_sheet(rows)
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *a):
        pass


def serve():
    rows = load_sheet()
    by_lex = _group(rows)
    done = sum(1 for _, rs in by_lex if all(r["verdict"] for r in rs))
    print(f"adjudication UI on http://localhost:{PORT}  "
          f"({done}/{len(by_lex)} lexemes done) — Ctrl-C to stop")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing sheet (DISCARDS adjudications already made)")
    ap.add_argument("--serve", action="store_true",
                    help=f"adjudicate the existing sheet in a browser on :{PORT}")
    args = ap.parse_args()
    if args.serve:
        if not os.path.exists(SHEET):
            raise SystemExit(f"{SHEET} does not exist — build it first")
        return serve()
    if os.path.exists(SHEET) and not args.force:
        raise SystemExit(f"{SHEET} exists — refusing to overwrite adjudications (--force to "
                         f"regenerate from scratch)")
    import review_queue
    cn = connect()
    cands = pool(cn)
    keys = sample(cands)
    rows = build(cn, keys, cands, review_queue.load_decisions())
    os.makedirs(os.path.dirname(PAGE), exist_ok=True)
    with open(SHEET, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    with open(PAGE, "w") as f:
        f.write(render(rows))
    per_lex = collections.Counter(r["lid"] for r in rows)
    strata = collections.Counter(bucket_of(cands[k]) for k in keys)
    print(f"pool: {len(cands):,} adjudicable multi-candidate lexemes "
          f"(>=1 candidate at support >= 2)")
    print(f"sampled {len(keys)} lexemes / {len(rows)} candidate rows -> {SHEET}")
    print(f"  by stratum: {dict(strata)}")
    print(f"  by lang: {dict(collections.Counter(k[1] for k in keys))}")
    print(f"  anchors per lexeme: {dict(sorted(collections.Counter(per_lex.values()).items()))}")
    print(f"  readable layout -> {PAGE}")


if __name__ == "__main__":
    main()
