#!/usr/bin/env python3
"""teammate-cwd: a named Agent call with `cwd` starts a pane teammate in that directory.

Two observations, both must agree:
  schema  (one trivial `claude -p` turn, dumped request body)
    patched: the Agent tool's input schema has a `cwd` property
    stock:   it doesn't (the full schema defines it but the model-facing one omits it)
  routing (interactive lead in tmux asked to call Agent with name + cwd)
    patched: a teammate pane opens and its shell sits in the requested directory
    stock:   the teammate (pane or in-process) runs in the lead's directory
The routing half needs tmux (psmux >= 3.3.8 on Windows).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import INCONCLUSIVE, PATCHED, STOCK, Sandbox, Verdict, main, retry_inconclusive, tool_schema, \
    tool_uses, wait_for
from _scenarios import baseline_session

NEEDS_TMUX = True
TEAMMATE = "walker"


def schema_verdict(binary: Path) -> Verdict:
    rec = baseline_session(binary)
    agent = next((s for r in rec.requests if (s := tool_schema(r, "Agent"))), None)
    if agent is None:
        return Verdict(INCONCLUSIVE, f"no Agent tool in the dumped requests (rc={rec.returncode})")
    props = (agent.get("input_schema") or {}).get("properties") or {}
    if "cwd" in props:
        return Verdict(PATCHED, "Agent schema offers cwd")
    return Verdict(STOCK, "Agent schema has no cwd")


def _same(a: str | Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == b.resolve()
    except OSError:
        return False


def routing_verdict(binary: Path) -> Verdict:
    with Sandbox(binary, "teammate-cwd") as sb:
        target = sb.root / "elsewhere"
        target.mkdir()
        sb.trust(target)
        prompt = (
            f'Call the Agent tool exactly once with these parameters: name "{TEAMMATE}", '
            f'cwd "{target}", description "cwd check", prompt "Reply with just: ok". '
            "If the call is rejected because of cwd, call it again without cwd. "
            "After that, end your turn immediately; do not wait for the agent."
        )
        try:
            sb.start_interactive(["--model", "sonnet", "--effort", "low", "--dangerously-skip-permissions"])
            sb.send(prompt)
            sb.wait_idle(timeout=240)
        except RuntimeError as e:
            return Verdict(INCONCLUSIVE, f"lead: {str(e)[:200]}")
        lead_id = sb.panes()[0]["id"]
        pane = wait_for(lambda: next((p for p in sb.panes() if p["id"] != lead_id), None), timeout=60)
        calls = [{k: b.get("input", {}).get(k) for k in ("name", "cwd")} for _, b in tool_uses(sb.rows(), "Agent")]
        asked_cwd = any(c.get("cwd") for c in calls)
        if pane:
            time.sleep(3)  # the pane's shell runs `cd <dir> && ... claude`; let the cd land
            pane = next((p for p in sb.panes() if p["id"] == pane["id"]), pane)
            if _same(pane["path"], target):
                return Verdict(PATCHED, f"teammate pane runs in the requested cwd ({target.name}/)")
            if _same(pane["path"], sb.cwd):
                if not asked_cwd:  # the raw tool_use input keeps cwd even where the schema strips it
                    return Verdict(INCONCLUSIVE, f"the model never passed cwd (Agent inputs: {calls})")
                return Verdict(STOCK, f"teammate pane runs in the lead's cwd (Agent inputs: {calls})")
            return Verdict(INCONCLUSIVE, f"teammate pane in an unexpected dir {pane['path']!r}")
        if not calls:
            return Verdict(INCONCLUSIVE, "the lead made no Agent call")
        # No pane: the call became an in-process agent (stock routes name+cwd there)
        # or was refused (patched in-process spawner refuses cwd; not expected with tmux).
        return Verdict(STOCK, f"no teammate pane opened (Agent inputs: {calls})")


def run(binary: Path) -> Verdict:
    schema = schema_verdict(binary)
    routing = retry_inconclusive(lambda: routing_verdict(binary), attempts=2)
    detail = f"schema: {schema.detail}; routing: {routing.detail}"
    if INCONCLUSIVE in (schema.status, routing.status):
        return Verdict(INCONCLUSIVE, detail)
    if schema.status != routing.status:
        return Verdict("mixed", detail)
    return Verdict(schema.status, detail)


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
