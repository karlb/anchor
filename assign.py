#!/usr/bin/env python3
"""Which SENSE of a polysemous lexeme does an anchor belong to?

The production verifier (verify.py) asks a PAIRWISE question — "does this noun denote this
concept?" — and answers it at LEXEME granularity. That is sound only while gate B pins the
target to exactly one sense, which is why the pilot write stream is monosemous. The
polysemous lane (gates.poly_match_clause, m>=2 with a free sense) needs one more answer
before a human can review anything: WHICH sense, or none of them.

This module owns that question and nothing else. It does not verify, does not decide, and
has no write path — write.py refuses a polysemous target outright. Its output is an
advisory the review UI shows beside the candidate, so the human approves a SENSE, not a
lexeme.

THE PROMPT IS THE MEASURED ONE. assign_eval.py imports SYSTEM/PROMPT_VER from here rather
than carrying its own copy, so the text that was measured is the text that ships. Editing
SYSTEM moves PROMPT_VER, which invalidates the assignment cache AND detaches the measured
numbers from the running prompt — if you edit it, re-run the arms. What was measured
(assign_eval.py, 2026-08-15, n=876):

  selection   93.0-94.7% top-1, and reading every miss found ZERO clean model errors —
              the residual is mostly Wikidata recording the same sense twice
  abstention  says "none" on 60% of questions whose answer was removed, against 1% of
              questions whose answer was present: a 60:1 discrimination
  residual    ~9.5% pick a genuinely different sense, and only in the constructed case
              where the true sense is absent from Wikidata entirely

  => human review, never auto-approve. The reviewer must see the OTHER senses; that is
     where the 9.5% is caught, and it is why render() shows the whole inventory.

    uv run assign.py --limit 200      # assign the poly lane's verifier-'yes' rows
    uv run assign.py --report
"""
import argparse
import collections
import hashlib
import sqlite3

import config
import gates
import llm
import verify

DB = config.ASSIGN

SYSTEM = (
    "You assign a Wikidata concept to ONE sense of a dictionary word, or to none of them.\n"
    "Each item gives a language, a noun, that noun's NUMBERED senses with their glosses, and "
    "one concept (label + descriptions). Decide which numbered sense the concept is the "
    "item-for-this-sense of.\n"
    "Answer with the sense NUMBER when the concept denotes what that sense means. Allow "
    "ordinary translation and register differences, and allow the concept to be somewhat "
    "wider or narrower than the gloss — pick the sense it fits best.\n"
    'Answer "none" when the concept denotes no sense listed: a different sense of the word '
    "that is not among those listed, a homograph, or an unrelated concept. Prefer \"none\" "
    "over forcing a poor fit — a wrong assignment is worse than an abstention.\n"
    "DESCRIPTION OVER LABEL: a shared word-form is not enough. Read the concept's "
    "description; if it names a different kind of thing than the gloss does, that is not the "
    "sense.\n"
    'Reply ONLY with a JSON array: [{"i":1,"sense":2,"reason":"<=12 words"}, ...] where '
    '"sense" is the sense number or the string "none". One object per item, no prose.'
)
PROMPT_VER = hashlib.sha1(SYSTEM.encode()).hexdigest()[:8]

SCHEMA = """
CREATE TABLE IF NOT EXISTS assigned(
    target_lid TEXT, anchor_qid TEXT, prompt_ver TEXT,
    -- the chosen S-id, or the literal 'none' (no offered sense fits) / 'error'.
    sid TEXT, reason TEXT, model TEXT, n_choices INTEGER, chosen_idx INTEGER,
    PRIMARY KEY (target_lid, anchor_qid, prompt_ver)
) WITHOUT ROWID;
"""


def connect_ro():
    cn = sqlite3.connect("file:" + config.CANDIDATES + "?mode=ro", uri=True)
    for alias, path in (("it", config.ITEMS), ("wd", config.WIKIDATA),
                        ("vf", config.VERIFY)):
        cn.execute("ATTACH DATABASE ? AS " + alias, ["file:" + path + "?mode=ro"])
    return cn


