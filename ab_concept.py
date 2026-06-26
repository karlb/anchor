#!/usr/bin/env python3
"""A/B the anchor-concept format against the human decision log — model PINNED.

Question: does widening the concept shown to the verifier to ALL CC0 languages
(labels first, then +descriptions) make it agree more with human reviewers? Ground
truth = decisions.csv (approved/written = keep, rejected = reject, deferred = defer).

Every arm uses the SAME model (default sonnet) so the only varying factor is the
concept format — mixing models would swamp the format effect (haiku vs sonnet is a
~90-vs-5 precision gap). Each pair is verified `--samples` times per arm and the
predicted action is majority-voted to damp LLM nondeterminism.

    uv run ab_concept.py --limit 60 --samples 1     # LEAN first read (~30k tokens)
    uv run ab_concept.py --samples 3                 # full run (pricey — ~430k tokens)
    uv run ab_concept.py --count                      # just size the job

Resumable: every completed batch is cached to data/ab_concept_cache.jsonl keyed by
(arm, sample, lid, qid), so a kill / quota stall loses nothing — re-run the SAME
command and it pays only for batches not yet cached. On a quota / rate-limit error it
ABORTS cleanly (rather than scoring the failed batches as fake 'defer's) so partial
results stay trustworthy; just re-run when quota recovers. Delete the cache file to
force a clean run.
"""
import argparse
import collections
import json
import os
import sqlite3
import subprocess

import config
import llm
import review_queue as rq
import verify

# "+alllangs" test arm: widen the concept beyond the 3 editing languages to a major-
# language set (de/en/sv already + these). NOT all ~73 langs Wikidata has per item —
# that drowns the prompt in redundant translations. Reads the throwaway probe DB built
# by scratch_fetch_alllangs.py; only covers the suspect-set anchors.
LANGS_WIDE = ("en", "de", "sv", "fr", "es", "it", "nl", "pl", "ru", "pt")
WIDE_DB = os.path.join(config.DATA, "ab_alllangs.sqlite3")

# "+alllangs-guided" arm: the SAME 10-language concept input as "+alllangs", but the
# prompt is told the extra language fields are translations of ONE concept (read together,
# not as independent claims). Tests whether plain +alllangs lost to +descs(3) only because
# the base prompt — written for 3 curated langs — mis-reads many raw translations as scope
# evidence / spurious overlap (the over-equation false-keeps), rather than because more
# languages are inherently unhelpful.
MULTILINGUAL_GUIDE = (
    "MULTILINGUAL CONCEPT FIELDS: the concept's label and description may be shown in several "
    "language-tagged fields separated by ' | ' or ' / '. These are translations of ONE concept "
    "— read them together as a single meaning, not as independent claims. Do NOT treat a terser, "
    "looser, or differently-phrased translation as evidence of broader/narrower scope; weigh scope "
    "only from the clearest, most specific description. Flag INCOHERENT only when translations name "
    "genuinely DIFFERENT referents (e.g. a debt vs. a lawsuit), never for mere variation in detail "
    "or completeness across languages. "
)
GUIDED_SYSTEM = verify.SYSTEM.replace(
    "Reply with ONLY a JSON array", MULTILINGUAL_GUIDE + "Reply with ONLY a JSON array", 1)

ARMS = [("base", False, False), ("+labels", True, False), ("+labels+descs", True, True)]
HUMAN = {"approved": "keep", "written": "keep", "rejected": "reject", "deferred": "defer"}
# The scope/coherence relations: the cases where the concept boundary is contested and
# multilingual context could plausibly help. Muddled anchors surface here too
# (Computerhersteller -> NARROWER, anspraak -> BROADER). --suspect restricts the eval set
# to these, so the format effect is measured where it can actually appear (not drowned by
# EQUAL-obvious keeps and clean homograph rejects).
SCOPE_RELS = ("BROADER", "NARROWER", "LATERAL", "INCOHERENT")
CACHE = os.path.join(config.DATA, "ab_concept_cache.jsonl")
QUOTA_MARKERS = ("quota", "rate limit", "rate-limit", "usage limit", "overloaded",
                 "429", "limit reached", "too many requests")


class QuotaAbort(RuntimeError):
    """Raised to stop the whole run on a quota/rate-limit failure (vs a transient batch error)."""


def predicted_action(verdict, reason):
    """Collapse a verifier verdict+relation to the action it implies, via scope_hint —
    so the metric scores the decision-relevant output, not the raw token."""
    if verdict == "no":
        return "reject"
    if verdict in ("uncertain", "error", "parse_error"):
        return "defer"
    _rel, sug, _ = rq.scope_hint(reason)
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


def load_wide():
    """{qid: {lang: (label, desc)}} for LANGS_WIDE, from the all-languages probe DB."""
    out = {}
    if not os.path.exists(WIDE_DB):
        return out
    db = sqlite3.connect(WIDE_DB)
    for qid, lang, label, desc in db.execute("SELECT qid, lang, label, desc FROM item_label_all"):
        if lang in LANGS_WIDE:
            out.setdefault(qid, {})[lang] = (label, desc)
    return out


