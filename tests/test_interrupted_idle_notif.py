#!/usr/bin/env python3
"""interrupted-idle-notif: interrupting a busy in-process teammate sends the lead no "interrupted" ping.

An interactive lead (teammateMode in-process) spawns a teammate that starts a
two-minute command. The test opens the teammate's view (↓ ↓ Enter) and presses
Escape until the TUI shows "Interrupted", as a user would.
  patched: no idle_notification with idleReason "interrupted" reaches the lead
  stock:   the lead's next API request carries one
Inconclusive unless the interrupt was seen on screen. (TaskStop is not the same
path: it kills the teammate, and neither binary pings then.) Linux/macOS only (tmux).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (INCONCLUSIVE, PATCHED, STOCK, Sandbox, Verdict, json_objects_containing, main,
                      request_text, wait_for)

NEEDS_TMUX = True
PROMPT = (
    "Use the Agent tool to spawn one teammate named worker with this prompt: \"Run this exact Bash "
    "command in the foreground with timeout 300000: python3 -c 'import time; time.sleep(120)' — then "
    "reply done.\" Then end your turn immediately."
)


def run(binary: Path) -> Verdict:
    with Sandbox(binary, "interrupted", settings={"teammateMode": "in-process"}) as sb:
        notes, interrupted = [], False
        try:
            sb.start_interactive(["--model", "sonnet", "--dangerously-skip-permissions"])
            sb.send(PROMPT)
            notes.append(f"lead: {sb.wait_idle(timeout=300, min_s=3)}")
            running = wait_for(lambda: "time.sleep(120)" in sb.capture() or "worker" in sb.capture(), 60)
            time.sleep(12)  # let the teammate get into its long command
            for key in ("Down", "Down", "Enter"):  # teammate list -> worker -> its view
                sb.keys(key)
                time.sleep(1.5)
            for _ in range(3):
                sb.keys("Escape")
                time.sleep(2.5)
                if "Interrupted" in sb.capture():
                    interrupted = True
                    break
            notes.append(f"worker listed: {bool(running)}")
            time.sleep(15)  # a stock ping wakes the lead, which then sends a request
        except Exception as e:
            notes.append(f"scenario error: {e!r}")
        requests = sb.requests()
    pings = [f for r in requests for f in json_objects_containing(request_text(r), '"idle_notification"')
             if f.get("idleReason") == "interrupted"]
    if pings:
        return Verdict(STOCK, "lead got an 'interrupted' idle_notification")
    if not interrupted:
        return Verdict(INCONCLUSIVE, f"never saw the teammate interrupted; notes={notes}")
    return Verdict(PATCHED, "teammate interrupted; the lead got no idle_notification")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
