#!/usr/bin/env python3
"""A/B the anchor-concept format against the human decision log — model PINNED.

Question: does feeding the verifier the concept's MULTILINGUAL CC0 descriptions (the
+multi arm) make it agree more with human reviewers than the base de/en/sv format —
especially by correcting wrong-relation calls (a BROADER flag that the richer concept
reveals is really EQUAL)? Ground truth = decisions.csv (approved/written = keep,
rejected = reject, deferred = defer).

Both arms run through the ONE unified concept formatter (verify.fmt_concept): `base`
passes langs=None (the legacy de/en/sv path, byte-identical to production); `+multi`
passes config.CONCEPT_LANGS_VERIFY, so the concept is built from the all-language CC0
cache (item_lang, fetch_anchor_langs.py) plus the multilingual-guide system prompt. There
is no separate wide-format code path or data store — just a wider language spec.

Every arm uses the SAME model (default sonnet) so the only varying factor is the concept
format. Each pair is verified `--samples` times per arm and the predicted action is
majority-voted to damp LLM nondeterminism.

    uv run ab_concept.py --suspect --samples 1     # LEAN first read on the scope slice
    uv run ab_concept.py --samples 3                # full run (pricey)
    uv run ab_concept.py --count                     # just size the job

The same rig also ablates the GLOSS (`--arms prod,-gloss`): the -gloss arm withholds the
target's own sense gloss and scores the loss against the decided pairs. Read it as "what does
the gloss contribute", NOT as a senseless-lane simulation — see the -gloss note in ARMS for the
measurement that ruled that reading out.

Resumable: every completed batch is cached to data/ab_concept_cache.jsonl keyed by
(arm, sample, lid, qid), so a kill / quota stall loses nothing — re-run the SAME command
and it pays only for batches not yet cached. On a quota / rate-limit error it ABORTS
cleanly (rather than scoring failed batches as fake 'defer's) so partial results stay
trustworthy; just re-run when quota recovers. Delete the cache file to force a clean run.
"""
import argparse
import collections
import csv
import json
import os
import sqlite3
import subprocess

import config
import review_queue as rq
import verify

# Each arm is a name -> kwargs for verify.run_batch. The multilingual guide now lives in
# verify.SYSTEM (production verifier is +multi+guided), so +multi just selects the wider language
# spec. NOTE: post-promotion the base arm also runs under the guided verify.SYSTEM; the historical
# non-guided base verdicts are preserved in the cache and used by score_approaches, so the locked
# gold comparison (base vs +multi) is unaffected.
ARMS = {
    "base": {},
    "+multi": dict(langs=config.CONCEPT_LANGS_VERIFY),
    # Nudge-validation arm: same wide format as +multi, but run AFTER the "description over label"
    # nudge was added to verify.SYSTEM. The cached +multi verdicts are the PRE-nudge baseline (the
    # ab cache is keyed by arm, not prompt, so they aren't re-run); +nudge is the post-nudge
    # candidate. The gold retest (score_approaches) compares the two. Don't delete the cache or the
    # baseline is lost. Run with --gold to keep the retest to the curated set.
    "+nudge": dict(langs=config.CONCEPT_LANGS_VERIFY),
    # GLOSS ABLATION. Same production prompt and concept format as +nudge, but the target's own
    # CC0 sense gloss is withheld, so verify.run_batch and every gloss-keyed hint downstream
    # (the demonym->ethnic nudge) go blind together. Measures what the gloss contributes,
    # population held fixed.
    #
    # It was BUILT as a senseless-lane simulation — the senseless verifier input is identical,
    # since verify.load_glosses returns nothing at n_senses = 0 — and that reading was WRONG.
    # Measured 2026-08-02 on the 534 decided pairs: false-keeps 28 -> 83. But the lane's own
    # decisions (`calibrate.py --lane senseless`, 47 reviewed then) run 6% reject vs the
    # monosemous lane's 13% — the human rejects LESS there, not 3x more. The two lanes are
    # asked different questions: monosemous is "does THIS sense denote X", senseless is "is X
    # A meaning worth creating a first sense for". Most of the ablation's defer -> keep flips
    # are the second question's correct answer, scored against the first question's rubric.
    # Two further biases, both flattering to prod: the reviewer saw prod's verdict + hints in
    # the queue when deciding, and the eval set is 18% de where the senseless pool is 73%.
    # So: use this arm for the gloss's contribution. For the senseless lane use its real
    # decisions — they exist, they carry the right rubric, and they cost nothing to read.
    "-gloss": dict(langs=config.CONCEPT_LANGS_VERIFY, nogloss=True),
    # The with-gloss baseline for that ablation, served FREE from the live verify cache rather
    # than re-run (seed_prod_arm): those verdicts already ARE the production verifier on these
    # exact pairs under the current prompt fingerprint, so re-running them would pay full price
    # to reproduce them and, being a fresh draw, would fold LLM nondeterminism into the delta.
    # samples=1 only — one cached verdict replayed 3x would fake a unanimous vote.
    "prod": dict(langs=config.CONCEPT_LANGS_VERIFY),
}
# Arms a bare `uv run ab_concept.py` scores. -gloss is opt-in (--arms +nudge,-gloss): it answers a
# different question than the concept-format A/B and shouldn't silently add a 4th arm's cost.
DEFAULT_ARMS = ("base", "+multi", "+nudge")
HUMAN = {"approved": "keep", "written": "keep", "rejected": "reject", "deferred": "defer"}
# The scope/coherence relations: the cases where the concept boundary is contested and
# multilingual context could plausibly help. --suspect restricts the eval set to these, so the
# format effect is measured where it can appear (not drowned by EQUAL-obvious keeps and clean
# homograph rejects).
SCOPE_RELS = ("BROADER", "NARROWER", "LATERAL", "INCOHERENT")
CACHE = os.path.join(config.DATA, "ab_concept_cache.jsonl")
QUOTA_MARKERS = ("quota", "rate limit", "rate-limit", "usage limit", "overloaded",
                 "429", "limit reached", "too many requests")


