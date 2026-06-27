#!/usr/bin/env python3
"""Shared runner for the batched ``claude -p`` JSON calls (CC0 inputs only).

Each verifier/tagger builds a numbered prompt ("Items:\\n1. ...\\n2. ...") and a SYSTEM
prompt asking for a JSON array reply ``[{"i":1, ...}, ...]``. This runs the local CLI in
print mode (reusing existing auth, no API key), strips an optional ``` code fence, parses
the array, and returns it reindexed to 1..n — filling a copy of ``default`` for any item the
model omitted. Raises RuntimeError on a non-zero exit so a caller can treat a failed batch
as transient (log it, mark the items as errors, continue) rather than crash the whole run.
"""
import json
import subprocess


def batch_json(system, prompt, model, n, default, timeout=300):
    out = subprocess.run(
        # --disallowedTools "*" drops the ~17.6k-token tool-schema context from every call
        # (this is a pure text->JSON task that uses no tools); measured ~98% less context/call.
        ["claude", "-p", "--output-format", "json", "--model", model, "--system-prompt", system,
         "--disallowedTools", "*"],
        input=prompt, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:300])
    result = json.loads(out.stdout)["result"].strip()
    if result.startswith("```"):
        result = result.split("\n", 1)[1].rsplit("```", 1)[0]
    by_i = {v.get("i"): v for v in json.loads(result)}
    return [by_i.get(i, dict(default)) for i in range(1, n + 1)]
