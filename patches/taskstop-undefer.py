#!/usr/bin/env python3
"""CLI patch: load the TaskStop tool up front instead of behind ToolSearch.

TaskStop ships deferred, so cancelling a background task — a runaway Bash job, a
Monitor that is firing too often, an agent that should stop — costs a
`ToolSearch select:TaskStop` turn first. That is the wrong shape for this tool
specifically: the moment you want it is the moment something is already going
wrong, and the deferral puts a round-trip between noticing and stopping it.
It also makes the capability invisible to a model deciding whether a background
launch is recoverable at all.

Same mechanism as monitor-undefer.py / sendmessage-undefer.py. The TaskStop tool
literal sets `shouldDefer:!0` inline (`Et({name:l_,searchHint:"kill a running
background task",aliases:["KillShell","KillBash"],...}` in 2.1.257's
chunk-9rvw4eh9.js). The deferral predicate reads `.shouldDefer===!0` at exactly
one site in the bundle (verified: 1 occurrence), so flipping the flag to `!1` is
both sufficient and side-effect-free — and `!0`->`!1` is a same-length edit,
which the Bun single-file executable requires.

Cost: TaskStop's description + input schema in every request of every session.
It is a small tool (a task_id and a reason), so this is the cheapest of the
three undefer patches.

Not patched: TaskOutput, the sibling in the same deferred pair. Its own
description in the binary reads "[Deprecated] — for bash and remote_agent tasks,
prefer Read on the output file path; for local_agent tasks, use the Agent tool
result directly", so paying schema tokens every request to surface a tool
upstream is steering away from isn't worth it. Its anchor, if that ever changes:
`shouldDefer:!0,aliases:["AgentOutputTool"` (1 occurrence in 2.1.257).

Anchoring: `shouldDefer:!0` appears 46 times in the binary, so the pattern
carries the two neighbouring members that identify TaskStop specifically — the
`task_id??shell_id` classifier input is unique to this tool. No minified
identifiers are included (the schema getters next door name `HMo`/`jMo`, which
are renamed every build); only the `e` parameter name, which is the minifier's
first choice and far more stable. To re-derive after an update:
`scripts/cli-patches/clisrc.py --find 'searchHint:"kill a running background'`
locates the tool literal and its shouldDefer flag.

Idempotency: the PATCHED string is unique and doubles as the applied-marker.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import apply_patch, candidate_binaries

PATTERN = b'shouldDefer:!0,isConcurrencySafe(){return!0},toAutoClassifierInput(e){return e.task_id'
PATCHED = b'shouldDefer:!1,isConcurrencySafe(){return!0},toAutoClassifierInput(e){return e.task_id'
assert len(PATTERN) == len(PATCHED), (len(PATTERN), len(PATCHED))


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if PATCHED in data:
            print(f"taskstop-undefer: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        if PATTERN in data:
            target = (binp, data)
            break

    if target is None:
        print(
            f"pattern {PATTERN!r} not found in any candidate binary "
            f"({[str(p) for p in candidate_binaries()]}) — upstream code changed "
            f"or unknown install layout; re-investigate by unpacking the bundle "
            f"(scripts/cli-patches/clisrc.py --find 'searchHint:\"kill a running background') "
            f"and reading the TaskStop tool literal for its shouldDefer flag",
            file=sys.stderr,
        )
        return 1

    binp, data = target
    n = data.count(PATTERN)
    if n != 1:
        print(
            f"expected exactly 1 occurrence of the pattern, found {n} in {binp} "
            f"— upstream code changed; refusing to patch",
            file=sys.stderr,
        )
        return 1

    patched = data.replace(PATTERN, PATCHED)
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        if len(written) != len(data) or PATCHED not in written or PATTERN in written:
            raise RuntimeError("post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)

    print(
        f"taskstop-undefer: applied patch to {binp} (pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
