#!/usr/bin/env python3
"""Measure the per-sense assignment question, without touching the production verifier.

The pipeline asks a PAIRWISE question ("does this noun denote this concept?") and writes at
lexeme granularity, which only works because gate B pins the target to exactly one sense.
The general form asks a per-sense question instead — "WHICH sense of this lexeme does this
item belong to, if any?" — and that is what has to be measured before any polysemous lane is
built. This script measures it and nothing else: no write path, no change to review.

It deliberately carries its OWN system prompt and its OWN cache (data/assign.sqlite3). It
never imports or edits verify.SYSTEM, so verify.PROMPT_VER does not move and none of the
~2,670 cached production verdicts are invalidated.

Two arms, because each is blind to the other's failure mode:

  --arm gold     Self-grading, zero human labels. Take lexemes with >= 2 senses where one
                 sense ALREADY carries P5137 -> Q on Wikidata and our own clusters
                 independently propose that same Q for that lexeme. Hide the link, ask which
                 sense it belongs to, score against the sense that really carries it. Built
                 upstream of match.py:146-149 (which drops already-grounded pairs from
                 `candidate`), so it must come from cluster_anchor + matched_member.
                 Measures: can it pick the right sense out of several?
                 BLIND TO: over-assignment. Every question has a correct sense by
                 construction, so a model that always names some sense scores 100%.

  --arm control  The complement, and the reason the gold alone is not enough. Monosemous
                 pairs the human already decided: an approved/written pair should come back
                 as the sole sense, a REJECTED pair should come back as "none". Measures the
                 NONE escape — the over-assignment rate the gold cannot see.

    uv run assign_eval.py --arm control --limit 200
    uv run assign_eval.py --arm gold    --limit 300
    uv run assign_eval.py --report
"""
import argparse
import collections
import csv
import json
import os
import random
import sqlite3

import config
import llm
import verify

DB = os.path.join(config.DATA, "assign.sqlite3")   # config-derived, never cwd-relative
SEED = 20260815

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
PROMPT_VER = __import__("hashlib").sha1(SYSTEM.encode()).hexdigest()[:8]

SCHEMA = """
CREATE TABLE IF NOT EXISTS assign(
    arm TEXT, target_lid TEXT, anchor_qid TEXT, answer TEXT, expected TEXT,
    reason TEXT, model TEXT, prompt_ver TEXT,
    -- how many senses were offered, and which position held the right answer. Recorded so
    -- the report can print the ALWAYS-ANSWER-FIRST baseline: senses are ordered by sid and
    -- the first is right ~61% of the time, so a top-1 score means nothing next to chance.
    n_choices INTEGER, expected_idx INTEGER,
    PRIMARY KEY (arm, target_lid, anchor_qid)
) WITHOUT ROWID;
"""


def connect_ro():
    cn = sqlite3.connect("file:" + config.CANDIDATES + "?mode=ro", uri=True)
    for alias, path in (("it", config.ITEMS), ("wd", config.WIKIDATA), ("cl", config.CLUSTERS)):
        cn.execute("ATTACH DATABASE ? AS " + alias, ["file:" + path + "?mode=ro"])
    return cn


def senses_of(cn, lid, lang):
    """[(sid, gloss)] in a stable order, target language preferred then en, then any."""
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


