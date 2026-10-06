#!/usr/bin/env python3
"""effort-session-only: interactive `/effort low` leaves settings.json alone.

Starts an interactive session and types `/effort low`.
  patched: the TUI says "(this session only)" and settings.json has no effortLevel
  stock:   the TUI says "(saved as your default ...)" and settings.json gets
           modelSettings.<model>.effortLevel: low (top-level effortLevel in older builds)
Uses `--model sonnet` (haiku has no effort levels). No model turn is sent.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import INCONCLUSIVE, PATCHED, STOCK, Sandbox, Verdict, main, retry_inconclusive


def run(binary: Path) -> Verdict:
    return retry_inconclusive(lambda: _once(binary))


def _once(binary: Path) -> Verdict:
    with Sandbox(binary, "effort") as sb:
        sb.start_interactive(["--model", "sonnet"])
        sb.tmux("send-keys", "-t", sb._pane("lead"), "-l", "/effort low")
        time.sleep(1.5)
        sb.keys("Enter")
        screen = ""
        for _ in range(30):
            time.sleep(1)
            screen = re.sub(r"\s+", " ", sb.capture())
            if "effort level to" in screen:
                break
        time.sleep(1)
        s = json.loads((sb.config / "settings.json").read_text())
        saved = "low" in json.dumps({k: s.get(k) for k in ("effortLevel", "modelSettings")})
        if "this session only" in screen and not saved:
            return Verdict(PATCHED, "session-only message, settings.json untouched")
        if saved and "saved as your default" in screen:
            return Verdict(STOCK, "effortLevel: low written to settings.json")
        return Verdict(INCONCLUSIVE, f"saved={saved!r}, screen tail={screen[-300:]!r}")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