class QuotaAbort(RuntimeError):
    """Raised to stop the whole run on a quota/rate-limit failure (vs a transient batch error)."""


def predicted_action(verdict, reason, gloss=None, anchor_desc=None):
    """Collapse a verifier verdict+relation to the action it implies, via scope_kind —
    so the metric scores the decision-relevant output, not the raw token. gloss + anchor_desc
    enable the demonym→ethnic-group nudge."""
    if verdict == "no":
        return "reject"
    if verdict in ("uncertain", "error", "parse_error"):
        return "defer"
    _rel, kind = rq.scope_kind(reason, gloss, anchor_desc)
    return rq.HINTS[kind][0] or "keep"   # EQUAL / convention-keep / unflagged -> keep


def vote_action(counter):
    """Majority-vote a Counter of per-sample predicted_action outputs down to one action; a
    TIE -> defer (a split verifier belongs in human review). Single-sourced here so the gold
    harnesses (score_approaches, build_problematic_cases) can't fork the tie rule. run_arm keeps
    its own most_common vote — it feeds the live A/B, not the gold — but there are no ties in the
    cache today, so both rules agree on current data."""
    top = counter.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return "defer"
    return top[0][0]


def scope_rel_map():
    """{(lid, qid): REL token} from the production verify cache — the verifier's relation
    for each pair, used to restrict the eval set to scope/coherence cases."""
    db = sqlite3.connect(config.VERIFY)
    return {(lid, qid): rq.scope_hint(reason)[0]
            for lid, qid, reason in db.execute(
                "SELECT target_lid, anchor_qid, reason FROM verdict WHERE prompt_ver = ?",
                [verify.PROMPT_VER])}


def load_eval_rows(suspect=False):
    truth = {k: HUMAN[(d.get("status") or "").strip()]
             for k, d in rq.load_decisions().items()
             if (d.get("status") or "").strip() in HUMAN}
    rows = [r for r in verify.candidates(gate="ABD")
            if verify.has_evidence(r) and (r[0], r[3]) in truth]
    if suspect:
        rel = scope_rel_map()
        rows = [r for r in rows if rel.get((r[0], r[3])) in SCOPE_RELS]
    return rows, truth


def load_cache():
    """The jsonl A/B cache, minus failures.

    error/parse_error entries are skipped rather than loaded: they are what a timeout or a
    quota stall leaves behind, and a cached failure is not a measurement. Loading them made
    them permanent (nothing retries a present key) AND scored them — predicted_action maps
    them to 'defer', so an infrastructure failure entered the metric as a real verdict."""
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE) as f:
            for line in f:
                e = json.loads(line)
                if e["verdict"] in ("error", "parse_error"):
                    continue
                cache[(e["arm"], e["sample"], e["lid"], e["qid"])] = (e["verdict"], e["reason"])
    return cache