def build_gold(cn):
    """[(lid, lang, lemma, qid, correct_sid)] — the self-grading set.

    Built from cluster_anchor + matched_member rather than from `candidate`, because
    match.py:146-149 removes exactly the already-grounded pairs this arm needs. Excludes
    items sitting on more than one sense of the same lexeme (the key would be ambiguous)."""
    onwiki = collections.defaultdict(set)
    for lid, sid, qid in cn.execute("SELECT lid, sid, qid FROM wd.sense_item"):
        onwiki[(lid, qid)].add(sid)
    lex = {lid: (lang, None) for lid, lang in cn.execute("SELECT lid, lang FROM wd.lexeme")}
    lemma = {}
    for lid, lm in cn.execute("SELECT lid, lemma FROM wd.lemma"):
        lemma.setdefault(lid, lm)
    n_senses = dict(cn.execute("SELECT lid, count(*) FROM wd.sense GROUP BY 1"))

    seen, out = set(), []
    for le, sn, qid in cn.execute("SELECT lexentry, sense_num, anchor_qid FROM cluster_anchor"):
        for (lid,) in cn.execute(
                "SELECT DISTINCT lid FROM matched_member WHERE lexentry=? AND sense_num=?",
                [le, sn]):
            key = (lid, qid)
            if key in seen:
                continue
            seen.add(key)
            sids = onwiki.get(key)
            if not sids or len(sids) > 1:          # not linked, or ambiguous key
                continue
            if n_senses.get(lid, 0) < 2:           # needs a real choice to be a test
                continue
            lang = lex.get(lid, ("", None))[0]
            if lang not in config.REVIEW_LANGS:
                continue
            out.append((lid, lang, lemma.get(lid, ""), qid, next(iter(sids))))
    return out


def build_control(cn):
    """[(lid, lang, lemma, qid, expected)] from the human decision log — monosemous only.

    expected = the lexeme's sole sid for approved/written, or the literal 'none' for
    rejected. This is the arm that can catch over-assignment, which the gold cannot."""
    import review_queue
    decided = review_queue.load_decisions()
    sole = {}
    for lid, sid in cn.execute(
            "SELECT lid, sid FROM wd.sense WHERE lid IN "
            "(SELECT lid FROM wd.sense GROUP BY lid HAVING count(*) = 1)"):
        sole[lid] = sid
    lemma = {}
    for lid, lm in cn.execute("SELECT lid, lemma FROM wd.lemma"):
        lemma.setdefault(lid, lm)
    lang = dict(cn.execute("SELECT lid, lang FROM wd.lexeme"))
    out = []
    for (lid, qid), dec in decided.items():
        status = (dec.get("status") or "").strip()
        if status not in ("approved", "written", "rejected") or lid not in sole:
            continue
        out.append((lid, lang.get(lid, ""), lemma.get(lid, ""), qid,
                    "none" if status == "rejected" else sole[lid]))
    return out


def concept_text(cn, qid, lang, il_map):
    """The anchor concept in the SAME +multi format the production verifier grades on, so
    this measurement is not accidentally testing a different concept rendering."""
    r = cn.execute("SELECT label_de, label_en, label_sv, desc_de, desc_en, desc_sv "
                   "FROM it.item WHERE qid = ?", [qid]).fetchone() or [None] * 6
    row = ("", lang, "", qid, 0, *r)
    return verify.fmt_concept(row, langs=config.CONCEPT_LANGS_VERIFY, il_map=il_map)


