#!/usr/bin/env python3
"""CLI patch: give the Monitor tool its `persistent` option back.

2.1.271 put every Monitor watch on a deadline: at most 30 minutes (10 in a
single-prompt `-p` run), after which the watch is killed and the model gets one
"[Monitor expired after 30 minutes with N events delivered. Re-arm it if you
still need the watch.]" notice. The `persistent: true` option that used to mean
"run until TaskStop or session end" was not rejected but *dropped from the
input schema*, so a call that still passes it is silently accepted and capped.

That breaks the shape every long job on this box relies on: one `bgwatch`
Monitor armed at launch, alive for the hours the job runs, waking the model
only on failure lines, progress markers and a backing-off heartbeat. A 30-min
cap turns that into a re-arm chore the model has to remember across
compactions — the notice names no command, so after a compaction the watch is
simply gone.

The change is a feature flag, not a rewrite. Both code paths are still in the
bundle and one function chooses between them:

    function v3(){return ql("tengu_breezy_crescent",!0)}      // 2.1.278

`ql` is the GrowthBook flag getter with a default; the default is `!0`, so the
cap is on even when the flag service is unreachable. Its seven callers all go
through this one function: the input-schema selector (`v3()?Te():Ce()` — `Ce`
is the legacy strict schema that still declares `persistent`), the argument
normalizer that forces `persistent:!1`, the `bounded` flag handed to the
watcher runtime, the expiry-notice builder, the tool description, the system
prompt's Monitor paragraph and the prompt-cache key bit. Making the function
return `!1` restores the pre-2.1.271 behavior everywhere at once, including
the description text the model reads.

The edit keeps the identifier and the flag name and is length-neutral for
any identifier length:

    return ql("tengu_breezy_crescent",!0)}     stock
    return!1&&ql("tengu_breezy_crescent")}     patched

`!1&&` short-circuits, so the getter is never called and a remote flag value
cannot override the result (flipping only the default would leave that door
open). The flag string survives in the binary for whoever re-investigates,
and the `!1&&` prefix in front of it is the idempotency marker.

To re-derive after an update: `scripts/cli-patches/clisrc.py --find
'tengu_breezy_crescent'` shows the gate function; check that its callers still
select between two schemas (the legacy one carries `persistent:` in its
`describe` text) before assuming the flip is enough.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries

FLAG = b'"tengu_breezy_crescent"'
PATTERN = re.compile(rb"return (" + JSID + rb")\(" + re.escape(FLAG) + rb",!0\)\}")
PATCHED = re.compile(rb"return!1&&(" + JSID + rb")\(" + re.escape(FLAG) + rb"\)\}")
# The legacy schema's `persistent` field description. Present with no flag at
# all = a binary from before 2.1.271, where there is nothing to restore.
LEGACY_TEXT = b"Run for the lifetime of the session (no timeout)"


def replacement(m: re.Match[bytes]) -> bytes:
    rep = b"return!1&&" + m.group(1) + b"(" + FLAG + b")}"
    assert len(rep) == len(m.group(0)), (rep, m.group(0))
    return rep


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if PATCHED.search(data):
            print(f"monitor-persistent: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        if PATTERN.search(data):
            target = (binp, data)
            break
        if FLAG not in data and LEGACY_TEXT in data:
            print(
                f"monitor-persistent: not needed — {binp} predates the Monitor "
                f"deadline flag (persistent is still stock behavior)",
                file=sys.stderr,
            )
            return 0

    if target is None:
        print(
            f"pattern {PATTERN.pattern!r} not found in any candidate binary "
            f"({[str(p) for p in candidate_binaries()]}) — upstream code changed "
            f"or unknown install layout; re-investigate by unpacking the bundle "
            f"(scripts/cli-patches/clisrc.py --find 'tengu_breezy_crescent') and "
            f"reading the gate function that selects between the two Monitor "
            f"schemas — or the flag is gone and the legacy path with it, in which "
            f"case this patch has nothing left to restore",
            file=sys.stderr,
        )
        return 1

    binp, data = target
    hits = list(PATTERN.finditer(data))
    if len(hits) != 1:
        print(
            f"expected exactly 1 occurrence of the pattern, found {len(hits)} in {binp} "
            f"— upstream code changed; refusing to patch",
            file=sys.stderr,
        )
        return 1

    m = hits[0]
    new = replacement(m)
    patched = data[: m.start()] + new + data[m.end() :]
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        if len(written) != len(data) or not PATCHED.search(written) or PATTERN.search(written):
            raise RuntimeError("post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)

    print(
        f"monitor-persistent: applied patch to {binp} ({m.group(0)!r} -> {new!r}; "
        f"pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
