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
}
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
    """Collapse a verifier verdict+relation to the action it implies, via scope_hint —
    so the metric scores the decision-relevant output, not the raw token. gloss + anchor_desc
    enable scope_hint's demonym→ethnic-group nudge."""
    if verdict == "no":
        return "reject"
    if verdict in ("uncertain", "error", "parse_error"):
        return "defer"
    _rel, sug, _ = rq.scope_hint(reason, gloss, anchor_desc)
    if sug.startswith("reject"):
        return "reject"
    if sug.startswith("defer"):
        return "defer"
    return "keep"   # EQUAL / convention-keep / unflagged


def scope_rel_map():
    """{(lid, qid): REL token} from the production verify cache — the verifier's relation
    for each pair, used to restrict the eval set to scope/coherence cases."""
    db = sqlite3.connect(config.VERIFY)
    return {(lid, qid): rq.scope_hint(reason)[0]
            for lid, qid, reason in db.execute(
                "SELECT target_lid, anchor_qid, reason FROM verdict")}


def load_eval_rows(suspect=False):
    truth = {k: HUMAN[(d.get("status") or "").strip()]
             for k, d in rq.load_decisions().items()
             if (d.get("status") or "").strip() in HUMAN}
    args = argparse.Namespace(gate="ABD", min_support=1, senseless_only=False)
    rows = [r for r in verify.candidates(args)
            if verify.has_evidence(r) and (r[0], r[3]) in truth]
    if suspect:
        rel = scope_rel_map()
        rows = [r for r in rows if rel.get((r[0], r[3])) in SCOPE_RELS]
    return rows, truth


def load_cache():
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE) as f:
            for line in f:
                e = json.loads(line)
                cache[(e["arm"], e["sample"], e["lid"], e["qid"])] = (e["verdict"], e["reason"])
    return cache


def run_arm(name, kwargs, rows, gmap, model, samples, cache, il_map, batch=8):
    votes = collections.defaultdict(collections.Counter)
    cf = open(CACHE, "a")
    try:
        for s in range(samples):
            for i in range(0, len(rows), batch):
                chunk = rows[i:i + batch]
                keys = [(name, s, r[0], r[3]) for r in chunk]
                if all(k in cache for k in keys):
                    results = [cache[k] for k in keys]
                else:
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
                    results = [(v.get("verdict"), v.get("reason", "")) for v in vs]
                    for k, (verdict, reason) in zip(keys, results):
                        cache[k] = (verdict, reason)
                        cf.write(json.dumps({"arm": name, "sample": s, "lid": k[2],
                                             "qid": k[3], "verdict": verdict, "reason": reason}) + "\n")
                    cf.flush()
                for r, (verdict, reason) in zip(chunk, results):
                    gloss = verify.pick_gloss(gmap, r[0], r[1])
                    anchor_desc = " | ".join(d for d in r[8:11] if d)  # de/en/sv anchor descs
                    votes[(r[0], r[3])][predicted_action(verdict, reason, gloss, anchor_desc)] += 1
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
                    "(default base,+multi); e.g. 'base' to score base alone")
    args = ap.parse_args()
    sel = args.arms.split(",") if args.arms else list(ARMS)
    arms = [(n, ARMS[n]) for n in sel if n in ARMS]

    rows, truth = load_eval_rows(suspect=args.suspect)
    if args.gold:
        gold_pairs = {(r["lid"], r["qid"])
                      for r in csv.DictReader(open("notes/problematic_cases.csv"))}
        rows = [r for r in rows if (r[0], r[3]) in gold_pairs]
    if args.limit:
        rows = rows[: args.limit]
    th = collections.Counter(truth[(r[0], r[3])] for r in rows)
    il_map = (verify.load_item_lang({r[3] for r in rows})
              if any(kw.get("langs") for _, kw in arms) else {})
    cache = load_cache()
    cached_batches = sum(
        all((name, s, r[0], r[3]) in cache for r in rows[i:i + 8])
        for name, _ in arms for s in range(args.samples)
        for i in range(0, len(rows), 8))
    nbatches = (-(-len(rows) // 8)) * args.samples * len(arms)
    print(f"eval pairs: {len(rows)}  human truth: {dict(th)}  "
          f"item_lang coverage: {sum(1 for r in rows if r[3] in il_map)}/{len(rows)}")
    print(f"model={args.model} samples={args.samples} arms={[a[0] for a in arms]} "
          f"-> {nbatches} batches  ({cached_batches} cached, {nbatches - cached_batches} to run)")
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

    # flip analysis: base vs +multi
    if "base" in preds and "+multi" in preds:
        base, w = preds["base"], preds["+multi"]
        flips = [(k, base[k], w[k], truth[k]) for k in base if base[k] != w[k]]
        fixed = sum(1 for _, b, l, t in flips if l == t and b != t)
        broke = sum(1 for _, b, l, t in flips if b == t and l != t)
        print(f"\nbase -> +multi: {len(flips)} flips  ({fixed} fixed toward human, {broke} broke away)")
        for (lid, qid), b, l, t in flips[:30]:
            mark = "OK " if (l == t and b != t) else ("BAD" if (b == t and l != t) else " . ")
            print(f"  {mark} {lid} {qid}: {b} -> {l}  (human={t})")


if __name__ == "__main__":
    main()
