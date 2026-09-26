#!/usr/bin/env python3
"""mode-nag-off: entering bypass-permissions mode injects no "While bypass permissions mode is active" block.

Scenario (shared mode-cycle session): an interactive session is shift+tabbed
into bypass-permissions mode, then prompted once.
  patched: the auto_mode attachment is in the transcript, but its text is not
           in any API request
  stock:   "While bypass permissions mode is active:" reaches the model
The session forces the "bash-first" gate (CLAUDE_CODE_THRIFTY_SONIC=1): without
it, stock produces no bypass-mode attachment at all and there is nothing to
suppress. Only the bypass branch is exercised; the auto-mode entry/exit texts
share the same two gated renderers. Linux/macOS only (tmux).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import Verdict, main
from _scenarios import mode_reminder_verdict

NEEDS_TMUX = True


def run(binary: Path) -> Verdict:
    return mode_reminder_verdict(binary, "auto_mode", "While bypass permissions mode is active")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
