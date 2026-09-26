#!/usr/bin/env python3
"""peer-msg-warning: teammate messages reach the model without the security boilerplate.

Scenario (shared team session): the teammate sends the lead "pong" and the lead
sends the teammate a shutdown_request; both land as <teammate-message> blocks.
  patched: those blocks arrive bare in the API requests
  stock:   they are wrapped in "Another Claude session sent a message ..." plus
           the ~90-word "... permission laundering." paragraph
Linux/macOS only (tmux).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import INCONCLUSIVE, PATCHED, STOCK, Verdict, main, walk_strings
from _scenarios import team_session

NEEDS_TMUX = True
MARKERS = ("Another Claude session sent a message", "permission laundering")


def run(binary: Path) -> Verdict:
    rec = team_session(binary)
    blocks = [s for r in rec.requests for s in walk_strings(r.get("messages"))
              if "<teammate-message" in s]
    if not blocks:
        return Verdict(INCONCLUSIVE, f"no teammate message reached a request; notes={rec.notes}")
    wrapped = [s for s in blocks if any(m in s for m in MARKERS)]
    if wrapped:
        return Verdict(STOCK, f"{len(wrapped)}/{len(blocks)} teammate-message blocks carry the boilerplate")
    return Verdict(PATCHED, f"{len(blocks)} teammate-message blocks, none wrapped")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
