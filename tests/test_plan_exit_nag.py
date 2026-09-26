#!/usr/bin/env python3
"""plan-exit-nag: passing through plan mode without a plan leaves no "Exited Plan Mode" reminder.

Scenario (shared mode-cycle session): an interactive session is shift+tabbed
from default mode through plan mode into bypass-permissions mode, then prompted once.
  patched: the plan_mode_exit attachment is in the transcript, but
           "## Exited Plan Mode" is not in any API request
  stock:   that reminder text reaches the model
Linux/macOS only (tmux).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import Verdict, main
from _scenarios import mode_reminder_verdict

NEEDS_TMUX = True


def run(binary: Path) -> Verdict:
    return mode_reminder_verdict(binary, "plan_mode_exit", "Exited Plan Mode")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
