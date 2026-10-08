#!/usr/bin/env python3
"""CLI patch (experimental): an explicit pane `teammateMode` wins in non-interactive sessions.

Stock, the backend registry forces in-process teammates whenever the session
is non-interactive, before it even reads `teammateMode`:

    function EOt(e=rpe){
      if(ke())return t("[BackendRegistry] isInProcessEnabled: true (non-interactive session)"),!0;
      let n=w(),a;if(n==="in-process")a=!0;else if(n==="tmux"||n==="iterm2")a=!1;else{...}

`ke()` is true for `-p` and for any `--input-format stream-json` driver, which
includes the Claude Desktop app's bundled CLI (`~/.claude/remote/ccd-cli/<ver>`).
There every named teammate runs in-process, so `teammateMode: "tmux"` is
ignored and `CLAUDE_CODE_TEAMMATE_COMMAND` (e.g. the fork-teammate launcher)
never runs.

Patched, the non-interactive shortcut only fires when the mode is not an
explicit pane backend:

    if(ke()&&!["tmux","iterm2"].includes(w()))return t("[BackendRegistry] in-process: -p"),!0;

`auto` and `in-process` behave as before. The bytes come from the debug-log
string. Anchored on that log string and the `"in-process"` / `"tmux"` /
`"iterm2"` literals; the function names are captured.

That gate alone does nothing, because a non-interactive session has no team
and a named Agent call without a team becomes a plain background subagent
("Async agent launched"; `SendMessage(to="team-lead")` then fails with "No
agent named 'team-lead' is reachable"). The implicit session team is only
created for interactive sessions:

    if(Lo()&&!ke()&&!o.agentId)try{let{initializeSessionTeam:g}=await import(...);T=await g(void 0,k)}...

Second edit: create it for any session not started with `-p`/`--print`, so the
Desktop app's and the Agent SDK's stream-json launches (no `--print`) get a
team while `claude -p` runs stay stock:

    if(Lo()&&!o.print&&!o.agentId)try{...;T=await g(0  ,k)}...

(`g`'s first argument is only read as `t?.existingTeamName`, so `0` is
equivalent to `void 0` and pays for the longer condition.)

Probed 2026-10-05 on a copy of 2.1.289 with a desktop-shaped lead (stream-json
in/out, no TMUX): pane teammates launch on a `claude-swarm-<leadpid>` tmux
server, messages flow both ways (an idle lead starts a turn on its own when a
teammate message arrives), a fork-teammate inherits the lead's context, and
shutdown approvals remove the teammates and end the tmux server. Known gaps:
after any teammate has run, closing stdin no longer ends the lead (print mode's
`Iqt` counts the pane teammates' `in_process_teammate` task entries as running
and non-idle; its shutdown_approved handler drops the teammate from the team
file but never completes the task, and it then injects a "shut down your team /
use the cleanup operation" prompt the lead has no tool for). One teammate reply
was also enqueued twice, 8 ms apart. Hence experimental.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries, read_binary

LOG = b"[BackendRegistry] isInProcessEnabled: true (non-interactive session)"
LOG_PATCHED = b"[BackendRegistry] in-process: -p"
# Groups: 1 isNonInteractive, 2 debug logger, 3 teammate-mode getter, 4 the `n` local.
STOCK_RX = re.compile(
    rb"if\((" + JSID + rb")\(\)\)return (" + JSID + rb")\(\"" + re.escape(LOG) + rb"\"\),!0;"
    rb"let (" + JSID + rb")=(" + JSID + rb")\(\),"
)
PATCHED_RX = re.compile(
    rb"if\(" + JSID + rb"\(\)&&!\[\"tmux\",\"iterm2\"\]\.includes\(" + JSID + rb"\(\)\)\)return "
    + JSID + rb"\(\"" + re.escape(LOG_PATCHED) + rb" *\"\),!0;"
)


TEAM_STOCK_RX = re.compile(
    rb"if\((" + JSID + rb")\(\)&&!(" + JSID + rb")\(\)&&!(" + JSID + rb")\.agentId\)"
    rb"try\{let\{initializeSessionTeam:(" + JSID + rb")\}=await import\((\"[^\"]+\")\);"
    rb"(" + JSID + rb")=await \4\(void 0,(" + JSID + rb")\)\}"
)
TEAM_PATCHED_RX = re.compile(
    rb"if\(" + JSID + rb"\(\)&&!" + JSID + rb"\.print&&!" + JSID + rb"\.agentId\)"
    rb"try\{let\{initializeSessionTeam:" + JSID + rb"\}=await import\(\"[^\"]+\"\);"
    + JSID + rb"=await " + JSID + rb"\(0 *," + JSID + rb"\)\}"
)


def build_team_replacement(m: re.Match) -> bytes:
    lo, _ke, o, g, path, T, k = m.groups()
    head = b"if(" + lo + b"()&&!" + o + b".print&&!" + o + b".agentId)try{let{initializeSessionTeam:" + g + b"}=await import(" + path + b");" + T + b"=await " + g + b"(0"
    tail = b"," + k + b")}"
    pad = len(m.group(0)) - len(head) - len(tail)
    if pad < 0:
        raise RuntimeError(f"team-init replacement longer than stock by {-pad} bytes")
    replacement = head + b" " * pad + tail
    assert len(replacement) == len(m.group(0))
    return replacement


def build_replacement(m: re.Match) -> bytes:
    ke, log, n, getter = m.group(1), m.group(2), m.group(3), m.group(4)
    head = b"if(" + ke + b"()&&![\"tmux\",\"iterm2\"].includes(" + getter + b"()))return " + log + b"(\""
    tail = b"\"),!0;let " + n + b"=" + getter + b"(),"
    pad = len(m.group(0)) - len(head) - len(LOG_PATCHED) - len(tail)
    if pad < 0:
        raise RuntimeError(f"replacement longer than stock by {-pad} bytes — shorten LOG_PATCHED")
    replacement = head + LOG_PATCHED + b" " * pad + tail
    assert len(replacement) == len(m.group(0)), (len(replacement), len(m.group(0)))
    return replacement


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = read_binary(binp)
        gate_done = bool(PATCHED_RX.search(data))
        team_done = bool(TEAM_PATCHED_RX.search(data))
        if gate_done and team_done:
            print(f"teammate-mode-print: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        if (gate_done or STOCK_RX.search(data)) and (team_done or TEAM_STOCK_RX.search(data)):
            target = (binp, data)
            break

    if target is None:
        print(
            f"in-process gate `if(<ke>())return <t>(\"{LOG.decode()}\"),!0;let n=<mode>(),` not found "
            f"in any candidate binary ({[str(p) for p in candidate_binaries()]}) — upstream code changed. "
            "Re-investigate by grepping for `isInProcessEnabled` and reading the function around it.",
            file=sys.stderr,
        )
        return 1

    binp, data = target
    patched = data
    for stock, build, label in ((STOCK_RX, build_replacement, "in-process gate"),
                                (TEAM_STOCK_RX, build_team_replacement, "session-team init")):
        matches = list(stock.finditer(patched))
        if not matches:
            continue  # this half is already patched
        if len(matches) != 1:
            print(f"expected exactly 1 {label}, found {len(matches)} in {binp} — refusing to patch", file=sys.stderr)
            return 1
        m = matches[0]
        patched = patched[: m.start()] + build(m) + patched[m.end():]

    def _verify(written: bytes) -> None:
        if (len(written) != len(data) or len(PATCHED_RX.findall(written)) != 1
                or len(TEAM_PATCHED_RX.findall(written)) != 1
                or STOCK_RX.search(written) or TEAM_STOCK_RX.search(written)):
            raise RuntimeError("post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)
    print(f"teammate-mode-print: applied patch to {binp} (pristine backup at {binp}.orig)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
