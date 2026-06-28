#!/usr/bin/env python3
"""Cheap A/B: does adding the concept's "also known as" (Wikidata aliases) to the
verifier prompt help or hurt, measured on the locked gold (notes/problematic_cases.csv)?

ISOLATED PROTOTYPE — touches no production file. Both arms use the CURRENT production
concept format (the +multi multilingual label/description, verify.fmt_concept with
CONCEPT_LANGS_VERIFY); the ONLY difference is that `+aka` appends an aka="form; form; ..."
field built from the concept's CC0 aliases AND adds an AKA clause to the system prompt
that subordinates aliases to the description (so it does not undo the "description over
label" nudge). `noaka` is the clean control (identical, minus the aliases). Aliases are
CC0 (Wikidata's own altLabels).

Aliases are fetched (props=aliases, ALL languages) ONLY for the gold anchors and cached to
data/aka_cache.json, so a re-run pays no network. The LLM A/B then scores each arm's
majority-voted action (tie -> defer) against the gold's acceptable-output set, reporting the
same metrics as score_approaches.py — watching false-keep, the alias risk.

    uv run aka_probe.py --count            # size the job, no LLM / no network
    uv run aka_probe.py --samples 1        # lean first read (needs network + claude CLI)
    uv run aka_probe.py --samples 3        # firmer vote (pricier)

Resumable: every completed batch's verdict is appended to data/aka_probe_cache.jsonl keyed by
(arm, sample, lid, qid), so a kill/quota-stall loses nothing — re-run the SAME command and it
pays only for batches not yet cached. Delete that file to force a clean run.

Needs network for the one-time alias fetch (www.wikidata.org) and for the claude CLI.
"""
import argparse
import collections
import csv
import json
import os
import re
import time

import ab_concept as ab
import config
import fetch_items as fi
import llm
import verify

AKA_CACHE = os.path.join(config.DATA, "aka_cache.json")          # fetched aliases (no LLM)
PROBE_CACHE = os.path.join(config.DATA, "aka_probe_cache.jsonl")  # per-batch LLM verdicts
ALIAS_CAP = 12   # max distinct alias forms shown per concept (bound tokens + noise)

# Clause appended to verify.SYSTEM for the +aka arm. Aliases are MORE label-like surface
# forms, so they push toward surface-match acceptance — the false-keep trap the "description
# over label" nudge guards against. This clause keeps them strictly below the description.
AKA_GUIDE = (
    " ALSO-KNOWN-AS: an item may show an aka=\"...\" field listing the concept's alternative "
    "names (Wikidata aliases). Treat these as additional surface forms of the SAME concept — "
    "useful to RECOGNISE a synonym the label omits — but they are NOT authoritative about "
    "scope or kind. A noun that matches only an alias is still DIFFERENT (or LATERAL) when the "
    "DESCRIPTION shows a different referent: description-over-label applies to aliases too."
)
SYSTEM_AKA = verify.SYSTEM + AKA_GUIDE


def load_gold():
    return {(r["lid"], r["qid"]): r for r in csv.DictReader(open("notes/problematic_cases.csv"))}


def acceptable(r):
    """The gold case's ACCEPTABLE-OUTPUT set (parsed from {..} in the rationale)."""
    m = re.search(r"\{([^}]*)\}", r["rationale"])
    return {x.strip() for x in m.group(1).split(",")} if m else {r["proposed_correct"]}


def resolve(counter):
    top = counter.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return "defer"          # split verifier -> human review (matches score_approaches)
    return top[0][0]


def gold_rows(gold):
    """Production ABD candidates that are in the gold set and have concept evidence."""
    args = argparse.Namespace(gate="ABD", min_support=1, senseless_only=False)
    return [r for r in verify.candidates(args)
            if verify.has_evidence(r) and (r[0], r[3]) in gold]


# ---- aliases ---------------------------------------------------------------
def load_aka_cache():
    if os.path.exists(AKA_CACHE):
        with open(AKA_CACHE) as f:
            return json.load(f)
    return {}


def fetch_aliases(qids):
    """{qid: {lang: [alias, ...]}} for the given anchors, fetching only the uncached ones."""
    cache = load_aka_cache()
    todo = [q for q in qids if q not in cache]
    for i in range(0, len(todo), fi.BATCH):
        batch = todo[i:i + fi.BATCH]
        data = fi.http_get({"action": "wbgetentities", "ids": "|".join(batch),
                            "props": "aliases", "format": "json", "maxlag": 5})
        ents = data.get("entities", {})
        for q in batch:
            al = ents.get(q, {}).get("aliases", {}) or {}
            cache[q] = {lg: [a["value"] for a in lst if a.get("value")] for lg, lst in al.items()}
        with open(AKA_CACHE, "w") as f:
            json.dump(cache, f)
        print(f"  fetched aliases {min(i + fi.BATCH, len(todo)):,}/{len(todo):,}")
        time.sleep(fi.SLEEP)
    return cache


