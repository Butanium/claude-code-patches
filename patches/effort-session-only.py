#!/usr/bin/env python3
"""CLI patch: `/effort` changes the effort level for the current session only.

Stock `/effort <level>` (and Enter in the no-argument picker) also writes the
level to ~/.claude/settings.json (`modelSettings.<model>.effortLevel` in 2.1.289),
so every new session on that model inherits it. All
interactive `/effort` paths go through one setter (2.1.289):

    async function _Te(e,t,n,r=!0,f){let s=!1,o=null,E=await T(e,t,r,(u)=>{...

where `r` is "persist to user settings". This patch forces it to false:

    async function _Te(e,t,n,r,f){let s=r=!1, o=null,E=await T(e,t,r,(u)=>{...

(same length: `=!0` dropped from the parameter default, `r=` added to the
`let`, one space of padding). The stock code already handles r=false, so the
result message reads "... (this session only)" and nothing is saved.

Untouched: the /model picker and `/model <name>` effort arg (their own
persistence calls), the SDK `update_settings` control request, `--effort` and
CLAUDE_CODE_EFFORT_LEVEL. To change the default for new sessions, edit
`effortLevel` in settings.json.

Anchor: the setter lives in the chunk with "Failed to set effort level"; it is
the only `async function X(a,b,c,d=!0,e){let F=!1,G=null,H=await Y(a,b,d,` in
the binary. The patched shape is its own idempotency marker.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries, read_binary, splice

I = rb"(" + JSID + rb")"
STOCK = re.compile(
    rb"async function " + I + rb"\(" + I + rb"," + I + rb"," + I + rb"," + I + rb"=!0," + I
    + rb"\)\{let " + I + rb"=!1," + I + rb"=null," + I + rb"=await " + I
    + rb"\(\2,\3,\5,"
)
PATCHED = re.compile(
    rb"async function " + JSID + rb"\(" + JSID + rb"," + JSID + rb"," + JSID + rb"," + I + rb","
    + JSID + rb"\)\{let " + JSID + rb"=\1=!1, " + JSID + rb"=null," + JSID + rb"=await " + JSID
    + rb"\(" + JSID + rb"," + JSID + rb",\1,"
)
CONTEXT = b"Failed to set effort level"


def replacement(m: re.Match) -> bytes:
    fn, a, b, c, r, f, s, o, e, t = m.groups()
    new = (b"async function " + fn + b"(" + a + b"," + b + b"," + c + b"," + r + b"," + f
           + b"){let " + s + b"=" + r + b"=!1, " + o + b"=null," + e + b"=await " + t
           + b"(" + a + b"," + b + b"," + r + b",")
    assert len(new) == len(m.group(0)), (new, m.group(0))
    return new


def main() -> int:
    for binp in candidate_binaries():
        data = read_binary(binp)
        if CONTEXT not in data:
            continue
        if PATCHED.search(data):
            print(f"effort-session-only: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        hits = list(STOCK.finditer(data))
        if len(hits) != 1:
            print(
                f"effort-session-only: expected exactly 1 stock /effort setter, found {len(hits)} "
                f"in {binp} — upstream code changed. Re-investigate: `clisrc.py --find "
                f"'Failed to set effort level'`, then find the async function that the /effort "
                f"command and picker call with a persist flag (stock `r=!0` default) and force it false.",
            )
            return 1
        m = hits[0]
        new = replacement(m)
        patched = splice(data, m.start(), m.end(), new)
        assert len(patched) == len(data)

        def _verify(written: bytes) -> None:
            if len(written) != len(data) or not PATCHED.search(written) or STOCK.search(written):
                raise RuntimeError("effort-session-only: post-write verification failed — live binary untouched")

        apply_patch(binp, data, patched, _verify)
        print(f"effort-session-only: applied to {binp} (backup at {binp}.orig)", file=sys.stderr)
        return 0

    print(
        f"effort-session-only: anchor {CONTEXT!r} not found in any candidate binary "
        f"({[str(p) for p in candidate_binaries()]}) — the /effort command was reworked; "
        f"re-investigate how /effort persists effortLevel to user settings.",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
