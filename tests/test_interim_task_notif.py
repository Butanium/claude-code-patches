#!/usr/bin/env python3
"""interim-task-notif: an agent's interim task-notification never reaches the main session.

A stream-json session (the Desktop app's mode) against the mock Messages API, no
tokens spent. The lead starts a background agent; the agent starts `sleep 12`
as a background Bash job and ends its turn with an interim result, then ends
again with a final result when the job's notification wakes it.
  patched: no lead request carries the interim result; the final one arrives
  stock:   a lead request carries the interim result
Cross-platform (needs `sleep` in the Bash tool's shell).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (INCONCLUSIVE, PATCHED, STOCK, MockMessagesAPI, Sandbox, Verdict, main, wait_for,
                      walk_strings)

LEAD_MARK = "ITN-LEAD-PROBE"
AGENT_MARK = "ITN-AGENT-PROBE"
INTERIM = "ITN-INTERIM-RESULT"
FINAL = "ITN-FINAL-RESULT"


def _user_texts(body: dict) -> list[str]:
    return ["\n".join(walk_strings(m.get("content"))) for m in body.get("messages") or [] if m.get("role") == "user"]


def _has_tool_result(body: dict) -> bool:
    return any(isinstance(b, dict) and b.get("type") == "tool_result"
               for m in body.get("messages") or [] if isinstance(m.get("content"), list) for b in m["content"])


def _role(body: dict) -> str:
    users = _user_texts(body)
    first = users[0] if users else ""
    if LEAD_MARK in first:
        return "lead"
    if AGENT_MARK in first:
        return "agent"
    return "other"


def _tool(body: dict, *names: str) -> str | None:
    have = {t.get("name") for t in body.get("tools") or []}
    return next((n for n in names if n in have), None)


def _reply(body: dict) -> list[dict]:
    role = _role(body)
    if role == "lead":
        if not _has_tool_result(body):
            return [{"type": "tool_use", "id": "toolu_itn_agent", "name": _tool(body, "Agent", "Task"), "input": {
                "description": "itn probe agent", "subagent_type": "general-purpose", "run_in_background": True,
                "prompt": f"{AGENT_MARK}: start the background job, then report.",
            }}]
        return [{"type": "text", "text": "lead-ack"}]
    if role == "agent":
        if not _has_tool_result(body):
            return [{"type": "tool_use", "id": "toolu_itn_bash", "name": "Bash", "input": {
                "command": "sleep 12; echo itn-job-done", "description": "itn probe job", "run_in_background": True,
            }}]
        if "<task-notification>" in _user_texts(body)[-1]:
            return [{"type": "text", "text": FINAL}]
        return [{"type": "text", "text": INTERIM}]
    return [{"type": "text", "text": "ok"}]


def _texts(mock: MockMessagesAPI, role: str) -> list[str]:
    bodies = [b["body"] for b in list(mock.bodies) if b["path"].split("?")[0].endswith("/v1/messages")]
    return ["\n".join(walk_strings(b.get("messages"))) for b in bodies if _role(b) == role]


def run(binary: Path) -> Verdict:
    with MockMessagesAPI(_reply) as mock, Sandbox(binary, "interim-notif", credentials=False) as sb:
        cmd = [str(binary), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--model", "haiku", "--dangerously-skip-permissions"]
        env = sb.env(ANTHROPIC_BASE_URL=mock.url, ANTHROPIC_API_KEY="sk-ant-mock")
        p = subprocess.Popen(cmd, cwd=sb.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True)
        try:
            p.stdin.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": f"{LEAD_MARK}: start the probe agent."}}) + "\n")
            p.stdin.flush()
            wait_for(lambda: any(FINAL in t for t in _texts(mock, "lead")), timeout=90)
        finally:
            p.stdin.close()
            try:
                _, err = p.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
                _, err = p.communicate()
        lead, agent = _texts(mock, "lead"), _texts(mock, "agent")
    notes = f"rc={p.returncode}, {len(lead)} lead / {len(agent)} agent requests"
    if not any(INTERIM in t for t in agent):
        return Verdict(INCONCLUSIVE, f"the agent never produced its interim result ({notes}): {err[-300:]}")
    if any(INTERIM in t for t in lead):
        return Verdict(STOCK, f"a lead request carried the interim result ({notes})")
    if not any(FINAL in t for t in lead):
        return Verdict(INCONCLUSIVE, f"no lead request carried the final result ({notes}): {err[-300:]}")
    return Verdict(PATCHED, f"the lead got the final result and never the interim one ({notes})")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
