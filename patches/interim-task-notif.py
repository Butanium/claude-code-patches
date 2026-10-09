#!/usr/bin/env python3
"""CLI patch: an agent's interim task-notification no longer wakes the main session.

A background agent that ends a turn while its own background work (a Bash job
run in the background, a Monitor) is still running sends the session a
task-notification, and does so again every time that work wakes it and it stops
again. Each one starts a new turn of the main session, so an agent babysitting
an upload or a training run can wake the lead dozens of times with results like
"Upload running at about 165 MB/s". The CLI marks these with the note "This
agent stopped with background work of its own still running ... the result
below may be interim." (2.1.293, `enqueueAgentNotification`):

    let Ye=Ue===void 0||Ne===Ue, ... ,
        $t=s==="completed"&&VVr(xe)?"This agent stopped with background work ...":"A task-notification fires ...",
        zt=aa({...body:`<note>${$t}</note>${Rt}...`});
    ea({value:Ye?zt:rEn(zt),mode:"task-notification",skipAttachments:!0,priority:qtt,agentId:Ne,taskId:e,...});

`Ue` is the agent's owner agent (undefined for an agent the main session
started), `Ne` the agent the notification is routed to.

The edit hoists the interim test into a new local and marks the enqueued
command `passive` when the agent has no owner:

    ,Iq$=s==="completed"&&VVr(xe),$t=Iq$?"<shorter interim note>":"A task-notification fires ...",
    zt=...;ea({value:...,agentId:Ne,...Iq$&&Ue===void 0&&{passive:!0}/*marker*/,taskId:e,...

`passive` is the command queue's existing "don't wake" flag: the idle-drain
predicates skip passive items (`Fm(e)=sp(e)&&e.passive!==!0` in the REPL queue;
`cl(e)` in the print/stream-json runtime), and the CLI already sets it on an
agent notice routed to the main session (`userResumeNotice`). The notification
stays queued and reaches the model with the session's next turn: the next
prompt, peer message, or the agent's final notification, which is not passive.

Scope:
- Only the interim kind. The notification an agent sends when it stops with no
  background work left still wakes the session.
- Only agents with no owner agent. For an agent started by another agent, the
  notification is what wakes that owner, so making it passive could leave the
  owner waiting.
- Task state is untouched: the notified stamp (`r3`) and the keepalive release
  (`a_t`) run before this code, and nothing after it touches the registry.

Bytes: the interim note is shortened (same meaning) to pay for the new local
and the spread; the remainder is the marker comment and space padding, both
outside any string. The marker is the idempotency signal.

Re-investigation anchor if this stops applying after an update: the string
"This agent stopped with background work of its own still running" and the
`ea({value:...,mode:"task-notification",...})` call right after it.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries, read_binary

MARKER = b"[itn5K interim notif passive]"
NOTE_HEAD = b"This agent stopped with background work of its own still running"
SHORT_NOTE = (
    b"This agent stopped with background work of its own still running. It may resume when that"
    b" work completes, and the same task-id notifies again; the result below may be interim."
)
# A new local for the hoisted interim test; the first one absent from i_t's text is used.
NAME_CANDIDATES = [b"Iq$", b"Iq_", b"$Iq", b"_Iq$"]

SITE = re.compile(
    rb"let (?P<ye>" + JSID + rb")=(?P<ue>" + JSID + rb")===void 0\|\|(?P<ne>" + JSID + rb")===(?P=ue),"
    rb"(?P<pre>.{0,1500}?)"
    rb",(?P<t>" + JSID + rb")=(?P<cond>" + JSID + rb'==="completed"&&' + JSID + rb"\(" + JSID + rb"\))\?"
    rb'"' + re.escape(NOTE_HEAD) + rb'[^"]*"'
    rb'(?P<mid>:"A task-notification fires each time[^"]*",.{0,800}?\);'
    + JSID + rb"\(\{value:(?P=ye)\?" + JSID + rb":" + JSID + rb"\(" + JSID + rb"\),"
    rb'mode:"task-notification",skipAttachments:!0,priority:' + JSID + rb",agentId:(?P=ne),)",
    re.DOTALL,
)


def _ident_in(name: bytes, text: bytes) -> bool:
    return re.search(rb"(?<![A-Za-z0-9_$])" + re.escape(name) + rb"(?![A-Za-z0-9_$])", text) is not None


def build(data: bytes, m: re.Match) -> bytes:
    fn_start = data.rfind(b"function ", max(0, m.start() - 4000), m.start())
    scope = data[fn_start if fn_start != -1 else m.start() - 4000 : m.end() + 600]
    name = next((c for c in NAME_CANDIDATES if not _ident_in(c, scope)), None)
    if name is None:
        raise RuntimeError(f"every candidate local name {NAME_CANDIDATES} already appears in the function")
    head = (
        b"let " + m["ye"] + b"=" + m["ue"] + b"===void 0||" + m["ne"] + b"===" + m["ue"] + b","
        + m["pre"]
        + b"," + name + b"=" + m["cond"] + b"," + m["t"] + b"=" + name + b'?"' + SHORT_NOTE + b'"'
        + m["mid"]
        + b"..." + name + b"&&" + m["ue"] + b"===void 0&&{passive:!0}"
    )
    old = m.group(0)
    pad = len(old) - len(head) - len(b"/*") - len(MARKER) - len(b"*/,")
    if pad < 0:
        raise RuntimeError(f"replacement is {-pad} bytes longer than the site; shorten SHORT_NOTE")
    new = head + b"/*" + MARKER + b" " * pad + b"*/,"
    assert len(new) == len(old), (len(new), len(old))
    return new


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = read_binary(binp)
        if MARKER in data:
            print(f"interim-task-notif: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        matches = list(SITE.finditer(data))
        if not matches:
            continue
        if len(matches) != 1:
            print(
                f"expected exactly 1 enqueue-site match, found {len(matches)} in {binp} — upstream "
                f"code changed; re-investigate around {NOTE_HEAD.decode()!r} in the binary",
                file=sys.stderr,
            )
            return 1
        target = (binp, data, matches[0])
        break

    if target is None:
        print(
            f"enqueue site not found in any candidate binary "
            f"({[str(p) for p in candidate_binaries()]}) — upstream code changed or unknown "
            f"install layout; re-investigate around {NOTE_HEAD.decode()!r} and the "
            f'ea({{value:...,mode:"task-notification",...}}) call after it',
            file=sys.stderr,
        )
        return 1

    binp, data, m = target
    try:
        new = build(data, m)
    except RuntimeError as e:
        print(f"interim-task-notif: {e}", file=sys.stderr)
        return 1
    patched = data[: m.start()] + new + data[m.end() :]
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        if len(written) != len(data) or MARKER not in written or SITE.search(written) is not None:
            raise RuntimeError(
                f"post-write verification failed (len={len(written)} want={len(data)}, "
                f"marker={MARKER in written}, site_still_present={SITE.search(written) is not None})"
                f" — live binary untouched"
            )

    apply_patch(binp, data, patched, _verify)
    print(
        f"interim-task-notif: applied patch to {binp} (interim agent notifications to the main "
        f"session are queued passive; pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