def run(arm, limit, model, batch, force):
    cn = connect_ro()
    items = build_gold(cn) if arm == "gold" else build_control(cn)
    print(f"[{arm}] built {len(items):,} candidate questions")

    out = sqlite3.connect(DB)
    out.executescript(SCHEMA)
    done = set() if force else {
        (r[0], r[1]) for r in out.execute(
            "SELECT target_lid, anchor_qid FROM assign WHERE arm = ? AND prompt_ver = ?",
            [arm, PROMPT_VER])}

    pool = []
    for lid, lang, lemma, qid, expected in items:
        if (lid, qid) in done:
            continue
        sen = senses_of(cn, lid, lang)
        if len(sen) < (2 if arm == "gold" else 1):
            continue                                   # no glosses -> nothing to choose from
        if arm == "gold" and expected not in {s for s, _ in sen}:
            continue                                   # the right answer must be answerable
        pool.append((lid, lang, lemma, qid, expected, sen))
    # The concept MUST render in the +multi format the live verifier grades on, and this
    # filter has to run BEFORE the sample is drawn or the sample comes up short.
    # verify.fmt_concept silently falls back to the RETIRED de/en/sv columns for any anchor
    # missing from item_lang, and the gold pool is drawn from cluster_anchor + matched_member —
    # far wider than the ABD-and-senseless universe fetch_anchor_langs populates — so most gold
    # anchors would quietly be graded on the retired format. Measured before this filter
    # existed: 3,976 of the gold arm's 5,905 anchors had no item_lang row. A number that
    # averages two concept renderings is not comparable to production or to the control arm,
    # so drop them rather than measure the mixture.
    il_map = verify.load_item_lang({p[3] for p in pool})
    kept = [p for p in pool if p[3] in il_map]
    if len(kept) < len(pool):
        print(f"[{arm}] excluded {len(pool) - len(kept):,} of {len(pool):,} questions — anchor "
              f"absent from item_lang, would grade on the retired de/en/sv concept "
              f"(widen with `uv run fetch_anchor_langs.py`)")
    pool = kept

    rng = random.Random(SEED)
    rng.shuffle(pool)
    if arm == "control":
        # Take EVERY rejected pair before any approved one. The over-assignment rate is the
        # only thing this arm measures that the gold cannot, and there are just 91 negatives
        # in the whole log — sampling them proportionally (~34 of 200) would put a uselessly
        # wide interval on the number the arm exists for.
        neg = [p for p in pool if p[4] == "none"]
        pos = [p for p in pool if p[4] != "none"]
        pool = neg[:limit] + pos[:max(0, limit - len(neg))]
    else:
        pool = pool[:limit]
    if not pool:
        print("nothing to do (all cached, or nothing survived the item_lang filter)")
        return
    print(f"[{arm}] asking {len(pool):,}  (model={model}, batch={batch}, prompt_ver={PROMPT_VER})")

    for i in range(0, len(pool), batch):
        chunk = pool[i:i + batch]
        lines = []
        for n, (lid, lang, lemma, qid, _exp, sen) in enumerate(chunk, 1):
            qid_, lab, desc = concept_text(cn, qid, lang, il_map)
            senses = "; ".join(f"({j}) {t}" for j, (_s, t) in enumerate(sen, 1))
            lines.append(
                f'{n}. language={config.LANGNAME.get(lang, lang)} noun="{lemma}" | '
                f'senses: {senses} | concept {qid_}: label="{lab}" description="{desc}"')
        try:
            ans = llm.batch_json(SYSTEM, "Items:\n" + "\n".join(lines), model, len(chunk),
                                 {"sense": "error", "reason": ""})
        except Exception as e:  # noqa: BLE001 — one bad batch must not kill the run
            print(f"  batch {i // batch} failed: {e}")
            ans = [{"sense": "error", "reason": ""}] * len(chunk)
        recs = []
        for (lid, lang, lemma, qid, expected, sen), a in zip(chunk, ans):
            raw = a.get("sense")
            if isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit()):
                idx = int(raw)
                answer = sen[idx - 1][0] if 1 <= idx <= len(sen) else "out_of_range"
            else:
                answer = str(raw).strip().lower()      # "none", "error", or junk
            sids = [s for s, _ in sen]
            recs.append((arm, lid, qid, answer, expected, str(a.get("reason", ""))[:150],
                         model, PROMPT_VER, len(sids),
                         sids.index(expected) + 1 if expected in sids else None))
        out.executemany("INSERT OR REPLACE INTO assign VALUES (?,?,?,?,?,?,?,?,?,?)", recs)
        out.commit()
        print(f"  {min(i + batch, len(pool)):,}/{len(pool):,}")

    u = llm.usage_summary()
    if u["calls"]:
        print(f"\nclaude -p usage: {u['calls']:,} calls  ~${u['cost_usd']:.3f}  "
              f"(~${u['cost_usd'] / u['calls']:.4f}/call)")
    report(out)


def _wilson(k, n, z=1.96):
    """Wilson score interval — the small-n honest one. A 1-of-18 rate quoted bare reads as
    5.6%; its interval runs past 25%, which is the number that decides whether the lane is
    safe, so the interval is printed rather than the point estimate alone."""
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - m), min(1.0, c + m)