def seed_prod_arm(cache, rows):
    """Fill the 'prod' arm from the live verify cache so run_arm finds it done and pays nothing.

    Only verdicts under the CURRENT prompt fingerprint count (verify.PROMPT_VER) — a stale one
    would silently baseline the ablation against a retired prompt. Seeded in memory only, never
    appended to the jsonl: the A/B cache stays a record of what this script actually ran.
    Anything missing is simply left uncached, so run_arm verifies it normally."""
    db = sqlite3.connect(config.VERIFY)
    have = {(lid, qid): (v, reason) for lid, qid, v, reason in db.execute(
        "SELECT target_lid, anchor_qid, verdict, reason FROM verdict WHERE prompt_ver = ?",
        [verify.PROMPT_VER])}
    db.close()
    seeded = 0
    for r in rows:
        if (r[0], r[3]) in have:
            cache[("prod", 0, r[0], r[3])] = have[(r[0], r[3])]
            seeded += 1
    return seeded


def run_arm(name, kwargs, rows, gmap, model, samples, cache, il_map, batch=8):
    kwargs = dict(kwargs)
    if kwargs.pop("nogloss", False):
        gmap = {}   # senseless simulation: no sense means no gloss in the prompt AND none for
                    # the demonym nudge below — both go blind together, as they do in that lane
    votes = collections.defaultdict(collections.Counter)
    cf = open(CACHE, "a")
    try:
        for s in range(samples):
            # Batch only the UNCACHED pairs. Batching the raw row list and replaying a chunk
            # only when ALL its pairs were cached meant one uncached pair re-paid the LLM for
            # its 7 cached neighbours — and any eval-subset change shifted chunk boundaries,
            # turning a mostly-cached run into a mostly-full-price one.
            uncached = [r for r in rows if (name, s, r[0], r[3]) not in cache]
            for i in range(0, len(uncached), batch):
                chunk = uncached[i:i + batch]
                # +multi batches are bigger and run slower; give generous headroom and
                # RETRY a slow batch rather than aborting the whole run on one transient
                # slow response. Only a real quota marker aborts.
                vs = None
                for _attempt in range(3):
                    try:
                        vs = verify.run_batch(chunk, model, gmap, il_map=il_map,
                                              timeout=240, **kwargs)
                        break
                    except subprocess.TimeoutExpired:
                        continue   # slow/transient — retry the batch
                    except RuntimeError as e:
                        if any(m in str(e).lower() for m in QUOTA_MARKERS):
                            raise QuotaAbort(str(e)[:200]) from e
                        break      # non-quota error — give up on this batch
                if vs is None:
                    vs = [{"verdict": "error", "reason": ""}] * len(chunk)  # mark + continue
                for r, v in zip(chunk, vs):
                    verdict, reason = v.get("verdict"), v.get("reason", "")
                    cache[(name, s, r[0], r[3])] = (verdict, reason)
                    cf.write(json.dumps({"arm": name, "sample": s, "lid": r[0],
                                         "qid": r[3], "verdict": verdict, "reason": reason}) + "\n")
                cf.flush()
            for r in rows:
                verdict, reason = cache[(name, s, r[0], r[3])]
                gloss = verify.pick_gloss(gmap, r[0], r[1])
                adesc = rq.anchor_desc(*r[8:11])  # de/en/sv anchor descs
                votes[(r[0], r[3])][predicted_action(verdict, reason, gloss, adesc)] += 1
    finally:
        cf.close()
    return {k: c.most_common(1)[0][0] for k, c in votes.items()}


def score(pred, truth):
    n = exact = binary = false_keep = false_drop = 0
    for k, p in pred.items():
        t = truth[k]
        n += 1
        exact += (p == t)
        pk, tk = p == "keep", t == "keep"
        binary += (pk == tk)
        false_keep += (pk and not tk)   # dangerous: would surface/write a bad link
        false_drop += (tk and not pk)   # over-conservative: would drop a good link
    return dict(n=n, exact=exact / n, binary=binary / n, fk=false_keep, fd=false_drop)


