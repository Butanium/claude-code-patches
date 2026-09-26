#!/usr/bin/env python3
"""thinking-only-nag: a turn that ends with only a thinking block is not retried with a nag.

One `claude -p` turn against a local mock of the Messages API (no tokens spent)
that answers the prompt with a thinking block and no text.
  patched: the CLI accepts the turn; no second request carries the nag
  stock:   it re-runs the turn with "[Your previous response had no visible
           output. Please continue ...]" injected
Cross-platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (INCONCLUSIVE, PATCHED, STOCK, MockMessagesAPI, Sandbox, Verdict, main,
                      walk_strings)

MARKER = "THINKING-ONLY-PROBE"
NAG = "had no visible output"


def _reply(body: dict) -> list[dict]:
    text = "\n".join(walk_strings(body.get("messages")))
    if MARKER in text and NAG not in text:
        return [{"type": "thinking", "thinking": "Nothing to add."}]
    return [{"type": "text", "text": "ok"}]


def run(binary: Path) -> Verdict:
    with MockMessagesAPI(_reply) as mock, Sandbox(binary, "thinking-only", credentials=False) as sb:
        r = sb.run_print(f"{MARKER}: think, then reply.", timeout=180,
                         env={"ANTHROPIC_BASE_URL": mock.url, "ANTHROPIC_API_KEY": "sk-ant-mock"})
        main_loop = [b for b in mock.bodies if MARKER in "\n".join(walk_strings(b["body"].get("messages")))]
    if not main_loop:
        return Verdict(INCONCLUSIVE, f"the prompt never reached the mock (rc={r.returncode}): {r.stderr[-300:]}")
    if any(NAG in "\n".join(walk_strings(b["body"].get("messages"))) for b in main_loop):
        return Verdict(STOCK, f"turn re-run with the nag ({len(main_loop)} requests)")
    return Verdict(PATCHED, f"thinking-only turn accepted ({len(main_loop)} request)")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