def senses_of(cn, lid, lang):
    """[(sid, gloss)] in a stable order (sid), target language preferred then en, then any.

    Senses with no gloss in ANY language are dropped: an empty line is not a choice, and
    offering it would let the model 'pick' something the human cannot read either. That
    also means the offered set can be smaller than the lexeme's sense count — the review UI
    shows the full inventory separately so the gap is visible rather than silent."""
    by = {}
    for sid, lg, gloss in cn.execute(
            "SELECT sid, lang, gloss FROM wd.sense_gloss WHERE lid = ?", [lid]):
        by.setdefault(sid, {})[lg] = gloss
    out = []
    for sid in sorted(by):
        g = by[sid]
        text = next((g[c] for c in (lang, "en", "de", "sv") if g.get(c)),
                    next(iter(g.values()), ""))
        out.append((sid, text))
    return [(s, t) for s, t in out if t]


def concept_text(cn, qid, lang, il_map):
    """The anchor concept in the SAME +multi format the production verifier grades on, so an
    assignment is never made from a different rendering than the verdict beside it."""
    r = cn.execute("SELECT label_de, label_en, label_sv, desc_de, desc_en, desc_sv "
                   "FROM it.item WHERE qid = ?", [qid]).fetchone() or [None] * 6
    row = ("", lang, "", qid, 0, *r)
    return verify.fmt_concept(row, langs=config.CONCEPT_LANGS_VERIFY, il_map=il_map)


def parse_answer(raw, sen):
    """(sid|'none'|'out_of_range', chosen_idx|None) from the model's `sense` field."""
    if isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit()):
        idx = int(raw)
        if 1 <= idx <= len(sen):
            return sen[idx - 1][0], idx
        return "out_of_range", None
    return str(raw).strip().lower(), None


def pool(cn):
    """[(lid, qid, lemma, lang)] — poly-lane rows the verifier already said 'yes' to.

    Assignment runs DOWNSTREAM of verification, not instead of it: the relation the verifier
    emits (EQUAL/BROADER/NARROWER/LATERAL) drives the scope hints and the calibrated
    auto-defer gates, and this module answers a different question (which sense), not a
    better version of the same one."""
    return cn.execute(
        "SELECT c.target_lid, c.anchor_qid, c.target_lemma, c.target_lang "
        "FROM candidate c "
        "JOIN it.item i ON i.qid = c.anchor_qid AND NOT i.missing "
        "JOIN vf.verdict v ON v.target_lid = c.target_lid AND v.anchor_qid = c.anchor_qid "
        "WHERE v.verdict = 'yes' AND "
        + gates.high_tier_where("c", "it", n_senses="poly")
        + " ORDER BY c.target_lang, c.target_lemma").fetchall()


