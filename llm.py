#!/usr/bin/env python3
"""Shared runner for the batched ``claude -p`` JSON calls (CC0 inputs only).

Each verifier/tagger builds a numbered prompt ("Items:\\n1. ...\\n2. ...") and a SYSTEM
prompt asking for a JSON array reply ``[{"i":1, ...}, ...]``. This runs the local CLI in
print mode (reusing existing auth, no API key), strips an optional ``` code fence, parses
the array, and returns it reindexed to 1..n — filling a copy of ``default`` for any item the
model omitted. Raises RuntimeError on a non-zero exit or an unlabeled reply so a caller can
treat a failed batch as transient (log it, mark the items as errors, continue) rather than
crash the whole run.
"""
import json
import subprocess

# Running tally of `claude -p` usage across batch_json calls in this process. Every JSON response
# carries `usage` + `total_cost_usd`; we used to parse `result` and discard the rest. Accumulating
# it here (a fresh process starts at zero) lets a caller print real token/cost numbers without
# changing batch_json's return type. Read via usage_summary(); reset_usage() to zero between phases.
_TOK_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_usage = {"calls": 0, "cost_usd": 0.0, **{k: 0 for k in _TOK_FIELDS}}


def reset_usage():
    _usage.update({"calls": 0, "cost_usd": 0.0, **{k: 0 for k in _TOK_FIELDS}})


def usage_summary():
    """A copy of the accumulated `claude -p` usage (calls, per-kind tokens, total cost_usd)."""
    return dict(_usage)


def batch_json(system, prompt, model, n, default, timeout=300):
    out = subprocess.run(
        # --disallowedTools "*" drops the ~17.6k-token tool-schema context from every call
        # (this is a pure text->JSON task that uses no tools); measured ~98% less context/call.
        ["claude", "-p", "--output-format", "json", "--model", model, "--system-prompt", system,
         "--disallowedTools", "*"],
        input=prompt, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:300])
    resp = json.loads(out.stdout)
    usage = resp.get("usage") or {}          # token/cost telemetry the CLI returns per call
    _usage["calls"] += 1
    _usage["cost_usd"] += resp.get("total_cost_usd") or 0.0
    for k in _TOK_FIELDS:
        _usage[k] += usage.get(k, 0)
    result = resp["result"].strip()
    if result.startswith("```"):
        result = result.split("\n", 1)[1].rsplit("```", 1)[0]
    parsed = json.loads(result)
    # Reindex by the model's own "i" labels (tolerating "3" for 3), NEVER by position — a
    # dropped/merged item would silently shift later verdicts onto the wrong pairs. A reply
    # with no usable labels raises: callers already treat a raised batch as transient
    # (before, all n items were silently recorded as parse_error verdicts).
    by_i = {}
    for v in parsed:
        try:
            by_i[int(v.get("i"))] = v
        except (TypeError, ValueError):
            pass
    if parsed and not by_i:
        raise RuntimeError("model reply has no usable 'i' indices")
    return [by_i.get(i, dict(default)) for i in range(1, n + 1)]
