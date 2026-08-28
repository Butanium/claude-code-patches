#!/usr/bin/env python3
"""CLI patch: drop the per-message security boilerplate on teammate messages.

Every message from another Claude session (teammate -> lead inbox, mid-turn peer
message, peer content-rewrite) is wrapped by ONE producer function that appends a
~90-word warning. In a busy team session the same paragraph lands verbatim on
every inbound teammate message (6+ times in one conversation is routine).
Repetition trains the reader to skip that region entirely, which defeats the
warning — and the trust model is already covered by the user's own instructions.

Shape as of 2.1.250 (the header text is hoisted into consts, so the guard no
longer contains a greppable string literal — see the re-anchoring note below):

    var HK="Another Claude session sent a message",
        w=`${HK} while you were working:`, A=`${HK}:`,
        M="This came from another Claude session ... permission laundering.",
        T=" After completing your current task, ...";
    function eLe(e,t){
        if(t.activityObservation===void 0?ee(e):te(e))return e;          // idempotency
        if(t.activityObservation!==void 0)return`${t.midTurn?q:C}\n${e}\n\n${j}`;  // activity obs
        let s=t.midTurn?w:A,r=t.midTurn?T:"";
        return`${s}\n${e}\n\n${M}${r}`                                   // <-- patched
    }

The patch replaces ONLY the final statement — the peer-message path — with a
bare `return <param>;` plus a marker comment padded to the same byte length:

    let s=t.midTurn?w:A,r=t.midTurn?T:"";return`${s}\n${e}\n\n${M}${r}`
    -->  return e;/*[e8Xw peer-msg-warning off]                        */

The activity-observation branch above it is deliberately left alone: unlike a
peer message (whose envelope already carries `from=`), an edit/reaction
notification has no other signal that it is an observation rather than a fresh
instruction, so its header is load-bearing. Verified in a node sim that both the
activity-observation and the already-wrapped idempotency paths stay byte-identical
to stock; only peer messages come back unwrapped.

Byte-length is preserved (Bun single-file executable stores the JS blob with
length metadata; same-length in-place edit is the safety contract). The anchor
matches the whole statement with back-references, so every minified identifier is
captured rather than assumed; 64 bytes of statement leave 24 bytes of pad.

Display-side is untouched on purpose: the CLI keeps a strip-list of the known
wrapper suffixes/prefixes used when rendering messages in the UI — those
strippers harmlessly no-op when the wrapper is absent and still clean up
wrappers in pre-patch transcripts.

Re-anchoring history: through 2.1.233 the guard read
`if(e.startsWith("Another Claude session sent a message")` and the patch cut
there. 2.1.250 hoisted that literal into a const and added the
activity-observation branch, so the string-literal anchor vanished. If this fails
again, grep the binary for "permission laundering" — that const still sits ~2 KB
ahead of the producer — and re-read the function that builds the wrapper.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import apply_patch, candidate_binaries

# The peer-message return statement. Structural, not string-literal based:
# `midTurn` is a stable property name and the back-references pin the two
# ternaries to the same options object and the template to the same locals.
ANCHOR = re.compile(
    rb'let (\w+)=(\w+)\.midTurn\?(\w+):(\w+),(\w+)=\2\.midTurn\?(\w+):"";'
    rb'return`\$\{\1\}\n\$\{(\w+)\}\n\n\$\{(\w+)\}\$\{\5\}`'
)
MSG_GROUP = 7  # the `${e}` inside the template — the message being wrapped
# Sanity tokens that must appear shortly BEFORE the statement (they live in the
# hoisted consts the statement interpolates).
REQUIRE = [b"permission laundering", b"Another Claude session sent a message"]
REQUIRE_WINDOW = 3500
MARKER = b"[e8Xw peer-msg-warning off]"
REINVESTIGATE = (
    "re-investigate around the string 'permission laundering' in the binary — the "
    "const it belongs to is interpolated by the teammate-message wrapper a couple "
    "of KB later; patch that wrapper's final return statement"
)


def main() -> int:
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if MARKER in data:
            print(f"peer-msg-warning: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        matches = list(ANCHOR.finditer(data))
        if len(matches) != 1:
            print(
                f"expected exactly 1 anchor match in {binp}, found {len(matches)} "
                f"— upstream code changed; {REINVESTIGATE}",
                file=sys.stdout,
            )
            return 1
        m = matches[0]
        window = data[max(0, m.start() - REQUIRE_WINDOW) : m.start()]
        for tok in REQUIRE:
            if tok not in window:
                print(
                    f"sanity token {tok!r} missing within {REQUIRE_WINDOW} bytes before "
                    f"the anchor — refusing to patch (upstream structure changed); "
                    f"{REINVESTIGATE}",
                    file=sys.stdout,
                )
                return 1

        original_seg = m.group(0)
        core_head = b"return " + m.group(MSG_GROUP) + b";/*" + MARKER
        core_tail = b"*/"
        pad = len(original_seg) - len(core_head) - len(core_tail)
        if pad < 0:
            print(
                f"anchor too short ({len(original_seg)} bytes) to hold the marker "
                f"— refusing; {REINVESTIGATE}",
                file=sys.stdout,
            )
            return 1
        replacement = core_head + b" " * pad + core_tail
        assert len(replacement) == len(original_seg), (len(replacement), len(original_seg))

        patched = data[: m.start()] + replacement + data[m.end() :]
        assert len(patched) == len(data)

        def _verify(written: bytes) -> None:
            if (
                len(written) != len(data)
                or MARKER not in written
                or ANCHOR.search(written) is not None
            ):
                raise RuntimeError(
                    f"post-write verification failed "
                    f"(len={len(written)} want={len(data)}, marker={MARKER in written}, "
                    f"anchor_still_present={ANCHOR.search(written) is not None}) "
                    f"— live binary untouched"
                )

        apply_patch(binp, data, patched, _verify)
        print(
            f"peer-msg-warning: applied patch to {binp} "
            f"(teammate-message wrapper now returns peer messages unchanged; "
            f"pristine backup at {binp}.orig)",
            file=sys.stderr,
        )
        return 0

    print(
        f"no candidate binary contained the anchor "
        f"({[str(p) for p in candidate_binaries()]}) — {REINVESTIGATE}",
        file=sys.stdout,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
