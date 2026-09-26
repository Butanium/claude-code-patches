#!/usr/bin/env python3
"""auto-background: a `sleep`-first Bash command that outlives its timeout is backgrounded, not killed.

One `claude -p` turn runs `sleep 8; echo bg-done` with BASH_DEFAULT_TIMEOUT_MS=3000.
  patched: "... was moved to the background" (output keeps streaming to a file)
  stock:   "Exit code 143 / Command timed out after 3s" (stock kills commands
           that begin with `sleep` instead of backgrounding them, 2.1.270+)
Cross-platform where the Bash tool exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (INCONCLUSIVE, PATCHED, STOCK, Verdict, main, retry_inconclusive,
                      tool_result_text, tool_uses)
from _scenarios import print_session

PROMPT = ('Call the Bash tool once with exactly these parameters and no others: '
          '{"command": "sleep 8; echo bg-done"}. Do not pass timeout or run_in_background. '
          'Then reply with what happened.')


def run(binary: Path) -> Verdict:
    return retry_inconclusive(lambda: _once(binary))


def _once(binary: Path) -> Verdict:
    rec = print_session(binary, PROMPT, name="auto-background",
                        settings={"env": {"BASH_DEFAULT_TIMEOUT_MS": "3000"}})
    # Only calls that ran under the 3 s default count: models sometimes retry
    # with their own `timeout`, which then lets the command finish.
    finished = False
    for _, b in tool_uses(rec.rows, "Bash"):
        inp = b.get("input") or {}
        if not inp.get("command", "").startswith("sleep 8") or "timeout" in inp or inp.get("run_in_background"):
            continue
        res = tool_result_text(rec.rows, b.get("id")) or ""
        if "moved to the background" in res:
            return Verdict(PATCHED, "timed-out sleep command was backgrounded")
        if "143" in res or "timed out" in res:
            return Verdict(STOCK, f"killed at the timeout: {res[:80]!r}")
        finished |= "bg-done" in res
    if finished:
        return Verdict(INCONCLUSIVE, "command finished inside the 3 s timeout")
    return Verdict(INCONCLUSIVE, f"no Bash call ran under the default timeout (rc={rec.returncode})")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