def fmt_concept_wide(row, wide):
    """Concept string from the major-language set (target-lang first), mirroring
    fmt_concept's descs_all format but across more languages."""
    qid, lang = row[3], row[1]
    ent = wide.get(qid, {})
    order = [c for c in dict.fromkeys((lang, *LANGS_WIDE)) if c in ent]
    labs = [ent[c][0] for c in order if ent[c][0]]
    descs = [f"{c}: {ent[c][1]}" for c in order if ent[c][1]]
    lab = " / ".join(dict.fromkeys(labs)) or "(no label)"
    return qid, lab, " | ".join(descs) or "(no description)"


def run_batch_wide(rows, model, gmap, wide, timeout=150, system=None):
    lines = []
    for n, row in enumerate(rows, 1):
        lid, lang, lemma = row[0], row[1], row[2]
        qid, lab, desc = fmt_concept_wide(row, wide)
        gloss = verify.pick_gloss(gmap, lid, lang)
        gpart = f' gloss="{gloss}"' if gloss else ""
        lines.append(f'{n}. language={verify.LANGNAME.get(lang, lang)} noun="{lemma}"{gpart} | '
                     f'concept {qid}: label="{lab}" description="{desc}"')
    return llm.batch_json(system or verify.SYSTEM, "Items:\n" + "\n".join(lines), model, len(rows),
                          {"verdict": "parse_error", "reason": ""}, timeout=timeout)


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


def run_arm(name, rows, gmap, model, labels_all, descs_all, samples, cache, wide=None, batch=8):
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
                    # wide (+alllangs) batches are bigger and run ~50s; give generous
                    # headroom and RETRY a slow batch rather than aborting the whole run
                    # on one transient slow response. Only a real quota marker aborts.
                    vs = None
                    for _attempt in range(3):
                        try:
                            if name == "+alllangs":
                                vs = run_batch_wide(chunk, model, gmap, wide, timeout=240)
                            elif name == "+alllangs-guided":
                                vs = run_batch_wide(chunk, model, gmap, wide, timeout=240,
                                                    system=GUIDED_SYSTEM)
                            else:
                                vs = verify.run_batch(chunk, model, gmap, labels_all, descs_all, timeout=240)
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
                    votes[(r[0], r[3])][predicted_action(verdict, reason)] += 1
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
    ap.add_argument("--arms", default="", help="comma-list of arm names to run "
                    "(default all); e.g. 'base,+labels+descs' to skip the inert +labels arm")
    args = ap.parse_args()
    all_arms = ARMS + [("+alllangs", None, None), ("+alllangs-guided", None, None)]
    sel = args.arms.split(",") if args.arms else [a[0] for a in ARMS]
    arms = [a for a in all_arms if a[0] in sel]
    wide = load_wide() if any(a[0] in ("+alllangs", "+alllangs-guided") for a in arms) else None

    rows, truth = load_eval_rows(suspect=args.suspect)
    if args.limit:
        rows = rows[: args.limit]
    th = collections.Counter(truth[(r[0], r[3])] for r in rows)
    cache = load_cache()
    cached_batches = sum(
        all((name, s, r[0], r[3]) in cache for r in rows[i:i + 8])
        for name, _, _ in arms for s in range(args.samples)
        for i in range(0, len(rows), 8))
    nbatches = (-(-len(rows) // 8)) * args.samples * len(arms)
    print(f"eval pairs: {len(rows)}  human truth: {dict(th)}")
    print(f"model={args.model} samples={args.samples} arms={[a[0] for a in arms]} "
          f"-> {nbatches} batches  ({cached_batches} cached, {nbatches - cached_batches} to run)")
    if args.count:
        return

    gmap = verify.load_glosses({r[0] for r in rows})
    preds = {}
    try:
        print(f"\n{'arm':<16}{'exact':>8}{'binary':>8}{'false-keep':>12}{'false-drop':>12}")
        for name, la, da in arms:
            preds[name] = run_arm(name, rows, gmap, args.model, la, da, args.samples, cache, wide)
            sc = score(preds[name], truth)
            print(f"{name:<16}{sc['exact']:>7.0%}{sc['binary']:>8.0%}{sc['fk']:>12}{sc['fd']:>12}")
    except QuotaAbort as e:
        print(f"\n!! quota/rate-limit hit, aborting: {e}")
        print(f"   progress is cached in {CACHE} — re-run the SAME command to resume.")
        return

    # flip analysis: base vs the widest arm that was actually run
    widest = next((n for n in ("+alllangs-guided", "+alllangs", "+labels+descs", "+labels") if n in preds), None)
    if "base" in preds and widest:
        base, w = preds["base"], preds[widest]
        flips = [(k, base[k], w[k], truth[k]) for k in base if base[k] != w[k]]
        fixed = sum(1 for _, b, l, t in flips if l == t and b != t)
        broke = sum(1 for _, b, l, t in flips if b == t and l != t)
        print(f"\nbase -> {widest}: {len(flips)} flips  ({fixed} fixed toward human, {broke} broke away)")
        for (lid, qid), b, l, t in flips[:30]:
            mark = "OK " if (l == t and b != t) else ("BAD" if (b == t and l != t) else " . ")
            print(f"  {mark} {lid} {qid}: {b} -> {l}  (human={t})")


if __name__ == "__main__":
    main()
