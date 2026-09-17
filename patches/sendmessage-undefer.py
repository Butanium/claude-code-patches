#!/usr/bin/env python3
"""CLI patch: load the SendMessage tool up front instead of behind ToolSearch.

SendMessage ships deferred, so an agent that wants to talk to a teammate must
first spend a turn on `ToolSearch select:SendMessage`. That cost lands exactly
where it hurts most: the first thing a lead does after spawning teammates is
message them, and the first thing a teammate does on waking is reply. Deferral
also makes the tool invisible to a model deciding *whether* to delegate at all —
the capability has to be remembered rather than seen.

Same mechanism as monitor-undefer.py. SendMessage's tool object sets
`shouldDefer:!0` inline (`Et({name:eo,searchHint:"send messages to agent
teammates",...,shouldDefer:!0,isReadOnly(e){...}` in 2.1.257's
chunk-yttmavg3.js). The deferral predicate is
`function npe(e){if(e.alwaysLoad===!0)return!1;if(jQn(e))return!1;if(e.isMcp===!0)return!tpe();return e.shouldDefer===!0}`,
and `.shouldDefer` is read at that one site and nowhere else in the bundle, so
flipping the flag to `!1` is both sufficient and side-effect-free — and
`!0`->`!1` is a same-length edit, which the Bun single-file executable requires.

Cost: SendMessage's description + input schema in every request of every
session, teams or not — measured at ~5.2 KB of text, roughly 1.3k tokens.

Why not the config route: `npe` consults an allowlist, `Atr()`, built from
`tengu_non_deferrable_builtins` (a feature value holding a list of tool names,
default empty) plus a server-delivered `non_deferrable_builtins`. Neither is
reachable locally in 2.1.257: the only local override path,
`getEnvironmentOverrides()` / `CLAUDE_INTERNAL_FC_OVERRIDES`, has been
dead-coded upstream — it returns unconditionally before the parse
(`return this.environmentOverridesParsed=!0,this.environmentOverrides;` with the
parsing code unreachable below it), and its sibling `readConfigOverrides()` is
an empty `return`. Reviving that early return is the patch to write if several
more builtins ever need undeferring; for one tool it is a far wider blast radius
than a flag flip.

Anchoring: `shouldDefer:!0` appears 46 times in the binary, so the pattern
carries the neighbouring property that identifies SendMessage specifically
(`isReadOnly` testing `e.message`). No minified identifiers are included — those
are renamed every build; the `e` parameter name is the minifier's first choice
and far more stable. To re-derive after an update:
`scripts/cli-patches/clisrc.py --find 'searchHint:"send messages to agent'`
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

PATTERN = b'shouldDefer:!0,isReadOnly(e){return typeof e.message'
PATCHED = b'shouldDefer:!1,isReadOnly(e){return typeof e.message'
assert len(PATTERN) == len(PATCHED), (len(PATTERN), len(PATCHED))


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if PATCHED in data:
            print(f"sendmessage-undefer: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        if PATTERN in data:
            target = (binp, data)
            break

    if target is None:
        print(
            f"pattern {PATTERN!r} not found in any candidate binary "
            f"({[str(p) for p in candidate_binaries()]}) — upstream code changed "
            f"or unknown install layout; re-investigate by unpacking the bundle "
            f"(scripts/cli-patches/clisrc.py --find 'searchHint:\"send messages to agent') "
            f"and reading the SendMessage tool literal for its shouldDefer flag",
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
        f"sendmessage-undefer: applied patch to {binp} (pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
