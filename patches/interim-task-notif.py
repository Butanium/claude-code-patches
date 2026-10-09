#!/usr/bin/env python3
"""CLI patch: drop an agent's interim task-notifications to the main session.

A background agent that ends a turn while its own background work (a Bash job
run in the background, a Monitor) is still running sends the session a
task-notification, and does so again every time that work wakes it and it stops
again. Each one starts a new turn of the main session, so an agent babysitting
an upload or a training run can wake the lead dozens of times with results like
"Upload running at about 165 MB/s". The CLI marks these with the note "This
agent stopped with background work of its own still running ... the result
below may be interim." (2.1.293, `enqueueAgentNotification`):

    Ne=a_t({ownerAgentId:Ue,keepaliveReason:`agent:${e}`,delivering:Ce,taskRegistry:w}); ...
    let Ye=Ue===void 0||Ne===Ue, ... ,
        $t=s==="completed"&&VVr(xe)?"This agent stopped with background work ...":"A task-notification fires ...",
        zt=aa({...body:`<note>${$t}</note>${Rt}...`});
    ea({value:Ye?zt:rEn(zt),mode:"task-notification",...,agentId:Ne,...},{turnAttribution:"inherit"})

`Ue` is the agent's owner. For an agent the main session started it holds the
main session's id, which is not a task in the registry `w`; an agent started by
another agent has that agent's task there. `a_t` routes on the same distinction.
The edit skips the enqueue for the interim kind when the owner is not a task:

    $t=s==="completed"&&VVr(xe)?"<shorter interim note>":"A task-notification fires ...",zt=...;
    s==="completed"&&VVr(xe)&&!w.get(Ue)||/*marker*/ea({value:...

What still arrives:
- The notification an agent sends when it stops with no background work left,
  and failure / stop notifications. These are byte-identical to stock.
- Interim notifications of an agent started by another agent (a subagent or an
  in-process teammate). That notification is what wakes the owner, so it is kept
  (with the shorter note).
- Anything the agent sends with SendMessage. An agent that ends a turn with a
  plain-text question while its job runs is not heard until it stops for good.

Task state is untouched: the notified stamp (`r3`) and the keepalive release
(`a_t`) run before this code, and nothing after the enqueue touches the
registry. A skipped interim enqueue also skips its handback pointer (`tdt`),
which only matters for a notification the session would not have woken for.

Dropping was chosen over delivering without a wake. Marking the command
`passive:!0` or `shouldQuery:!1` instead did not stop the wake in a stream-json
session (the Desktop app's mode) on 2.1.293. For `passive`, the print runtime's
fallback head-taking (`dequeueOrphansFirst`) seems to take the item anyway;
the `shouldQuery` failure was not traced. Delivering every interim result on
the next turn would also pile up stale progress lines. A cleaner variant
(deliver without waking, keep only the latest interim per agent) is in IDEAS.md.

Bytes: the interim note is shortened (same meaning) to pay for the guard; the
remainder is the marker comment and space padding, outside any string. The
marker is the idempotency signal.

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
from _binpatch import JSID, apply_patch, candidate_binaries, read_binary, splice

MARKER = b"[itn5K interim notif dropped]"
NOTE_HEAD = b"This agent stopped with background work of its own still running"
SHORT_NOTE = (
    b"This agent stopped with background work of its own still running and may notify again"
    b" later; the result below may be interim."
)

SITE = re.compile(
    rb"(?P<ne>" + JSID + rb")=" + JSID + rb"\(\{ownerAgentId:(?P<ue>" + JSID + rb"),keepaliveReason:.{0,80}?"
    rb",taskRegistry:(?P<reg>" + JSID + rb")\}\);.{0,600}?"
    rb"let (?P<ye>" + JSID + rb")=(?P=ue)===void 0\|\|(?P=ne)===(?P=ue),.{0,1500}?"
    rb",(?P<t>" + JSID + rb")=(?P<cond>" + JSID + rb'==="completed"&&' + JSID + rb"\(" + JSID + rb"\))\?"
    rb'"(?P<note>' + re.escape(NOTE_HEAD) + rb'[^"]*)"'
    rb':"A task-notification fires each time[^"]*",.{0,800}?\);'
    rb"(?P<call>" + JSID + rb"\(\{value:(?P=ye)\?" + JSID + rb":" + JSID + rb"\(" + JSID + rb"\),"
    rb'mode:"task-notification",skipAttachments:!0,priority:' + JSID + rb",agentId:(?P=ne),)",
    re.DOTALL,
)


def build(m: re.Match) -> bytes:
    old = m.group(0)
    o = m.start()
    guard = m["cond"] + b"&&!" + m["reg"] + b".get(" + m["ue"] + b")||"
    head = old[: m.start("note") - o] + SHORT_NOTE + old[m.end("note") - o : m.start("call") - o] + guard
    pad = len(old) - len(head) - len(b"/*") - len(MARKER) - len(b"*/") - len(m["call"])
    if pad < 0:
        raise RuntimeError(f"replacement is {-pad} bytes longer than the site; shorten SHORT_NOTE")
    new = head + b"/*" + MARKER + b" " * pad + b"*/" + m["call"]
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
        new = build(m)
    except RuntimeError as e:
        print(f"interim-task-notif: {e}", file=sys.stderr)
        return 1
    patched = splice(data, m.start(), m.end(), new)
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
        f"interim-task-notif: applied patch to {binp} (interim notifications from agents the "
        f"main session started are no longer enqueued; pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
