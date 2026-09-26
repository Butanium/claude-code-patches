#!/usr/bin/env python3
"""monitor-persistent: Monitor accepts `persistent: true` and arms a watch with no deadline.

One `claude -p` turn asks the model to call Monitor with persistent=true.
  patched: the tool result says the watch is persistent ("runs until TaskStop or
           session end"); the Monitor schema the model sees has `persistent`
  stock:   the watch is put on a deadline ("expires in ...") and the schema has
           no `persistent` (2.1.271+; older binaries have it natively)
Cross-platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (INCONCLUSIVE, PATCHED, STOCK, Verdict, main, retry_inconclusive,
                      tool_result_text, tool_schema, tool_uses)
from _scenarios import print_session

PROMPT = (
    "Call the Monitor tool exactly once with these arguments: command \"echo hi\", description "
    "\"probe\", persistent true, timeout_ms 60000. If Monitor is not loaded yet, load it first with "
    "ToolSearch (query \"select:Monitor\"). Then reply with the Monitor tool result, verbatim."
)


def run(binary: Path) -> Verdict:
    return retry_inconclusive(lambda: _once(binary))


def _once(binary: Path) -> Verdict:
    rec = print_session(binary, PROMPT, name="monitor-persistent")
    schemas = [s for r in rec.requests if (s := tool_schema(r, "Monitor"))]
    has_param = any("persistent" in ((s.get("input_schema") or {}).get("properties") or {}) for s in schemas)
    for _, b in tool_uses(rec.rows, "Monitor"):
        res = tool_result_text(rec.rows, b.get("id")) or ""
        if "persistent" in res and "expires in" not in res:
            return Verdict(PATCHED, f"watch armed persistent; schema has persistent: {has_param}")
        if "expires in" in res:
            return Verdict(STOCK, f"watch got a deadline: {res[:120]!r}")
    if schemas:
        return Verdict(PATCHED if has_param else STOCK,
                       f"no Monitor call made; schema {'has' if has_param else 'lacks'} persistent")
    return Verdict(INCONCLUSIVE, f"Monitor never called nor loaded (rc={rec.returncode})")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
