#!/usr/bin/env python3
"""sleep-guard-off: a foreground `sleep 26` runs instead of being refused.

One `claude -p` turn asks the model to run `sleep 26; echo slept-ok` in Bash.
  patched: the command runs and its output contains slept-ok
  stock:   the call is refused before it runs ("Blocked: sleep 26 ...")
Cross-platform where the Bash tool exists (not the PowerShell-only setup).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (INCONCLUSIVE, PATCHED, STOCK, Verdict, main, retry_inconclusive,
                      tool_result_text, tool_uses)
from _scenarios import print_session

PROMPT = (
    "Run exactly this Bash command, in the foreground, with no timeout and no run_in_background "
    "parameter: sleep 26; echo slept-ok\nThen reply with its output."
)


def run(binary: Path) -> Verdict:
    return retry_inconclusive(lambda: _once(binary))


def _once(binary: Path) -> Verdict:
    rec = print_session(binary, PROMPT, name="sleep-guard", timeout=400)
    for _, b in tool_uses(rec.rows, "Bash"):
        if "sleep 26" not in (b.get("input") or {}).get("command", ""):
            continue
        res = tool_result_text(rec.rows, b.get("id")) or ""
        if "Blocked" in res and "sleep" in res:
            return Verdict(STOCK, f"refused: {res[:100]!r}")
        if "slept-ok" in res:
            return Verdict(PATCHED, "sleep 26 ran in the foreground")
    return Verdict(INCONCLUSIVE, f"no matching Bash call/result (rc={rec.returncode})")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