def render_aka(alias_by_lang, target):
    """A deduped 'form; form; ...' of the concept's aliases, target-language first, capped.

    Mirrors the +multi label ordering (config.CONCEPT_LANGS_VERIFY, target moved to front,
    nb/no/nn collapsed) so aliases come from the same languages the label/description do."""
    if not alias_by_lang:
        return ""
    spec = sorted(config.CONCEPT_LANGS_VERIFY, key=lambda e: e[0] != target)
    forms = []
    for _disp, srcs in spec:
        for s in srcs:
            got = alias_by_lang.get(s)
            if got:
                for fm in got:
                    if fm and fm not in forms:
                        forms.append(fm)
                break          # first present source code wins (collapse nb/no/nn)
    return "; ".join(forms[:ALIAS_CAP])


# ---- arms ------------------------------------------------------------------
def build_lines(rows, gmap, il_map, aka_map):
    """Per-arm prompt lines. aka_map=None -> the noaka control (verify's production line)."""
    lines = []
    for n, row in enumerate(rows, 1):
        lid, lang, lemma = row[0], row[1], row[2]
        qid, lab, desc = verify.fmt_concept(row, langs=config.CONCEPT_LANGS_VERIFY, il_map=il_map)
        gloss = verify.pick_gloss(gmap, lid, lang)
        gpart = f' gloss="{gloss}"' if gloss else ""
        apart = ""
        if aka_map is not None:
            aka = render_aka(aka_map.get(qid, {}), lang)
            apart = f' aka="{aka}"' if aka else ""
        lines.append(f'{n}. language={verify.LANGNAME.get(lang, lang)} noun="{lemma}"{gpart} | '
                     f'concept {qid}: label="{lab}" description="{desc}"{apart}')
    return lines


def load_probe_cache():
    """{(arm, sample, lid, qid): (verdict, reason)} of LLM verdicts already produced."""
    cache = {}
    if os.path.exists(PROBE_CACHE):
        with open(PROBE_CACHE) as f:
            for line in f:
                e = json.loads(line)
                cache[(e["arm"], e["sample"], e["lid"], e["qid"])] = (e["verdict"], e["reason"])
    return cache


def ensure_verdicts(name, rows, gmap, il_map, model, samples, system, aka_map, cache, cf, batch=8):
    """Fill `cache` with this arm's verdicts, running ONLY the batches not already cached
    (resumable: a kill/quota-stall loses nothing — re-run pays just for the missing batches).
    Successful batches are appended to PROBE_CACHE; a failed batch is retried 3x then aborts
    the run with the cache intact (never cached as a fake verdict)."""
    for s in range(samples):
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            keys = [(name, s, r[0], r[3]) for r in chunk]
            if all(k in cache for k in keys):
                continue
            prompt = "Items:\n" + "\n".join(build_lines(chunk, gmap, il_map, aka_map))
            vs = None
            for _attempt in range(3):
                try:
                    vs = llm.batch_json(system, prompt, model, len(chunk),
                                        {"verdict": "error", "reason": ""}, timeout=240)
                    break
                except RuntimeError:
                    continue          # transient (slow/quota/blip) — retry the batch
            if vs is None:
                raise RuntimeError(f"arm {name} batch at row {i} failed 3x — aborting; "
                                   f"cache preserved, re-run to resume")
            for k, v in zip(keys, vs):
                cache[k] = (v.get("verdict", "error"), v.get("reason", ""))
                cf.write(json.dumps({"arm": k[0], "sample": k[1], "lid": k[2], "qid": k[3],
                                     "verdict": cache[k][0], "reason": cache[k][1]}) + "\n")
            cf.flush()


def vote_actions(name, rows, gmap, samples, cache):
    """{(lid,qid): majority-voted action} from the cached verdicts (tie -> defer)."""
    votes = collections.defaultdict(collections.Counter)
    for r in rows:
        gloss = verify.pick_gloss(gmap, r[0], r[1])
        anchor_desc = " | ".join(d for d in r[8:11] if d)
        for s in range(samples):
            v = cache.get((name, s, r[0], r[3]))
            if v:
                votes[(r[0], r[3])][ab.predicted_action(v[0], v[1], gloss, anchor_desc)] += 1
    return {k: resolve(c) for k, c in votes.items() if c}


