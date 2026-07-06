#!/usr/bin/env python3
"""CLI patch: stop teammates from spamming the lead with per-turn idle pings.

Every teammate gets a "Stop" hook registered at init that fires on EVERY
turn-end and writes an idle_notification to the leader's mailbox:

    n4$(H,$,"Stop","",async(w,M)=>{
        cC$(K,f,!1);                                   // <- local idle-state update (KEEP)
        let j=ry$(f,{idleReason:"available",summary:Kh$(w)});
        return await AY(O,{from:f,text:CH(j),...}),     // <- writes to leader's mailbox (DROP)
               N(`[TeammateInit] Sent idle notification to leader ${O}`),!0
    },"Failed to send idle notification to team leader",{timeout:1e4})

The leader then receives a fake user turn like:
    {"type":"idle_notification","from":"qual-analyst","timestamp":"...","idleReason":"available"}
on every single teammate turn-end. Pure noise — the lead reads idle state via
the team-inbox MCP (which uses the cC$ state map, not this mailbox write).

This patch replaces everything AFTER the first statement (`cC$(...);`, the
local idle-state update we keep) with `return!0;` + a same-length block-comment
carrying a unique marker (`[4l32P patched idle notifs]`) + space padding. The
callback marks the teammate idle locally and returns true, but never writes to
the leader's mailbox. Byte-length is preserved (Bun single-file executable
stores the JS blob with length metadata; a same-length in-place text edit is
the safe move — same technique as task-nag.sh). The marker doubles as the
idempotency signal: grep the whole binary for it (like task-nag greps its
patched string) to know the patch is already applied.

ONLY the per-turn `idleReason:"available"` ping is killed. The separate `dh4`
path (idleReason:"failed" / teammate_terminated) is untouched, so genuine
failure/termination signals still reach the lead.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import apply_patch, candidate_binaries

# Stable, human-readable anchors (no minified identifiers — survive rebuilds).
SUFFIX = b'},"Failed to send idle notification to team leader"'
ARROW = b'=>{'
# Replacement = COMMENT_OPEN + MARKER + space-padding + COMMENT_CLOSE, sized to
# exactly fill the region. MARKER is unique to this patch, so its presence
# anywhere in the binary is the idempotency signal.
COMMENT_OPEN = b'return!0;/*'
COMMENT_CLOSE = b'*/'
MARKER = b'[4l32P patched idle notifs]'
# Sanity tokens that must appear in the un-patched callback body.
REQUIRE = [b'idleReason:"available"', b'Sent idle notification to leader']


def locate_body(data: bytes) -> tuple[int, int]:
    """Return (body_start, body_end) of the Stop-hook arrow body, or raise."""
    n = data.count(SUFFIX)
    if n != 1:
        raise RuntimeError(
            f"expected exactly 1 occurrence of the error-string anchor "
            f"{SUFFIX!r}, found {n} — upstream code changed; re-investigate "
            f"around 'Sent idle notification to leader' in the binary"
        )
    suffix_start = data.index(SUFFIX)
    # body ends at the '}' that begins SUFFIX (closes the arrow body)
    body_end = suffix_start
    # find the arrow '=>{' that opens this body, searching backward within 400 bytes
    window_start = max(0, body_end - 400)
    rel = data.rfind(ARROW, window_start, body_end)
    if rel == -1:
        raise RuntimeError(
            "could not find '=>{' opening the Stop-hook callback within 400 "
            "bytes before the error string — upstream structure changed"
        )
    body_start = rel + len(ARROW)
    return body_start, body_end


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if SUFFIX not in data:
            continue
        try:
            bs, be = locate_body(data)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        if MARKER in data:
            print(f"idle-notif: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        body = data[bs:be]
        target = (binp, data, bs, be, body)
        break

    if target is None:
        print(
            f"anchor {SUFFIX!r} not found in any candidate binary "
            f"({[str(p) for p in candidate_binaries()]}) — upstream code changed "
            f"or unknown install layout; re-investigate around the string "
            f"'Sent idle notification to leader' in the binary",
            file=sys.stderr,
        )
        return 1

    binp, data, bs, be, body = target

    for tok in REQUIRE:
        if tok not in body:
            print(
                f"sanity token {tok!r} missing from the callback body — refusing "
                f"to patch (upstream structure changed). Body was: {body!r}",
                file=sys.stderr,
            )
            return 1

    # Keep up to and including the first statement (the local idle-state update,
    # e.g. cC$(K,f,!1);). Replace the rest (R) with an equal-length no-op.
    semi = body.find(b";")
    if semi == -1:
        print("no ';' in callback body — refusing to patch", file=sys.stderr)
        return 1
    keep = body[: semi + 1]
    R = body[semi + 1 :]

    if b"*/" in R:
        print(
            "the region to neutralize contains '*/' — can't wrap it as a block "
            "comment safely; re-investigate",
            file=sys.stderr,
        )
        return 1

    pad = len(R) - len(COMMENT_OPEN) - len(MARKER) - len(COMMENT_CLOSE)
    if pad < 0:
        print(
            f"region too short ({len(R)} bytes) to hold the marker — refusing",
            file=sys.stderr,
        )
        return 1
    new = COMMENT_OPEN + MARKER + b" " * pad + COMMENT_CLOSE
    assert len(new) == len(R), (len(new), len(R))

    new_body = keep + new
    assert len(new_body) == len(body)
    patched = data[:bs] + new_body + data[be:]
    assert len(patched) == len(data)

    # Write to a temp copy, verify, then atomically swap in (rename-aside on
    # Windows where the running .exe is locked; see _binpatch.apply_patch).
    def _verify(written: bytes) -> None:
        if len(written) != len(data) or MARKER not in written or written.count(SUFFIX) != 1:
            raise RuntimeError(
                f"post-write verification failed "
                f"(len={len(written)} want={len(data)}, marker={MARKER in written}, "
                f"suffix_count={written.count(SUFFIX)}) — live binary untouched"
            )

    apply_patch(binp, data, patched, _verify)

    print(
        f"idle-notif: applied patch to {binp} "
        f"(neutralized {len(R)} bytes; pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
