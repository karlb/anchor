#!/usr/bin/env python3
"""A/B the anchor-concept format against the human decision log — model PINNED.

Question: does widening the concept shown to the verifier to ALL CC0 languages
(labels first, then +descriptions) make it agree more with human reviewers? Ground
truth = decisions.csv (approved/written = keep, rejected = reject, deferred = defer).

Every arm uses the SAME model (default sonnet) so the only varying factor is the
concept format — mixing models would swamp the format effect (haiku vs sonnet is a
~90-vs-5 precision gap). Each pair is verified `--samples` times per arm and the
predicted action is majority-voted to damp LLM nondeterminism.

    uv run ab_concept.py --model sonnet --samples 3        # full run
    uv run ab_concept.py --count                            # just size the job

Arms: base (target+en label, 1 desc) / +labels (all langs) / +labels+descs (all langs).
"""
import argparse
import collections

import config  # noqa: F401  (verify imports it; kept explicit for clarity)
import review_queue as rq
import verify

ARMS = [("base", False, False), ("+labels", True, False), ("+labels+descs", True, True)]
HUMAN = {"approved": "keep", "written": "keep", "rejected": "reject", "deferred": "defer"}


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


def load_eval_rows():
    truth = {k: HUMAN[(d.get("status") or "").strip()]
             for k, d in rq.load_decisions().items()
             if (d.get("status") or "").strip() in HUMAN}
    args = argparse.Namespace(gate="ABD", min_support=1, senseless_only=False)
    rows = [r for r in verify.candidates(args)
            if verify.has_evidence(r) and (r[0], r[3]) in truth]
    return rows, truth


def run_arm(rows, gmap, model, labels_all, descs_all, samples, batch=8):
    votes = collections.defaultdict(collections.Counter)
    for _ in range(samples):
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            try:
                vs = verify.run_batch(chunk, model, gmap, labels_all, descs_all)
            except Exception:  # noqa: BLE001 — a failed batch shouldn't kill the arm
                vs = [{"verdict": "error", "reason": ""}] * len(chunk)
            for row, v in zip(chunk, vs):
                votes[(row[0], row[3])][predicted_action(v.get("verdict"), v.get("reason", ""))] += 1
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
    args = ap.parse_args()

    rows, truth = load_eval_rows()
    if args.limit:
        rows = rows[: args.limit]
    th = collections.Counter(truth[(r[0], r[3])] for r in rows)
    nbatches = (-(-len(rows) // 8)) * args.samples * len(ARMS)
    print(f"eval pairs: {len(rows)}  human truth: {dict(th)}")
    print(f"model={args.model} samples={args.samples} arms={len(ARMS)} -> ~{nbatches} batches")
    if args.count:
        return

    gmap = verify.load_glosses({r[0] for r in rows})
    preds = {}
    print(f"\n{'arm':<16}{'exact':>8}{'binary':>8}{'false-keep':>12}{'false-drop':>12}")
    for name, la, da in ARMS:
        preds[name] = run_arm(rows, gmap, args.model, la, da, args.samples)
        sc = score(preds[name], truth)
        print(f"{name:<16}{sc['exact']:>7.0%}{sc['binary']:>8.0%}{sc['fk']:>12}{sc['fd']:>12}")

    base, lab = preds["base"], preds["+labels"]
    flips = [(k, base[k], lab[k], truth[k]) for k in base if base[k] != lab[k]]
    fixed = sum(1 for _, b, l, t in flips if l == t and b != t)
    broke = sum(1 for _, b, l, t in flips if b == t and l != t)
    print(f"\nbase -> +labels: {len(flips)} flips  ({fixed} fixed toward human, {broke} broke away)")
    for (lid, qid), b, l, t in flips[:30]:
        mark = "OK " if (l == t and b != t) else ("BAD" if (b == t and l != t) else " . ")
        print(f"  {mark} {lid} {qid}: {b} -> {l}  (human={t})")


if __name__ == "__main__":
    main()
