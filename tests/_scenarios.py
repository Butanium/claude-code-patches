"""Expensive sessions that several tests read. Each runs once per binary per
process (see _harness.memoized), so the suite runner pays for one team session
even though idle-notif, shutdown-reason and peer-msg-warning all look at it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from _harness import INCONCLUSIVE, PATCHED, STOCK, Sandbox, Verdict, memoized, request_text, wait_for

TEAMMATE = "pinger"

SPAWN_PROMPT = (
    f"Use the Agent tool to spawn exactly one teammate named {TEAMMATE}. Pass it this prompt "
    "verbatim: \"Step 1: send the team lead one message with SendMessage (to: team-lead, message: "
    "pong), then end your turn without writing anything else. Step 2: later the lead will send you a "
    "shutdown_request. Approve it by calling SendMessage with to=team-lead and message set to the "
    "object {type: shutdown_response, request_id: <that request's id>, approve: true, reason: <a "
    "warm, specific thank-you note to the lead, at least 350 characters long>}. Include the reason "
    "field. If that call is rejected, write the exact error text as plain text, then approve again "
    "without the reason.\" After spawning, end your turn immediately; do not wait for the teammate."
)
SHUTDOWN_PROMPT = (
    f"Now send {TEAMMATE} a shutdown request: call SendMessage with to={TEAMMATE} and message set to "
    "the object {type: shutdown_request, reason: test finished}. Then end your turn."
)


@dataclass
class TeamRecord:
    """What one lead + one pane teammate session left behind."""
    rows: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)
    inbox: list[dict] = field(default_factory=list)
    teammate_went_idle: bool = False   # the teammate finished its first turn (idle glyph seen)
    teammate_exited: bool = False      # its pane closed after the shutdown request
    notes: list[str] = field(default_factory=list)


def _run_team(binary: Path) -> TeamRecord:
    rec = TeamRecord()
    with Sandbox(binary, "team") as sb:
        try:
            sb.start_interactive(["--model", "sonnet", "--dangerously-skip-permissions"])
            sb.send(SPAWN_PROMPT)
            rec.notes.append(f"lead after spawn: {sb.wait_idle(timeout=300)}")

            def teammate_pane():
                return next((p for p in sb.panes() if p["id"] != sb.panes()[0]["id"]), None)

            pane = wait_for(teammate_pane, timeout=120)
            if not pane:
                rec.notes.append("no teammate pane appeared")
            else:
                st = sb.wait_idle(pane["id"], timeout=240, min_s=10)
                rec.teammate_went_idle = st == "idle"
                rec.notes.append(f"teammate first turn: {st}")
                time.sleep(8)  # the idle notification (stock) is written at turn end
                sb.wait_idle(timeout=180, min_s=2)  # the lead may wake on the pong
                sb.send(SHUTDOWN_PROMPT)
                gone = wait_for(lambda: sb.pane_state(pane["id"]) == "gone", timeout=300, step=3)
                rec.teammate_exited = bool(gone)
                rec.notes.append(f"teammate exited: {rec.teammate_exited}")
                time.sleep(5)
                rec.notes.append(f"lead at end: {sb.wait_idle(timeout=180, min_s=2)}")
        except Exception as e:  # recorded; each test decides whether it has enough evidence
            rec.notes.append(f"scenario error: {e!r}")
        rec.rows = sb.rows()
        rec.requests = sb.requests()
        rec.responses = sb.responses()
        rec.inbox = sb.inbox_messages()
    return rec


def team_session(binary: Path) -> TeamRecord:
    return memoized(("team", str(binary)), lambda: _run_team(Path(binary)))


@dataclass
class PrintRecord:
    rows: list[dict]
    requests: list[dict]
    returncode: int
    stderr: str


def print_session(binary: Path, prompt: str, *args: str, model: str = "haiku",
                  settings: dict | None = None, env: dict | None = None,
                  timeout: float = 300, name: str = "print") -> PrintRecord:
    with Sandbox(binary, name, settings=settings) as sb:
        r = sb.run_print(prompt, *args, model=model, env=env, timeout=timeout)
        return PrintRecord(sb.rows(), sb.requests(), r.returncode, r.stderr[-2000:])


def baseline_session(binary: Path) -> PrintRecord:
    """One trivial `claude -p` turn: its request shows the tool list and schemas."""
    return memoized(("baseline", str(binary)),
                    lambda: print_session(Path(binary), "Reply with just: ok", name="baseline"))


# A tool that stays deferred on every binary: if it shows up in the loaded tool
# list, tool search is off for this account/model and "not deferred" proves nothing.
DEFERRED_CANARY = "WebFetch"


def undefer_verdict(binary: Path, tool: str) -> Verdict:
    """Patched: `tool` ships with its full schema in the first request's `tools`.
    Stock: it is absent there (deferred behind ToolSearch)."""
    rec = baseline_session(binary)
    if not rec.requests:
        return Verdict(INCONCLUSIVE, f"no request dumped (rc={rec.returncode}): {rec.stderr[-300:]}")
    names = {t.get("name") for t in rec.requests[0].get("tools") or []}
    if DEFERRED_CANARY in names or "ToolSearch" not in names:
        return Verdict(INCONCLUSIVE, "tool search is off in this session, deferral can't be observed")
    if tool in names:
        return Verdict(PATCHED, f"{tool} loaded up front ({len(names)} tools in the first request)")
    return Verdict(STOCK, f"{tool} deferred (not in the first request's tools)")


# Footer text of each permission mode, as the TUI prints it next to "(shift+tab to cycle)".
MODE_FOOTERS = {"plan": "plan mode on", "bypass": "bypass permissions on", "accept": "accept edits on",
                "auto": "auto mode on"}


@dataclass
class ModeRecord:
    rows: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _footer_mode(sb: Sandbox) -> str:
    tail = sb.capture()[-600:]
    return next((m for m, s in MODE_FOOTERS.items() if s in tail), "default")


def _run_mode_cycle(binary: Path) -> ModeRecord:
    """Shift+tab from default mode through plan into bypass-permissions mode,
    then one prompt: the mode-transition attachments land on that turn."""
    rec = ModeRecord()
    # The bypass-mode block is only produced when the "bash-first" gate is on; this
    # env var forces that gate (it is otherwise a server-side cohort flag).
    with Sandbox(binary, "modes", settings={"env": {"CLAUDE_CODE_THRIFTY_SONIC": "1"}}) as sb:
        try:
            sb.start_interactive(["--model", "haiku", "--allow-dangerously-skip-permissions"])
            rec.visited.append(_footer_mode(sb))
            for _ in range(6):
                sb.keys("BTab")
                time.sleep(1.5)
                rec.visited.append(_footer_mode(sb))
                if rec.visited[-1] == "bypass" and "plan" in rec.visited:
                    break
            sb.send("Reply with just: ok")
            rec.notes.append(f"after prompt: {sb.wait_idle(timeout=180, min_s=2)}")
        except Exception as e:
            rec.notes.append(f"scenario error: {e!r}")
        rec.rows = sb.rows()
        rec.requests = sb.requests()
    return rec


def mode_cycle_session(binary: Path) -> ModeRecord:
    return memoized(("modes", str(binary)), lambda: _run_mode_cycle(Path(binary)))


def mode_reminder_verdict(binary: Path, attachment: str, rendered: str) -> Verdict:
    """Patched: the transition's attachment is in the transcript but its text
    never reaches the model. Stock: the text is in the next request."""
    rec = mode_cycle_session(binary)
    produced = any((r.get("attachment") or {}).get("type") == attachment for r in rec.rows)
    seen = any(rendered in request_text(r) for r in rec.requests)
    if seen:
        return Verdict(STOCK, f"'{rendered}' reached the model (modes visited: {rec.visited})")
    if not produced:
        return Verdict(INCONCLUSIVE, f"no {attachment} attachment; visited={rec.visited} notes={rec.notes}")
    return Verdict(PATCHED, f"{attachment} produced, not rendered (modes visited: {rec.visited})")
