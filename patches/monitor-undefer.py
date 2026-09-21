#!/usr/bin/env python3
"""CLI patch: load the Monitor tool up front instead of behind ToolSearch.

Monitor ships deferred, so a session that wants to arm a watcher must first
spend a turn on `ToolSearch select:Monitor`. That was 100% of the sessions that
used Monitor in the 2026-09-14 whowill A/Bs — one wasted tool call per
background job, and every hint that mentions Monitor has to carry a line telling
the model to fetch the schema first. The deferral is the generic token-budget
one, not a "keep this out of reach" gate: the tool's own description already
says to arm it and keep working.

Monitor's tool object spreads a base literal that carries `shouldDefer:!0`
(`fe={name:ca,enablesCodeExecution:!0,maxResultSizeChars:1e4,shouldDefer:!0,...}`
in 2.1.257's chunk-fb2mwnxw.js). The deferral predicate is
`function npe(e){if(e.alwaysLoad===!0)return!1; ... return e.shouldDefer===!0}`,
so flipping the flag to `!1` is enough — and `!0`->`!1` is a same-length edit,
which the Bun single-file executable requires (the JS blob is stored with length
metadata).

Cost: Monitor's schema text in every request, for every session.

Anchoring: `shouldDefer:!0` appears 46 times in the binary, so the pattern
carries the neighbouring property names for uniqueness. Those are structural
(never minified); only parameter names would churn across releases, and they
are deliberately left out of the pattern. The property *after* the flag has
moved once already — `permissionCheckFailureDecision` through 2.1.257,
`userFacingName(){return"Monitor"}` from 2.1.271 (`De={name:za,
enablesCodeExecution:!0,maxResultSizeChars:1e4,shouldDefer:!0,userFacingName()
{return"Monitor"},...}` in 2.1.278's chunk-sdv516s1.js) — so the regex accepts
either. To re-derive after an update:
`scripts/cli-patches/clisrc.py --find 'searchHint:"watch, monitor'` locates the
Monitor tool literal, and the base object it spreads holds the flag.

Idempotency: the patched shape is unique and doubles as the applied-marker.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import apply_patch, candidate_binaries

_NEXT_PROP = rb'(permissionCheckFailureDecision|userFacingName\(\)\{return"Monitor"\})'
PATTERN = re.compile(rb"maxResultSizeChars:1e4,shouldDefer:!0," + _NEXT_PROP)
PATCHED = re.compile(rb"maxResultSizeChars:1e4,shouldDefer:!1," + _NEXT_PROP)


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if PATCHED.search(data):
            print(f"monitor-undefer: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        if PATTERN.search(data):
            target = (binp, data)
            break

    if target is None:
        print(
            f"pattern {PATTERN.pattern!r} not found in any candidate binary "
            f"({[str(p) for p in candidate_binaries()]}) — upstream code changed "
            f"or unknown install layout; re-investigate by unpacking the bundle "
            f"(scripts/cli-patches/clisrc.py --find 'searchHint:\"watch, monitor') "
            f"and reading the base object the Monitor tool spreads for its "
            f"shouldDefer flag",
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
    new = m.group(0).replace(b"shouldDefer:!0", b"shouldDefer:!1")
    assert len(new) == len(m.group(0))
    patched = data[: m.start()] + new + data[m.end() :]
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        if len(written) != len(data) or not PATCHED.search(written) or PATTERN.search(written):
            raise RuntimeError("post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)

    print(
        f"monitor-undefer: applied patch to {binp} (pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
