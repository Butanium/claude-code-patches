#!/usr/bin/env python3
"""teammate-cwd: the Agent tool offers `cwd` to the model.

One trivial `claude -p` turn, then the dumped request body is read.
  patched: the Agent tool's input schema has a `cwd` property
  stock:   it doesn't (the full schema defines it but the model-facing one omits it)
Only the schema edit is observed here. The routing half of the patch (a named
call with cwd becomes a pane teammate started in that directory) is not
exercised. Cross-platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import INCONCLUSIVE, PATCHED, STOCK, Verdict, main, tool_schema
from _scenarios import baseline_session


def run(binary: Path) -> Verdict:
    rec = baseline_session(binary)
    agent = next((s for r in rec.requests if (s := tool_schema(r, "Agent"))), None)
    if agent is None:
        return Verdict(INCONCLUSIVE, f"no Agent tool in the dumped requests (rc={rec.returncode})")
    props = (agent.get("input_schema") or {}).get("properties") or {}
    if "cwd" in props:
        return Verdict(PATCHED, "Agent schema offers cwd")
    return Verdict(STOCK, "Agent schema has no cwd")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
