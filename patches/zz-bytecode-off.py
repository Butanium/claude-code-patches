#!/usr/bin/env python3
"""CLI patch (runs LAST): make the other patches' text edits actually execute.

Since Bun 1.4.1 (claude 2.1.250+) every module in the bundle ships with
pre-compiled JSC bytecode, and the loader runs that bytecode without checking
that the embedded JS text still matches it. The same-length text edits every
other patch here makes are therefore dead bytes: they show up in `grep`, the
patch scripts report "already applied", and the process keeps executing the
stock code. Found 2026-09-01 when a new patch had no effect and, on
re-checking, neither did the older ones (teammate messages still carried the
boilerplate `peer-msg-warning.py` strips).

Bun falls back to compiling a module from its text when that module's bytecode
length in the standalone module table is zero (verified: a text-only edit of the
trust-dialog label appeared only once the module's bytecode was disabled; the
5.5 MB main chunk compiles from source with no measurable startup cost).

So this script diffs the live binary against the pristine `.orig` backup that
`_binpatch.apply_patch` keeps next to it, finds every module whose JS text
differs, and zeroes that module's bytecode length. It is deliberately
independent of the individual patches: it fixes text that was patched in an
earlier session (before this script existed), and text left behind by a patch
now parked via CLAUDE_CLI_PATCHES_SKIP. The `zz-` prefix makes the runner's
glob run it after every text patch in the same pass.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if it can't do its job (no `.orig`, graph layout changed) — the runner
relays the message to Claude, and in that state the other patches are inert.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bungraph
from _binpatch import apply_patch, candidate_binaries

CHUNK = 1 << 20


def changed_offsets(a: bytes, b: bytes):
    """Yield the file offset of every differing byte (a and b same length),
    comparing in 1 MiB chunks so identical regions cost one memcmp each."""
    for start in range(0, len(a), CHUNK):
        ca, cb = a[start : start + CHUNK], b[start : start + CHUNK]
        if ca == cb:
            continue
        for i, (x, y) in enumerate(zip(ca, cb)):
            if x != y:
                yield start + i


def main() -> int:
    cands = candidate_binaries()
    if not cands:
        print("no claude binary found (unknown install layout)", file=sys.stderr)
        return 1
    binp = cands[0]
    orig = binp.with_name(binp.name + ".orig")
    if not orig.is_file():
        print(
            f"zz-bytecode-off: no pristine backup at {orig} — nothing to compare against. "
            f"It is created by the first patch applied to this binary; if patches ARE "
            f"applied but the backup is gone, reinstall claude and re-run the patches.",
            file=sys.stderr,
        )
        return 1
    data = binp.read_bytes()
    stock = orig.read_bytes()
    if len(data) != len(stock):
        print(
            f"zz-bytecode-off: {binp} ({len(data)} B) and {orig} ({len(stock)} B) differ in "
            f"size — the backup is from another version; refusing to compare",
            file=sys.stderr,
        )
        return 1

    try:
        graph = _bungraph.parse(data)
    except RuntimeError as e:
        print(f"zz-bytecode-off: {e} — text patches are INERT until _bungraph.py is fixed", file=sys.stderr)
        return 1

    # Modules whose JS text was edited by some patch. Diffs inside the module
    # table itself (this script's own earlier edits) belong to no module and are
    # skipped naturally.
    touched: dict[int, _bungraph.Module] = {}
    for off in changed_offsets(stock, data):
        m = graph.module_for(off)
        if m is not None:
            touched.setdefault(m.index, m)

    buf = bytearray(data)
    disabled = [m for m in touched.values() if _bungraph.disable_bytecode(buf, m)]
    already = [m for m in touched.values() if m.bytecode[1] == 0]
    if not disabled:
        print(
            f"zz-bytecode-off: confirmed — {len(touched)} patched module(s) already run from "
            f"source ({binp})",
            file=sys.stderr,
        )
        return 0

    patched = bytes(buf)

    def _verify(written: bytes) -> None:
        if len(written) != len(data):
            raise RuntimeError("post-write verification failed (length) — live binary untouched")
        g2 = _bungraph.parse(written)
        for m in disabled:
            if g2.modules[m.index].bytecode[1] != 0:
                raise RuntimeError("post-write verification failed (bytecode still on) — live binary untouched")

    apply_patch(binp, data, patched, _verify)
    names = ", ".join(m.name.decode(errors="replace").rsplit("/", 1)[-1] for m in disabled)
    print(
        f"zz-bytecode-off: disabled bytecode for {len(disabled)} patched module(s) [{names}]"
        + (f", {len(already)} already off" if already else "")
        + f" ({binp})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
