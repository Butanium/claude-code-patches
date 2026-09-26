#!/usr/bin/env python3
"""idle-notif: a pane teammate ending its turn sends the lead no idle_notification.

Scenario (shared team session): a pane teammate messages the lead once and ends
its turn.
  patched: no {"type":"idle_notification","idleReason":"available"} from it
           reaches the lead (inbox file or any API request)
  stock:   that frame is in the lead's inbox and shows up as a lead turn
Inconclusive when the teammate was never seen going idle. Linux/macOS only (tmux).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (INCONCLUSIVE, PATCHED, STOCK, Verdict, json_objects_containing, main,
                      request_text, walk_strings)
from _scenarios import TEAMMATE, team_session

NEEDS_TMUX = True


def run(binary: Path) -> Verdict:
    rec = team_session(binary)
    texts = [request_text(r) for r in rec.requests] + list(walk_strings(rec.inbox))
    pings = [f for t in texts for f in json_objects_containing(t, '"idle_notification"')
             if f.get("type") == "idle_notification" and f.get("from") == TEAMMATE
             and f.get("idleReason") == "available"]
    if pings:
        return Verdict(STOCK, f"lead got {len(pings)} 'available' idle_notification(s) from {TEAMMATE}")
    if not rec.teammate_went_idle:
        return Verdict(INCONCLUSIVE, f"teammate never observed idle; notes={rec.notes}")
    return Verdict(PATCHED, f"{TEAMMATE} ended a turn and the lead got no idle_notification")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
