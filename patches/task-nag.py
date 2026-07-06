#!/usr/bin/env python3
"""CLI patch: disable the "task tools haven't been used recently" reminder.

The reminder (both todo_reminder and task_reminder attachments) is gated by
OC$={TURNS_SINCE_WRITE:10,TURNS_BETWEEN_REMINDERS:10} in the claude binary. Both
conditions must hold, so setting SINCE_WRITE to 1e9 makes it unreachable.
'1e9,...:9' is byte-for-byte the same length as '10,...:10' — a safe in-place
text edit (the Bun single-file executable stores the JS blob with length
metadata; verified 2026-06-10 that the binary executes its embedded JS text, not
bytecode, by patching a --help string and watching the output change).

Ported from task-nag.sh on 2026-07-06 to share _binpatch (shim-following
resolver + Windows rename-aside swap). The bash version silently patched an inert
versions/ copy on Windows — `command -v claude` there missed the live binary and
its `mv` could not overwrite the locked running .exe anyway.

Idempotency: the PATCHED string is unique and doubles as the applied-marker.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import apply_patch, candidate_binaries

PATTERN = b"TURNS_SINCE_WRITE:10,TURNS_BETWEEN_REMINDERS:10"
PATCHED = b"TURNS_SINCE_WRITE:1e9,TURNS_BETWEEN_REMINDERS:9"
assert len(PATTERN) == len(PATCHED), (len(PATTERN), len(PATCHED))


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if PATCHED in data:
            print(f"task-nag: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        if PATTERN in data:
            target = (binp, data)
            break

    if target is None:
        print(
            f"pattern {PATTERN!r} not found in any candidate binary "
            f"({[str(p) for p in candidate_binaries()]}) — upstream code changed "
            f"or unknown install layout; re-investigate around the 'task_reminder' "
            f"string in the binary",
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
        f"task-nag: applied patch to {binp} (pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
