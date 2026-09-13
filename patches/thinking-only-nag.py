#!/usr/bin/env python3
"""CLI patch: allow a turn to end with no user-visible text.

When a turn ends (`stop_reason` end_turn/stop_sequence) and no assistant text
block has non-empty trimmed content, the query loop injects

    [Your previous response had no visible output. Please continue and produce
    a user-visible response.]

as an isMeta message and re-runs the turn once (telemetry
`query_thinking_only_response` / "nudged"; the retry is capped by the
`thinkingOnlyNudged` flag, so it fires at most once per turn):

    if(!kr){g("query_thinking_only_response","nudged");
     let _l=Ie({content:hft,isMeta:!0,turnCompanion:!0,...});
     yield _l,Oe={...,thinkingOnlyNudged:!0,transition:{reason:"thinking_only_retry"}};
     continue}
    f("query_thinking_only_response","nudge_exhausted")

There is no environment variable or setting gating this. In particular
CLAUDE_CODE_SILENT_TURN_REMINDER is NOT it — that one is a triBool gating
`tengu_hushed_lark`, the unrelated "The user hasn't heard from you in a while"
nudge, via Btr(). Setting it to "0" correctly disables hushed_lark and has no
effect here. That mismatch is why this patch exists.

Why turn it off: CLAUDE.md on this box instructs Claude to end a turn silently
when a notification is no longer relevant (a timer for a job that already
finished), explicitly "Zero text tokens". The nag makes that instruction
unfollowable — the model complies, the loop rejects the empty turn and demands
prose, and the user gets a paragraph of noise about a stale timer. Since the
gate cannot distinguish a deliberate silent turn from an accidental
thinking-only one, the choice is all or nothing.

Tradeoff, stated plainly: with this applied, a turn that ends with only a
thinking block is delivered as-is. If the model ever does that by mistake the
user sees nothing rather than a retried answer.

The edit neutralizes the guard rather than removing the branch, so the
surrounding control flow (the `f(...,"nudge_exhausted")` telemetry call, the
`else if(kr)` arm, and every state field threaded through `Oe`) is untouched
and the byte length is preserved. `!kr` becomes `!1`; the freed byte is padded
with a space before the `)`, which is outside any string literal.

`kr` is a minified identifier and is renamed on every build, so the pattern is
built from a regex anchored on the stable telemetry string
`"query_thinking_only_response","nudged"` (one occurrence in 2.1.257 and in
2.1.270) and the identifier is read out of the match. So is the telemetry
emitter in front of it — hardcoding that as `g(` is what broke this patch on
2.1.270, where the same import minified to `h(`.

Idempotency: the patched guard `if(!1` in front of that same telemetry string is
unique and doubles as the applied-marker.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries

ANCHOR = b'"query_thinking_only_response","nudged"'
# The telemetry emitter is itself a minified import (`g(` in 2.1.257, `h(` in
# 2.1.270), so it is matched as an identifier rather than hardcoded.
GUARD_RX = re.compile(
    rb"if\(!(" + JSID + rb")\)\{" + JSID + rb'\("query_thinking_only_response","nudged"\)'
)
PATCHED_RX = re.compile(
    rb"if\(!1 *\)\{" + JSID + rb'\("query_thinking_only_response","nudged"\)'
)


def build_replacement(pattern: bytes, ident: bytes) -> bytes:
    """`if(!kr){g(...)` -> `if(!1 ){g(...)`, same byte length."""
    pad = len(ident) - 1
    if pad < 0:
        raise RuntimeError(f"identifier {ident!r} shorter than the literal it replaces")
    replacement = pattern.replace(b"if(!" + ident + b")", b"if(!1" + b" " * pad + b")", 1)
    if len(replacement) != len(pattern):
        raise RuntimeError(
            f"length drift: {len(pattern)} -> {len(replacement)}; refusing to patch"
        )
    return replacement


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if PATCHED_RX.search(data):
            print(f"thinking-only-nag: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        if GUARD_RX.search(data):
            target = (binp, data)
            break

    if target is None:
        print(
            "guard `if(!<ident>){g(\"query_thinking_only_response\",\"nudged\")` not found "
            f"in any candidate binary ({[str(p) for p in candidate_binaries()]}) — "
            "upstream code changed or unknown install layout. Re-investigate by "
            f"grepping for {ANCHOR!r} (the telemetry name is the stable anchor; the "
            "surrounding identifier is minified and is renamed every build), then read "
            "the enclosing `if(stop_reason===\"end_turn\" && ...no non-empty text block...)` "
            "block in the query loop.",
            file=sys.stderr,
        )
        return 1

    binp, data = target
    matches = GUARD_RX.findall(data)
    if len(matches) != 1:
        print(
            f"expected exactly 1 occurrence of the guard, found {len(matches)} in {binp} "
            "— upstream code changed; refusing to patch",
            file=sys.stderr,
        )
        return 1

    m = GUARD_RX.search(data)
    assert m is not None
    pattern = m.group(0)
    replacement = build_replacement(pattern, m.group(1))

    patched = data.replace(pattern, replacement, 1)
    if len(patched) != len(data):
        print("length changed after replace — refusing to patch", file=sys.stderr)
        return 1

    def _verify(written: bytes) -> None:
        if (
            len(written) != len(data)
            or not PATCHED_RX.search(written)
            or GUARD_RX.search(written)
        ):
            raise RuntimeError("post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)

    print(
        f"thinking-only-nag: applied patch to {binp} (pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