def _relations(keys):
    """{(lid, qid): EQUAL|BROADER|...} from the production verdict cache. Read-only, and used
    only to interpret the control — it never feeds an assignment."""
    import re
    out = {}
    if not keys:
        return out
    vf = sqlite3.connect("file:" + config.VERIFY + "?mode=ro", uri=True)
    for lid, qid in keys:
        r = vf.execute("SELECT reason FROM verdict WHERE target_lid=? AND anchor_qid=?",
                       [lid, qid]).fetchone()
        m = re.match(r"\s*(EQUAL|BROADER|NARROWER|LATERAL|DIFFERENT|INCOHERENT)",
                     (r or [""])[0] or "")
        if m:
            out[(lid, qid)] = m.group(1)
    vf.close()
    return out


def report(out=None, csv_path=""):
    out = out or sqlite3.connect(DB)
    out.executescript(SCHEMA)
    rows = list(out.execute("SELECT arm, target_lid, anchor_qid, answer, expected, reason, "
                            "n_choices, expected_idx FROM assign WHERE prompt_ver = ?",
                            [PROMPT_VER]))
    if not rows:
        print("no results yet")
        return
    print("\n" + "=" * 62)
    for arm in ("control", "gold"):
        sub = [r for r in rows if r[0] == arm]
        if not sub:
            continue
        print(f"\n[{arm}]  n = {len(sub):,}")
        if arm == "gold":
            hit = sum(1 for r in sub if r[3] == r[4])
            none = sum(1 for r in sub if r[3] == "none")
            err = sum(1 for r in sub if r[3] in ("error", "out_of_range"))
            n = len(sub)
            lo, hi = _wilson(hit, n)
            print(f"  top-1 correct sense : {hit:,}/{n:,} = {hit / n:.1%}  95% CI [{lo:.1%}, {hi:.1%}]"
                  f"   <- the number the lane turns on")
            print(f"  abstained ('none')  : {none:,} = {none / n:.1%}  (a miss here, but a SAFE miss)")
            print(f"  wrong sense picked  : {n - hit - none - err:,} = {(n - hit - none - err) / n:.1%}"
                  f"  (the UNSAFE miss — this is what writes a bad edit)")
            if err:
                print(f"  unparseable         : {err:,}")
            # Baselines, without which the top-1 above is unreadable. Senses are listed in sid
            # order and the FIRST one holds the answer ~61% of the time, so a model that always
            # says "1" already scores far above chance. Beat the majority baseline, not chance.
            idx = [r[7] for r in sub if r[7]]
            if idx:
                first = sum(1 for i in idx if i == 1) / len(idx)
                chance = sum(1 / r[6] for r in sub if r[6]) / len(sub)
                print(f"  baselines           : always-answer-first {first:.1%}, "
                      f"uniform chance {chance:.1%}   <- top-1 must clear the FIRST of these")
        else:
            pos = [r for r in sub if r[4] != "none"]
            neg = [r for r in sub if r[4] == "none"]
            if pos:
                ok = sum(1 for r in pos if r[3] == r[4])
                print(f"  human APPROVED, model named the sole sense : {ok:,}/{len(pos):,} = {ok / len(pos):.1%}")
            if neg:
                ok = sum(1 for r in neg if r[3] == "none")
                assigned = [r for r in neg if r[3] != "none"]
                print(f"  human REJECTED, model said 'none'          : {ok:,}/{len(neg):,} = {ok / len(neg):.1%}")
                # That raw rate is NOT an over-assignment rate, and reading it as one was this
                # harness's first mistake. A human `rejected` is mostly a SCOPE/convention call
                # (BROADER/NARROWER/LATERAL): the item denotes the right sense, it is just not
                # the item to write. Naming that sense is the CORRECT answer on the sense axis.
                # Two things this reporting must not do: quote buckets that are not a partition
                # (EQUAL/INCOHERENT/unknown fell through a hole before), and use `assigned` as
                # the denominator — the sense axis can only be scored on rows that are
                # sense-level negatives to begin with.
                rel = _relations({(r[1], r[2]) for r in neg})
                SCOPE, NEG = ("BROADER", "NARROWER", "LATERAL"), ("DIFFERENT", "INCOHERENT")
                buckets = collections.Counter(rel.get((r[1], r[2]), "no verdict row") for r in assigned)
                print(f"    the {len(assigned):,} it did assign, by verifier relation: "
                      + ", ".join(f"{k} {v}" for k, v in buckets.most_common())
                      + f"   (scope {sum(buckets[k] for k in SCOPE)}, "
                      f"= a right-sense call the human rejected on scope, not a sense error)")
                sense_neg = [r for r in neg if rel.get((r[1], r[2])) in NEG]
                wrong = [r for r in sense_neg if r[3] != "none"]
                if sense_neg:
                    lo, hi = _wilson(len(wrong), len(sense_neg))
                    print(f"    wrong-sense rate, scored ONLY on the {len(sense_neg):,} sense-level "
                          f"negatives (DIFFERENT/INCOHERENT): {len(wrong):,}/{len(sense_neg):,} = "
                          f"{len(wrong) / len(sense_neg):.1%}  95% CI [{lo:.1%}, {hi:.1%}]")
                    print("      CAVEAT: those relations come from the production verifier — the same "
                          "model under a different prompt. decisions.csv carries no human sense-level\n"
                          "      label, so this is a consistency check, not an independent ground truth.")
                for r in wrong[:5]:
                    print(f"      wrong sense: {r[1]} {r[2]}  \"{r[5][:70]}\"")
    print("\n" + "=" * 62)
    if csv_path:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["arm", "target_lid", "anchor_qid", "answer", "expected", "reason"])
            w.writerows(rows)
        print(f"wrote {csv_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=("gold", "control"), help="which measurement to run")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--force", action="store_true", help="re-ask cached questions")
    ap.add_argument("--report", action="store_true", help="print results without asking anything")
    ap.add_argument("--csv", default="", help="also dump the raw answers here")
    ap.add_argument("--sizes", action="store_true", help="print the buildable set sizes and exit")
    ap.add_argument("--dump-qids", metavar="FILE", help="write the arm's anchor Q-ids that are "
                    "missing from item_lang, for `fetch_anchor_langs.py --qids-from FILE`. "
                    "fetch_anchor_langs' own needed_qids() is scoped to the ABD and "
                    "AD-senseless production selections, so it reports 0 to fetch for this "
                    "pool no matter how much of it is uncached.")
    args = ap.parse_args()
    if args.dump_qids:
        cn = connect_ro()
        items = build_gold(cn) if (args.arm or "gold") == "gold" else build_control(cn)
        qids = {q for _l, _lg, _lm, q, _e in items}
        missing = sorted(qids - set(verify.load_item_lang(sorted(qids))))
        with open(args.dump_qids, "w") as f:
            f.write("\n".join(missing) + ("\n" if missing else ""))
        print(f"{len(qids):,} distinct anchors in the {args.arm or 'gold'} arm; "
              f"{len(missing):,} missing from item_lang -> {args.dump_qids}")
        print(f"  next: uv run fetch_anchor_langs.py --qids-from {args.dump_qids}")
        return
    if args.sizes:
        cn = connect_ro()
        g, c = build_gold(cn), build_control(cn)
        print(f"gold    (self-grading, m>=2, already-linked): {len(g):,} pairs over "
              f"{len({x[0] for x in g}):,} lexemes")
        print(f"        by lang: {dict(collections.Counter(x[1] for x in g).most_common())}")
        print(f"control (decided monosemous pairs)         : {len(c):,} pairs "
              f"({sum(1 for x in c if x[4] == 'none'):,} rejected / "
              f"{sum(1 for x in c if x[4] != 'none'):,} approved-or-written)")
        return
    if args.report or not args.arm:
        return report(csv_path=args.csv)
    run(args.arm, args.limit, args.model, args.batch, args.force)


if __name__ == "__main__":
    main()
