#!/usr/bin/env python3
"""CLI patch (EXPERIMENTAL): let /rewind target teammate messages, not just yours.

Scripts in experimental/ are NOT run by run_cli_patches.sh. They are meant to be
applied to a throwaway copy of the bundle built by ../make-expclaude.sh, driven
via $CLAUDE_CLI_PATCH_TARGET, so a half-baked patch can be lived with for a while
before it goes anywhere near the CLI you actually work in.

WHAT /rewind SHOWS TODAY

The checkpoint list is literally `messages.filter(a5e)`, and `a5e` is:

    function a5e(e){
        if(!eqe(e))return!1;
        if(e.origin&&e.origin.kind!=="human")return!1;    // <- gate B
        if(e.stackedExpansion)return!1;
        return!0
    }
    function eqe(e){
        if(e.type!=="user")return!1;
        if(Array.isArray(e.message.content)&&e.message.content[0]?.type==="tool_result")return!1;
        if(Mbe(e))return!1;
        if(e.isMeta)return!1;                             // <- gate A
        ...
    }

A message from another Claude session lands in the transcript as

    {"type":"user","isMeta":true,
     "origin":{"kind":"peer","from":"...","name":"...","body":"<what they said>"}}

so it is rejected twice — once by gate A, once by gate B — and you can only ever
rewind to a message you typed yourself. If a teammate dumped 40k tokens into your
context, the nearest checkpoint is whatever you said before that, which throws
away your own turns too.

WHAT THIS PATCH DOES

1. Blanks gate A and gate B (same-length: comment marker + spaces). This is
   deliberately a superset of "allow peer": every user-role message that isn't a
   tool result / compact summary / hook-tag blob becomes a checkpoint, so task
   notifications and observer messages become rewind targets as well. Allowing
   *only* kind==="peer" costs ~30 bytes with no local slack to pay them from,
   and the extra entries are ones you'd plausibly want to rewind past anyway.

2. Makes the rows readable. Each row renders one truncated line of the message
   text, and every peer message starts with the same wrapper preamble
   ("Another Claude session sent a message:"), so without this the list is N
   identical rows. `origin.body` holds the teammate's raw text, so the row
   renderer prefers it:

       let BoD=wP(lNl)?.trim()||"(no prompt)";
    -> let BoD=lNl.origin?.body||wP(lNl)?.trim()||"(no prompt)";

   Paid for by dropping `flexDirection:"row",` from the "((empty message))"
   branch a few bytes later — Ink's Box already defaults to row, so that prop is
   a no-op and its 20 bytes are free. (Next 10 bytes, if a future rebuild renames
   identifiers longer and this stops fitting: `italic:!0,` in the same branch.)

BLAST RADIUS (all call sites checked on 2.1.226)

  a5e -> SXs (userPromptCount): re-checks isMeta and origin itself, unaffected.
  a5e -> the SDK `rewind_conversation` control path, where it decides whether a
         rewind target is stale. Loosening makes remote rewind refuse in more
         cases (a peer message after the target now counts as "stale target").
  eqe -> a transcript filter that has its own `!isMeta` guard, unaffected.
  eqe -> a "does this session have any content" check in the fork-context
         builder, which gets slightly more permissive.

KNOWN WART, DELIBERATELY LEFT

Restoring prefills the prompt box with the rewound message's text ($Ta), so
rewinding to a peer message dumps the whole wrapper blob into your input. Clear
it with ctrl-u. Fixing it means a third site with its own byte hunt.

Anchors are structural (this code has no stable strings to hang off), so the
patterns are regexes over the shapes with the minified identifiers wildcarded,
and each must match exactly once.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import apply_patch, candidate_binaries

MARKER = b"[Zq4x rewind-peer]"

# --- edit 1+2: the two gates, one contiguous span across a5e and eqe ---------
# Wildcards: \1 = a5e, \2 = eqe. Requiring the same \2 in the call and in the
# following definition is what makes this shape-match trustworthy.
RE_GATES = re.compile(
    rb"function ([\w$]{1,8})\(e\)\{if\(!([\w$]{1,8})\(e\)\)return!1;"
    rb'(if\(e\.origin&&e\.origin\.kind!=="human"\)return!1;)'
    rb"if\(e\.stackedExpansion\)return!1;return!0\}"
    rb'function \2\(e\)\{if\(e\.type!=="user"\)return!1;'
    rb"(.{0,300}?)"
    rb"(if\(e\.isMeta\)return!1;)",
    re.S,
)

# --- edit 3: the checkpoint-row renderer ------------------------------------
# \1 = local text var, \2 = the message-text extractor, \3 = the message prop.
RE_ROW = re.compile(
    rb'let ([\w$]{1,8})=([\w$]{1,8})\(([\w$]{1,8})\)\?\.trim\(\)\|\|"\(no prompt\)";'
    rb"(.{0,300}?)"
    rb'(flexDirection:"row",)width:"100%",',
    re.S,
)

ROW_PREFER_ORIGIN = b".origin?.body||"


def _one(rx: re.Pattern[bytes], data: bytes, what: str) -> re.Match[bytes]:
    hits = list(rx.finditer(data))
    if len(hits) != 1:
        raise RuntimeError(
            f"expected exactly 1 match for the {what} shape, found {len(hits)} — "
            f"upstream code changed. Re-investigate: the rewind checkpoint list is "
            f'`messages.filter(<pred>)`; find it by grepping for the string "(no prompt)" '
            f'(row renderer) or \'e.origin&&e.origin.kind!=="human"\' (the filter).'
        )
    return hits[0]


def _gates_replacement(m: re.Match[bytes]) -> tuple[int, int, bytes]:
    """Blank both gates in one span. Returns (start, end, replacement)."""
    origin_gate, ismeta_gate = m.group(3), m.group(5)
    comment = b"/*" + MARKER + b"*/"
    pad = len(origin_gate) - len(comment)
    if pad < 0:
        raise RuntimeError(
            f"origin gate ({len(origin_gate)} bytes) too short to hold the "
            f"{len(comment)}-byte marker — shorten MARKER"
        )
    new_origin = comment + b" " * pad
    new_ismeta = b" " * len(ismeta_gate)
    assert len(new_origin) == len(origin_gate)
    assert len(new_ismeta) == len(ismeta_gate)

    span = m.group(0)
    new = span.replace(origin_gate, new_origin, 1)
    # The isMeta gate is the tail of the span, so a tail-anchored swap is exact.
    assert new.endswith(ismeta_gate)
    new = new[: -len(ismeta_gate)] + new_ismeta
    assert len(new) == len(span)
    return m.start(), m.end(), new


def _row_replacement(m: re.Match[bytes]) -> tuple[int, int, bytes]:
    """Prefer origin.body in the row text; pay for it with the no-op flexDirection."""
    var, extract, msg, between, flexdir = m.groups()
    head = (
        b"let "
        + var
        + b"="
        + msg
        + ROW_PREFER_ORIGIN
        + extract
        + b"("
        + msg
        + b')?.trim()||"(no prompt)";'
    )
    pad = len(flexdir) - (len(msg) + len(ROW_PREFER_ORIGIN))
    if pad < 0:
        raise RuntimeError(
            f"row edit needs {len(msg) + len(ROW_PREFER_ORIGIN)} bytes but only "
            f"{len(flexdir)} are free from the redundant flexDirection prop — find "
            f"{-pad} more nearby (`italic:!0,` in the same JSX call is 10 free bytes, "
            f"it only italicises the empty-message placeholder)"
        )
    # Padding sits between object-literal properties, outside any string literal.
    new = head + between + b'width:"100%",' + b" " * pad
    assert len(new) == len(m.group(0)), (len(new), len(m.group(0)))
    return m.start(), m.end(), new


def main() -> int:
    try:
        cands = candidate_binaries()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not cands:
        print(
            "no claude binary found (set CLAUDE_CLI_PATCH_TARGET to aim at an "
            "experimental copy)",
            file=sys.stderr,
        )
        return 1
    binp = cands[0]
    data = binp.read_bytes()

    if MARKER in data and ROW_PREFER_ORIGIN in data:
        print(f"rewind-peer-targets: confirmed already patched ({binp})", file=sys.stderr)
        return 0
    if MARKER in data or ROW_PREFER_ORIGIN in data:
        print(
            f"{binp} carries only half of this patch (marker={MARKER in data}, "
            f"row={ROW_PREFER_ORIGIN in data}) — restore from {binp}.orig and re-run",
            file=sys.stderr,
        )
        return 1

    try:
        edits = [
            _gates_replacement(_one(RE_GATES, data, "rewind checkpoint filter")),
            _row_replacement(_one(RE_ROW, data, "checkpoint row renderer")),
        ]
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    edits.sort(key=lambda t: t[0])
    if any(a[1] > b[0] for a, b in zip(edits, edits[1:])):
        raise RuntimeError("edit spans overlap — refusing")

    patched, cursor = b"", 0
    for start, end, new in edits:
        patched += data[cursor:start] + new
        cursor = end
    patched += data[cursor:]
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        ok = (
            len(written) == len(data)
            and MARKER in written
            and written.count(ROW_PREFER_ORIGIN) == 1
            and not RE_GATES.search(written)
        )
        if not ok:
            raise RuntimeError(
                f"post-write verification failed (len={len(written)} want={len(data)}, "
                f"marker={MARKER in written}, row={written.count(ROW_PREFER_ORIGIN)}) "
                f"— live binary untouched"
            )

    apply_patch(binp, data, patched, _verify)
    print(
        f"rewind-peer-targets: applied patch to {binp} "
        f"(pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
