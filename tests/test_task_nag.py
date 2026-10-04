#!/usr/bin/env python3
"""task-nag: no "task tools haven't been used recently" reminder in a long run of turns.

One `claude -p --model haiku` session (haiku gets the task tools; newer models
don't, so the reminder can't fire there at all) makes 14 sequential Bash calls
without touching task tools, with CLAUDE_CODE_TODO_REMINDER_MODE=baseline so the
env switch that also silences the reminder is not what we observe.
  patched: no task_reminder / todo_reminder attachment in the transcript
           (mod arm: attachments may be recorded, but none reaches a request)
  stock:   at least one, sent to the model (the stock threshold is 10 turns)
Cross-platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import INCONCLUSIVE, PATCHED, STOCK, Verdict, is_mod_arm, main, tool_uses, walk_strings
from _scenarios import print_session

N = 14
PROMPT = (
    f"Run these {N} Bash commands strictly one at a time, one command per tool call, waiting for "
    f"each result before starting the next: " + ", ".join(f"echo step{i}" for i in range(1, N + 1))
    + ". Do not use any task or todo tools. When all are done, reply: done."
)


def run(binary: Path) -> Verdict:
    rec = print_session(binary, PROMPT, name="task-nag", timeout=600,
                        settings={"env": {"CLAUDE_CODE_TODO_REMINDER_MODE": "baseline"}})
    calls = len(tool_uses(rec.rows, "Bash"))
    reminders = [r for r in rec.rows if (r.get("attachment") or {}).get("type") in ("task_reminder", "todo_reminder")]
    sent = {s for r in rec.requests for s in walk_strings(r.get("messages")) if "been used recently" in s}
    if sent:
        return Verdict(STOCK, f"{len(reminders)} reminder(s) over {calls} Bash turns, sent to the model")
    if reminders:
        # The mod keeps the reminder out of requests; only the patch keeps it out of the transcript.
        return Verdict(PATCHED if is_mod_arm(binary) else STOCK,
                       f"{len(reminders)} reminder(s) recorded in the transcript, none sent to the model")
    if calls < 11:
        return Verdict(INCONCLUSIVE, f"only {calls} Bash turns; the stock threshold is 10")
    return Verdict(PATCHED, f"no reminder over {calls} Bash turns")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
