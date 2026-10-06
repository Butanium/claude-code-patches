#!/usr/bin/env python3
"""thinking-summaries-print: `showThinkingSummaries: true` reaches `thinking.display` in -p mode.

Two `claude -p` turns in a sandbox whose settings.json sets showThinkingSummaries.
1. `--output-format text` on haiku, against the real API (one cheap call):
     patched: the request carries display "summarized" and the transcript's
              thinking block has text
     stock:   the request carries display "omitted" and the thinking block is empty
2. `--output-format stream-json --verbose` (the shape the Agent SDK and the
   Desktop app launch the CLI with) against the local mock API, no tokens:
     patched: display "summarized"
     stock:   no display key at all
Cross-platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (INCONCLUSIVE, PATCHED, STOCK, MockMessagesAPI, Sandbox, Verdict, main,
                      retry_inconclusive, walk_strings)

MARKER = "THINKING-DISPLAY-PROBE"
PROMPT = f"{MARKER}: what is 17*23 + 5? Work it out step by step in your head, then answer with just the number."
SETTINGS = {"showThinkingSummaries": True}


def _main_requests(bodies: list[dict]) -> list[dict]:
    return [b for b in bodies if MARKER in "\n".join(walk_strings(b.get("messages")))]


def _display(req: dict) -> str | None:
    thinking = req.get("thinking")
    if not isinstance(thinking, dict):
        return None
    return thinking.get("display", "<absent>")


def _thinking_lengths(rows: list[dict]) -> list[int]:
    out = []
    for r in rows:
        if r.get("type") != "assistant":
            continue
        for b in (r.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "thinking":
                out.append(len(b.get("thinking") or ""))
    return out


def _text_run(binary: Path) -> Verdict:
    with Sandbox(binary, "thinking-summaries", settings=SETTINGS) as sb:
        r = sb.run_print(PROMPT, output_format="text", timeout=240)
        reqs = _main_requests(sb.requests())
        lengths = _thinking_lengths(sb.rows())
    if not reqs:
        return Verdict(INCONCLUSIVE, f"no main-loop request dumped (rc={r.returncode}): {r.stderr[-300:]}")
    display = _display(reqs[0])
    if display is None:
        return Verdict(INCONCLUSIVE, "the request has no thinking config (thinking disabled for this model?)")
    if display == "omitted" and not any(lengths):
        return Verdict(STOCK, f"text mode sent display=omitted; thinking blocks empty ({lengths})")
    if display == "summarized":
        if any(lengths):
            return Verdict(PATCHED, f"text mode sent display=summarized; thinking block of {max(lengths)} chars")
        return Verdict(INCONCLUSIVE, "display=summarized but the model produced no thinking block")
    return Verdict(INCONCLUSIVE, f"unexpected display={display!r}, thinking lengths {lengths}")


def _stream_json_run(binary: Path) -> Verdict:
    with MockMessagesAPI(lambda body: [{"type": "text", "text": "ok"}]) as mock, \
            Sandbox(binary, "thinking-summaries-sj", settings=SETTINGS, credentials=False) as sb:
        r = sb.run_print(PROMPT, timeout=180,
                         env={"ANTHROPIC_BASE_URL": mock.url, "ANTHROPIC_API_KEY": "sk-ant-mock"})
        reqs = _main_requests([b["body"] for b in mock.bodies])
    if not reqs:
        return Verdict(INCONCLUSIVE, f"the prompt never reached the mock (rc={r.returncode}): {r.stderr[-300:]}")
    display = _display(reqs[0])
    if display == "summarized":
        return Verdict(PATCHED, "stream-json sent display=summarized")
    if display == "<absent>":
        return Verdict(STOCK, "stream-json sent no display")
    return Verdict(INCONCLUSIVE, f"stream-json: unexpected display={display!r}")


def run(binary: Path) -> Verdict:
    text = retry_inconclusive(lambda: _text_run(binary))
    if text.status == INCONCLUSIVE:
        return text
    sj = _stream_json_run(binary)
    if sj.status == text.status:
        return Verdict(text.status, f"{text.detail}; {sj.detail}")
    return Verdict(INCONCLUSIVE, f"text mode observed {text.status} ({text.detail}) but {sj.detail}")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
