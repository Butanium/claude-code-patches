#!/usr/bin/env python3
"""fork-subagents: a fork receives the rewritten boilerplate and no "don't re-delegate" repeat.

A stream-json session against the mock Messages API, no tokens spent. The lead
starts a fork, then the fork makes a nested general-purpose Agent call and a
fork Agent call.
  patched: the fork's request has the new boilerplate, no request says
           "re-delegate" about forks, and the nested fork is still refused
  stock:   the fork's request says "You are a worker fork"
Cross-platform.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import INCONCLUSIVE, PATCHED, STOCK, MockMessagesAPI, Sandbox, Verdict, main, wait_for, walk_strings

LEAD_MARK = "FSB-LEAD-PROBE"
FORK_DIRECTIVE = "FSB-FORK-DIRECTIVE"
NESTED_MARK = "FSB-NESTED-PROBE"
NESTED_DONE = "FSB-NESTED-DONE"
FORK_DONE = "FSB-FORK-DONE"
NEW_INTRO = "You just split yourself as a fork"
STOCK_INTRO = "You are a worker fork."
REFUSAL = "Fork is not available inside a forked worker"
REPEATS = ["execute directly; do not re-delegate", "execute directly — don't re-delegate"]


def _all_text(body: dict) -> str:
    return "\n".join(walk_strings(body))


def _messages_text(body: dict) -> str:
    return "\n".join(walk_strings(body.get("messages")))


def _agent_tool(body: dict) -> str:
    have = {t.get("name") for t in body.get("tools") or []}
    return "Agent" if "Agent" in have else "Task"


def _reply(body: dict) -> list[dict]:
    text = _messages_text(body)
    if "<fork-boilerplate>" in text:
        if REFUSAL in text:
            return [{"type": "text", "text": FORK_DONE}]
        name = _agent_tool(body)
        return [
            {"type": "tool_use", "id": "toolu_fsb_nested", "name": name, "input": {
                "description": "fsb nested", "subagent_type": "general-purpose", "prompt": NESTED_MARK}},
            {"type": "tool_use", "id": "toolu_fsb_refork", "name": name, "input": {
                "description": "fsb refork", "subagent_type": "fork", "prompt": "refork"}},
        ]
    if NESTED_MARK in text:
        return [{"type": "text", "text": NESTED_DONE}]
    if LEAD_MARK in text and "tool_result" not in json.dumps(body.get("messages")):
        return [{"type": "tool_use", "id": "toolu_fsb_fork", "name": _agent_tool(body), "input": {
            "description": "fsb fork", "subagent_type": "fork", "prompt": FORK_DIRECTIVE}}]
    return [{"type": "text", "text": "ack"}]


def run(binary: Path) -> Verdict:
    with MockMessagesAPI(_reply) as mock, Sandbox(binary, "fork-subagents", credentials=False) as sb:
        cmd = [str(binary), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--model", "haiku", "--dangerously-skip-permissions"]
        env = sb.env(ANTHROPIC_BASE_URL=mock.url, ANTHROPIC_API_KEY="sk-ant-mock", CLAUDE_CODE_FORK_SUBAGENT="1")
        p = subprocess.Popen(cmd, cwd=sb.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True)

        def bodies() -> list[dict]:
            return [b["body"] for b in list(mock.bodies) if b["path"].split("?")[0].endswith("/v1/messages")]

        try:
            p.stdin.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": f"{LEAD_MARK}: start the fork."}}) + "\n")
            p.stdin.flush()
            wait_for(lambda: any(REFUSAL in _messages_text(b) for b in bodies()), timeout=60)
        finally:
            p.stdin.close()
            try:
                _, err = p.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
                _, err = p.communicate()
        seen = bodies()
    forks = [b for b in seen if "<fork-boilerplate>" in _messages_text(b)]
    if not forks:
        return Verdict(INCONCLUSIVE, f"no fork request in {len(seen)} requests: {err[-300:]}")
    first = _messages_text(forks[0])
    if STOCK_INTRO in first:
        return Verdict(STOCK, "the fork got the stock boilerplate")
    if NEW_INTRO not in first:
        return Verdict(INCONCLUSIVE, "the fork got neither boilerplate")
    stale = [r for r in REPEATS if any(r in _all_text(b) for b in seen)]
    if stale:
        return Verdict(STOCK, f"new boilerplate, but a request still says {stale}")
    if not any(REFUSAL in _messages_text(b) for b in forks):
        return Verdict(INCONCLUSIVE, "the fork-in-fork call was never refused")
    return Verdict(PATCHED, f"new boilerplate, no re-delegate repeat, re-fork refused ({len(seen)} requests)")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