def flip_report(pa, pb, truth, names, a, b):
    """Every pair where arm `a` and arm `b` chose different actions, scored against the human.

    'toward human' / 'away from human' are directional a -> b: for base -> +multi that reads as
    the format helping or hurting; for +nudge -> -gloss it reads as what the gloss was holding up.
    """
    flips = [(k, pa[k], pb[k], truth[k]) for k in pa if k in pb and pa[k] != pb[k]]
    fixed = sum(1 for _, x, y, t in flips if y == t and x != t)
    broke = sum(1 for _, x, y, t in flips if x == t and y != t)
    print(f"\n{a} -> {b}: {len(flips)} flips  ({fixed} toward human, {broke} away from human)")
    for k, x, y, t in flips[:30]:
        mark = "OK " if (y == t and x != t) else ("BAD" if (x == t and y != t) else " . ")
        print(f"  {mark} {names.get(k, k[0])} {k[1]}: {x} -> {y}  (human={t})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="cap eval pairs (0 = all)")
    ap.add_argument("--count", action="store_true", help="print job size and exit")
    ap.add_argument("--suspect", action="store_true",
                    help="restrict eval set to scope/coherence cases (BROADER/NARROWER/"
                         "LATERAL/INCOHERENT) — where the concept format can actually matter")
    ap.add_argument("--gold", action="store_true",
                    help="restrict eval set to the curated gold pairs "
                         "(notes/problematic_cases.csv) — the cheap retest set")
    ap.add_argument("--arms", default="", help="comma-list of arm names to run "
                    f"(default {','.join(DEFAULT_ARMS)}); e.g. 'base' to score base alone, or "
                    "'prod,-gloss' for the senseless-lane ablation")
    args = ap.parse_args()
    sel = args.arms.split(",") if args.arms else list(DEFAULT_ARMS)
    arms = [(n, ARMS[n]) for n in sel if n in ARMS]
    if "prod" in sel and args.samples != 1:
        ap.error("--arms prod requires --samples 1: it replays ONE cached production verdict per "
                 "pair, so more samples would just fake a unanimous vote rather than resample.")

    rows, truth = load_eval_rows(suspect=args.suspect)
    if args.gold:
        gold_pairs = {(r["lid"], r["qid"])
                      for r in csv.DictReader(open(config.GOLD))}
        rows = [r for r in rows if (r[0], r[3]) in gold_pairs]
    if args.limit:
        rows = rows[: args.limit]
    th = collections.Counter(truth[(r[0], r[3])] for r in rows)
    il_map = (verify.load_item_lang({r[3] for r in rows})
              if any(kw.get("langs") for _, kw in arms) else {})
    cache = load_cache()
    if "prod" in sel:      # before the count, so --count prices the ablation honestly
        print(f"prod arm seeded from the live verify cache: {seed_prod_arm(cache, rows)}/{len(rows)}")
    total = len(rows) * args.samples * len(arms)
    cached = sum((name, s, r[0], r[3]) in cache
                 for name, _ in arms for s in range(args.samples) for r in rows)
    print(f"eval pairs: {len(rows)}  human truth: {dict(th)}  "
          f"item_lang coverage: {sum(1 for r in rows if r[3] in il_map)}/{len(rows)}")
    print(f"model={args.model} samples={args.samples} arms={[a[0] for a in arms]} "
          f"-> {total} pair-verdicts  ({cached} cached, {total - cached} to run)")
    if args.count:
        return

    gmap = verify.load_glosses({r[0] for r in rows})
    preds = {}
    try:
        print(f"\n{'arm':<16}{'exact':>8}{'binary':>8}{'false-keep':>12}{'false-drop':>12}")
        for name, kw in arms:
            preds[name] = run_arm(name, kw, rows, gmap, args.model, args.samples, cache, il_map)
            sc = score(preds[name], truth)
            print(f"{name:<16}{sc['exact']:>7.0%}{sc['binary']:>8.0%}{sc['fk']:>12}{sc['fd']:>12}")
    except QuotaAbort as e:
        print(f"\n!! quota/rate-limit hit, aborting: {e}")
        print(f"   progress is cached in {CACHE} — re-run the SAME command to resume.")
        return

    # flip analysis, pairwise: the concept-format A/B, and the senseless ablation
    names = {(r[0], r[3]): f"{r[1]}:{r[2]}" for r in rows}
    for a, b in (("base", "+multi"), ("prod", "-gloss"), ("+nudge", "-gloss")):
        if a in preds and b in preds:
            flip_report(preds[a], preds[b], truth, names, a, b)


if __name__ == "__main__":
    main()
