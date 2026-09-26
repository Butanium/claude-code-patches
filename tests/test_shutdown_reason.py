#!/usr/bin/env python3
"""shutdown-reason: a teammate's approval reason reaches the lead, whole.

Scenario (shared team session): a pane teammate is told to approve the lead's
shutdown_request with a thank-you `reason` of 350+ characters.
  patched: the lead's API request carries a shutdown_approved frame whose
           `reason` equals what the teammate sent (edit A lets the call through,
           edit B puts the reason in the frame, edit C lets more than 256 chars
           of it through the lead's receive sanitizer)
  stock:   the teammate's SendMessage call is rejected ("reason is only
           delivered on rejections"), or the reason arrives cut at 256 chars
Linux/macOS only (tmux).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (INCONCLUSIVE, PATCHED, STOCK, Verdict, json_objects_containing, main,
                      request_text, response_tool_uses, tool_result_text, tool_uses)
from _scenarios import team_session

NEEDS_TMUX = True

REJECTION = "reason is only delivered on rejections"


def _message(block: dict) -> dict:
    m = (block.get("input") or {}).get("message")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            return {}
    return m if isinstance(m, dict) else {}


def run(binary: Path) -> Verdict:
    rec = team_session(binary)
    rejected = False
    for _, b in tool_uses(rec.rows, "SendMessage"):
        if REJECTION in (tool_result_text(rec.rows, b.get("id")) or ""):
            rejected = True
    # The approval is the teammate's last act before it exits, so its transcript
    # may not have it; the dumped response body always does.
    calls = [b for _, b in tool_uses(rec.rows, "SendMessage")] + response_tool_uses(rec.responses, "SendMessage")
    sent = [m["reason"] for b in calls if (m := _message(b)).get("type") == "shutdown_response"
            and m.get("approve") is True and m.get("reason")]

    received = []
    for req in rec.requests:
        for frame in json_objects_containing(request_text(req), '"shutdown_approved"'):
            if frame.get("type") == "shutdown_approved" and frame.get("reason"):
                received.append(frame["reason"])

    if received:
        got = max(received, key=len)
        if got in sent:
            c = "exercised" if len(got) > 256 else f"not exercised (reason only {len(got)} chars)"
            return Verdict(PATCHED, f"lead received the {len(got)}-char reason intact; edit C {c}")
        if any(s.startswith(got.removesuffix(" [truncated]")[:200]) for s in sent):
            return Verdict(STOCK, f"reason reached the lead cut to {len(got)} chars (edit C missing)")
        return Verdict(PATCHED, f"lead received a reason ({len(got)} chars) but no matching send was found")
    if rejected:
        return Verdict(STOCK, "teammate's approve-with-reason call was rejected")
    return Verdict(INCONCLUSIVE, f"no approval with a reason seen; sent={len(sent)} notes={rec.notes}")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
