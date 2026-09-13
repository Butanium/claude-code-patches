#!/usr/bin/env python3
"""CLI patch: don't ping the lead when the user interrupts an in-process teammate.

When the user stops an in-process teammate (Escape / TaskStop), the runner
returns the teammate to idle and mails the lead an idle_notification (shape as
of 2.1.270):

    let Ae=se?.reason;
    if(xe){let r=bTe(O,{emitTelemetry:!ae}),f=ae?void 0:r.result,
        E=await we(e.agentName,e.color,e.teamName,
          {idleReason:ae?"interrupted":Ae!==void 0?"failed":"available",...});
      ...}
    else t(`[inProcessRunner] Skipping duplicate idle notification for ...`);

`ae` (`let ae=Ce||I.signal.aborted`) is the "current work aborted (Escape
pressed)" flag; `xe` is the not-already-idle guard. The resulting
    {"type":"idle_notification","from":"...","idleReason":"interrupted"}
turn in the lead's transcript is pure noise: the user did the interrupting,
and the lead separately learns the task state from the harness. (This is the
in-process-runner sibling of idle-notif.py, which kills the tmux Stop-hook
"available" pings.)

The edit gates the send on the abort flag and drops the now-dead ternary head:

    if(xe&&!ae){let r=...,E=await we(...,{idleReason:/*hFq3nInt*/Ae!==void 0?...});

Interrupted -> nothing is sent (the else-branch just debug-logs, and its
"duplicate" wording is now also reached on a genuine interrupt — a debug line,
not model- or user-facing). The "failed" and "available" sends, and the local
idle-state bookkeeping around the call, are untouched. The abort flag still
appears inside the block (`emitTelemetry:!ae`, `f=ae?void 0:r.result`) but is
always false there now, so telemetry and `result` delivery for the
"failed"/"available" cases are exactly stock. Byte-length is preserved:
removing `<ae>?"interrupted":` frees len(ae)+15 bytes; inserting `&&!<ae>`
costs len(ae)+3; the constant 12-byte remainder is exactly the marker comment
`/*hFq3nInt*/`, whose payload `hFq3nInt` is the idempotency signal (grep the
binary for it).

Re-investigation anchor if this stops applying after an update: the regex below
around `idleReason:(\\w+)\\?"interrupted":`, with the debug string
'Skipping duplicate idle notification' just after the call site.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries

MARKER = b"hFq3nInt"
COMMENT = b"/*" + MARKER + b"*/"
assert len(COMMENT) == 12

# The send site, with every minified identifier captured so the rewrite works
# across renames; the shape (agentName/color/teamName positional args + the
# idleReason ternary) is the stable part. The send sits inside a block that
# first computes the result to deliver (`{let r=bTe(O,{emitTelemetry:!ae}),
# f=ae?void 0:r.result,E=await we(...`); that prefix is captured as one group
# and re-emitted verbatim.
#
# 2.1.270 collapsed the two-flag guard (`if(!Ve&&!y)`) into one hoisted local
# (`let xe=!(De?.type==="in_process_teammate"&&De.isIdle)&&!l; ... if(xe){`),
# so the guard is matched as a single identifier and `&&!<abort>` is appended
# to it.
SITE = re.compile(
    rb"if\((" + JSID + rb")\)(\{let " + JSID + rb"=.{0,400}?" + JSID + rb"=await "
    + JSID + rb"\((" + JSID + rb")\.agentName,\3\.color,\3\.teamName,"
    rb'\{idleReason:)(' + JSID + rb')\?"interrupted":',
    re.DOTALL,
)
# Debug string that must sit shortly after the call — proves we're at the
# in-process-runner site and not some future lookalike.
NEARBY = b"Skipping duplicate idle notification"
NEARBY_WINDOW = 400


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if MARKER in data:
            print(
                f"interrupted-idle-notif: confirmed already patched ({binp})",
                file=sys.stderr,
            )
            return 0
        matches = list(SITE.finditer(data))
        if not matches:
            continue
        if len(matches) != 1:
            print(
                f"expected exactly 1 send-site match, found {len(matches)} in "
                f"{binp} — upstream code changed; re-investigate around "
                f'`idleReason:X?"interrupted":` in the binary',
                file=sys.stderr,
            )
            return 1
        target = (binp, data, matches[0])
        break

    if target is None:
        print(
            f"send site not found in any candidate binary "
            f"({[str(p) for p in candidate_binaries()]}) — upstream code "
            f"changed or unknown install layout; re-investigate around "
            f'`idleReason:X?"interrupted":` / "Skipping duplicate idle '
            f'notification" in the binary',
            file=sys.stderr,
        )
        return 1

    binp, data, m = target

    if NEARBY not in data[m.end() : m.end() + NEARBY_WINDOW]:
        print(
            f"sanity string {NEARBY!r} not within {NEARBY_WINDOW} bytes after "
            f"the send site — refusing to patch (upstream structure changed). "
            f"Site was: {data[m.start():m.end() + 120]!r}",
            file=sys.stderr,
        )
        return 1

    guard, prefix, _agent_obj, abort = m.groups()
    new = b"if(" + guard + b"&&!" + abort + b")" + prefix + COMMENT
    old = data[m.start() : m.end()]
    assert len(new) == len(old), (len(new), len(old))

    patched = data[: m.start()] + new + data[m.end() :]
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        if (
            len(written) != len(data)
            or MARKER not in written
            or SITE.search(written) is not None
        ):
            raise RuntimeError(
                f"post-write verification failed "
                f"(len={len(written)} want={len(data)}, "
                f"marker={MARKER in written}, "
                f"site_still_present={SITE.search(written) is not None}) — "
                f"live binary untouched"
            )

    apply_patch(binp, data, patched, _verify)

    print(
        f"interrupted-idle-notif: applied patch to {binp} "
        f"(gated the send on the abort flag; pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