def score(pred, gold):
    inset = fk = fd = defers = n = 0
    bad = []
    for k, p in pred.items():
        acc = acceptable(gold[k])
        n += 1
        inset += p in acc
        if p == "keep" and "keep" not in acc:
            fk += 1
            bad.append(f"FALSE-KEEP {gold[k]['lang']}:{gold[k]['lemma']} (gold {gold[k]['proposed_correct']})")
        if p == "reject" and "reject" not in acc:
            fd += 1
            bad.append(f"FALSE-DROP {gold[k]['lang']}:{gold[k]['lemma']} (gold {gold[k]['proposed_correct']})")
        defers += (p == "defer")
    return dict(n=n, inset=inset, fk=fk, fd=fd, defers=defers, bad=bad)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--count", action="store_true", help="size the job (no network, no LLM)")
    args = ap.parse_args()

    gold = load_gold()
    rows = gold_rows(gold)
    qids = sorted({r[3] for r in rows})
    nbatches = (-(-len(rows) // 8)) * args.samples * 2     # 2 arms
    print(f"gold pairs: {len(gold)}   scored here (ABD + evidence): {len(rows)}   "
          f"distinct anchors: {len(qids)}")
    print(f"model={args.model} samples={args.samples} -> ~{nbatches} claude -p batches "
          f"(2 arms: noaka, +aka)")
    if args.count:
        return

    print("\nfetching aliases for gold anchors (cached) ...")
    aka_map = fetch_aliases(qids)
    n_with = sum(1 for q in qids if render_aka(aka_map.get(q, {}), "en"))
    print(f"  anchors with at least one alias: {n_with}/{len(qids)}")

    gmap = verify.load_glosses({r[0] for r in rows})
    il_map = verify.load_item_lang(set(qids))
    print(f"  item_lang coverage (the +multi concept): {sum(1 for q in qids if q in il_map)}/{len(qids)}")

    llm.reset_usage()
    arms = [("noaka", verify.SYSTEM, None), ("+aka", SYSTEM_AKA, aka_map)]
    cache = load_probe_cache()
    nb = -(-len(rows) // 8) * args.samples
    cached = sum(all((name, s, r[0], r[3]) in cache for r in rows[i:i + 8])
                 for name, _, _ in arms for s in range(args.samples)
                 for i in range(0, len(rows), 8))
    print(f"  verdict cache: {cached}/{nb * len(arms)} batches done ({nb * len(arms) - cached} to run)")

    results = {}
    cf = open(PROBE_CACHE, "a")
    try:
        for name, system, am in arms:
            ensure_verdicts(name, rows, gmap, il_map, args.model, args.samples, system, am, cache, cf)
    finally:
        cf.close()

    print(f"\n{'arm':<8}{'in-set':>9}{'false-keep':>12}{'false-drop':>12}{'defers':>9}{'covered':>9}")
    for name, _system, _am in arms:
        pred = vote_actions(name, rows, gmap, args.samples, cache)
        results[name] = (pred, score(pred, gold))
        sc = results[name][1]
        rate = f"{sc['inset'] / sc['n']:.0%}" if sc['n'] else "-"
        print(f"{name:<8}{rate:>9}{sc['fk']:>12}{sc['fd']:>12}{sc['defers']:>9}{sc['n']:>6}/{len(rows)}")

    print("\n--- dangerous errors (false-keep / false-drop) ---")
    for name, _, _ in arms:
        bad = results[name][1]["bad"]
        print(f"\n{name}: {len(bad)} dangerous")
        for b in bad:
            print(f"    {b}")

    # flips: where adding aliases changed the action
    noaka, aka = results["noaka"][0], results["+aka"][0]
    flips = [(k, noaka[k], aka[k]) for k in noaka if k in aka and noaka[k] != aka[k]]
    print(f"\nnoaka -> +aka: {len(flips)} action flips")
    for (lid, qid), a, b in flips:
        acc = acceptable(gold[(lid, qid)])
        mark = "OK " if (b in acc and a not in acc) else ("BAD" if (a in acc and b not in acc) else " . ")
        print(f"  {mark} {gold[(lid, qid)]['lang']}:{gold[(lid, qid)]['lemma']}: {a} -> {b}  (acc {sorted(acc)})")

    u = llm.usage_summary()
    print(f"\nclaude -p usage: {u['calls']:,} calls  in {u['input_tokens']:,} "
          f"(+{u['cache_read_input_tokens']:,} cached-read) / out {u['output_tokens']:,} tok  "
          f"~${u['cost_usd']:.2f}")


if __name__ == "__main__":
    main()