def run(limit, model, batch, force):
    cn = connect_ro()
    items = pool(cn)
    out = sqlite3.connect(DB)
    out.executescript(SCHEMA)
    done = set() if force else {
        (r[0], r[1]) for r in out.execute(
            "SELECT target_lid, anchor_qid FROM assigned WHERE prompt_ver = ?", [PROMPT_VER])}
    print(f"poly lane, verifier-'yes': {len(items):,} rows; {len(done & {(i[0], i[1]) for i in items}):,} already assigned")

    todo = []
    for lid, qid, lemma, lang in items:
        if (lid, qid) in done:
            continue
        sen = senses_of(cn, lid, lang)
        if len(sen) < 2:
            continue          # nothing to choose between (all but one sense unglossed)
        todo.append((lid, qid, lemma, lang, sen))
    # Same item_lang requirement the verifier enforces: no +multi concept, no assignment.
    il_map = verify.load_item_lang({t[1] for t in todo})
    kept = [t for t in todo if t[1] in il_map]
    if len(kept) < len(todo):
        print(f"  skipping {len(todo) - len(kept):,} — anchor absent from item_lang "
              f"(run fetch_anchor_langs.py)")
    todo = kept[:limit] if limit else kept
    if not todo:
        print("nothing to do")
        return report(out)
    print(f"asking {len(todo):,}  (model={model}, batch={batch}, prompt_ver={PROMPT_VER})")

    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        lines = []
        for n, (_lid, qid, lemma, lang, sen) in enumerate(chunk, 1):
            q, lab, desc = concept_text(cn, qid, lang, il_map)
            senses = "; ".join(f"({j}) {t}" for j, (_s, t) in enumerate(sen, 1))
            lines.append(
                f'{n}. language={config.LANGNAME.get(lang, lang)} noun="{lemma}" | '
                f'senses: {senses} | concept {q}: label="{lab}" description="{desc}"')
        try:
            ans = llm.batch_json(SYSTEM, "Items:\n" + "\n".join(lines), model, len(chunk),
                                 {"sense": "error", "reason": ""})
        except Exception as e:  # noqa: BLE001 — one bad batch must not kill the run
            print(f"  batch {i // batch} failed: {e}")
            ans = [{"sense": "error", "reason": ""}] * len(chunk)
        recs = []
        for (lid, qid, _lemma, _lang, sen), a in zip(chunk, ans):
            sid, idx = parse_answer(a.get("sense"), sen)
            recs.append((lid, qid, PROMPT_VER, sid, str(a.get("reason", ""))[:150],
                         model, len(sen), idx))
        out.executemany("INSERT OR REPLACE INTO assigned VALUES (?,?,?,?,?,?,?,?)", recs)
        out.commit()
        print(f"  {min(i + batch, len(todo)):,}/{len(todo):,}")

    u = llm.usage_summary()
    if u["calls"]:
        print(f"\nclaude -p usage: {u['calls']:,} calls  ~${u['cost_usd']:.3f}  "
              f"(~${u['cost_usd'] / u['calls']:.4f}/call)")
    report(out)


def load(lids_qids=None):
    """{(lid, qid): (sid, reason, n_choices)} for the CURRENT prompt — the review UI's reader.
    Missing key = not assigned yet; the UI must say so rather than imply a sense."""
    try:
        cn = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
        rows = cn.execute("SELECT target_lid, anchor_qid, sid, reason, n_choices "
                          "FROM assigned WHERE prompt_ver = ?", [PROMPT_VER]).fetchall()
        cn.close()
    except sqlite3.OperationalError:
        return {}                                  # no DB / no table yet
    out = {(l, q): (s, r, n) for l, q, s, r, n in rows}
    return {k: v for k, v in out.items() if k in lids_qids} if lids_qids else out


def report(out=None):
    out = out or sqlite3.connect(DB)
    out.executescript(SCHEMA)
    rows = list(out.execute("SELECT sid, n_choices, chosen_idx FROM assigned "
                            "WHERE prompt_ver = ?", [PROMPT_VER]))
    if not rows:
        print("no assignments yet")
        return
    n = len(rows)
    none = sum(1 for r in rows if r[0] == "none")
    err = sum(1 for r in rows if r[0] in ("error", "out_of_range"))
    print(f"\nassigned {n:,} poly-lane pairs (prompt_ver {PROMPT_VER})")
    print(f"  a sense was chosen : {n - none - err:,} = {(n - none - err) / n:.1%}")
    print(f"  abstained ('none') : {none:,} = {none / n:.1%}   <- these must never reach a write")
    if err:
        print(f"  unparseable        : {err:,}")
    # The measured abstention rate was 60% when the answer was absent and 1% when present.
    # A live rate far above 1% is the signal that the true sense is missing from Wikidata
    # more often than the gold pool implied — which is exactly the multiplier the measurement
    # could not supply. Worth watching as the lane fills.
    print(f"  (measured: 1% abstention when the sense IS present, 60% when it is not — a "
          f"live rate well above 1%\n   means the true sense is often missing from Wikidata, "
          f"the one multiplier assign_eval could not measure)")
    ch = collections.Counter(r[1] for r in rows)
    print("  offered choice sets: " + "  ".join(f"{k}:{v}" for k, v in sorted(ch.items())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200, help="max pairs to assign (0 = all)")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--force", action="store_true", help="re-ask cached pairs")
    ap.add_argument("--report", action="store_true", help="print results without asking")
    args = ap.parse_args()
    if args.report:
        return report()
    run(args.limit, args.model, args.batch, args.force)


if __name__ == "__main__":
    main()
