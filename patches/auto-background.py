#!/usr/bin/env python3
"""CLI patch: every Bash command is eligible for auto-background at sync timeout.

When a synchronous Bash call hits its timeout, Claude Code does one of two
things, decided BEFORE spawn by a pure function of the command string
(shape as of 2.1.270):

    hn=!Wn&&mzs(Ae),Xt=!Wn,Hn=l6t({...,canAutoBackground:hn})
       ^^^^^^^^^^^^ this patch: `mzs(Ae)` -> `!0` (+ same-length comment)

- `hn` true  -> at timeout the tool registers the command as a background task:
  partial output is kept, the process keeps running, a completion notification
  fires ("Command did not complete within its Ns timeout and was moved to the
  background").
- `hn` false -> SIGTERM to the process group, exit 143, "Command timed out
  after Ns", and NO output at all — not even the lines already printed.

`mzs` is the eligibility predicate. Through 2.1.257 it rejected any command
the static shell analyzer could not decompose as "simple" (a `$VAR` or
backtick in a redirect target, an unquoted heredoc, a quoted heredoc with a
redirect after the operator, process substitution) plus any git subcommand and
a leading `sleep`. The rule was undocumented and fiddly enough that models kept
writing kill-class commands without noticing, and the kill converts a
measurement into a guess (https://github.com/anthropics/claude-code/issues/79879).

2.1.270 cut it down to a first-word blocklist — `var uzs=["sleep"]`, so a
leading `sleep` is now the ONLY ineligible shape, and the sibling `Xt`
(turn-abort backgrounding) dropped its `/git/i` test entirely. That is most of
what this patch was for, but not all of it: on this box `sleep`-led commands
are exactly the watchdog shape `hooks/force_background_sleep.py` exists to
background, and that hook skips subagents (they get no background-completion
notification), so an unpatched subagent watchdog still ends in a silent
SIGTERM. Forcing the flag closes that last case. See also
`sleep-guard-off.py`, which removes the separate pre-spawn rejection of the
same commands.

The flag's only consumers (traced on 2.1.257): the timeout handler class
(`if(#y&&#d) #d(background) else kill`), the caller's `if(Ct.onTimeout&&Pt)`
registration of that callback, and the CLAUDE_CODE_AUTO_BACKGROUND_TIMEOUT_MS
clamp. Nothing downstream needs the predicate's verdict for anything else, so
forcing the flag just routes every timeout down the existing background path.
`Wn` (non-interactive / background forbidden) still wins — `-p` mode is
unchanged. `Xt` (turn-abort backgrounding) is a separate flag and is untouched.
The PowerShell tool has a sibling site (`me=!fe&&await Hs(T)`) that is left
alone: untestable here.

2.1.271+ adds one more clause in front of the predicate:
`Vt=!sr&&ve===void 0&&rYo(Ye),` where `ve` is the tool's `shellWallCapMs`
argument (a caller-supplied wall-clock cap, unset on the ordinary interactive
path). Upstream turns auto-background off whenever a cap is passed; this patch
leaves that clause alone and still only forces the predicate call, so a capped
call behaves exactly as stock. The regex accepts the clause as optional and
matches both shapes. The predicate itself is unchanged (still the first-word
blocklist, `["sleep"]`).

Same-length edit (Bun single-file executable, JS blob carries length
metadata). The predicate call is only ~8 bytes, so the marker is whatever
prefix of `autobg` fits inside `!0/*...*/` — the patched shape itself
(`=!Wn&&!0/*au*/,` in front of the same `canAutoBackground:` flag) is the
idempotency signal.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries

# Stable property name that survives identifier renames. It appears at the two
# tool call sites (Bash, PowerShell) and in the `l6t({...canAutoBackground:r})`
# definition; the regexes below pin the flag's own name and then require its
# assignment just before, which only the Bash site has in this shape.
FLAG = b"canAutoBackground:"
FLAG_RE = re.compile(re.escape(FLAG) + rb"(" + JSID + rb")[,}]")
WINDOW = 400  # bytes before FLAG in which the flag's assignment must sit

# `hn=!Wn&&mzs(Ae),` (≤2.1.270) or `Vt=!sr&&ve===void 0&&rYo(Ye),` (2.1.271+)
# — eligibility = "not forbidden" [AND no wall cap] AND the static predicate.
# PowerShell's is `me=!fe&&await Hs(T),`, which this does not match (the
# `await ` breaks the `&&<call>` adjacency) — deliberately, see the docstring.
_HEAD = rb"=!" + JSID + rb"&&(?:" + JSID + rb"===void 0&&)?"
STOCK_TAIL = _HEAD + rb"(" + JSID + rb"\(" + JSID + rb"\)),"
PATCHED_TAIL = _HEAD + rb"(!0(?:/\*[a-z]*\*/| *)),"
MARKER_TEXT = b"autobg"


def find_site(data: bytes, tail: bytes) -> list[tuple[int, int]]:
    """(start, end) of the predicate region at each `canAutoBackground:<flag>`
    whose preceding window assigns that same `<flag>` with `tail`'s shape."""
    hits = []
    for m in FLAG_RE.finditer(data):
        assign = re.compile(rb"(?<![A-Za-z0-9_$])" + re.escape(m.group(1)) + tail)
        a = assign.search(data, max(0, m.start() - WINDOW), m.start())
        if a:
            hits.append((a.start(1), a.end(1)))
    return hits


def replacement(n: int) -> bytes:
    if n < 2:
        raise RuntimeError(f"predicate call is only {n} bytes — can't fit `!0`")
    if n >= 6:  # `!0/**/` plus as much of the marker as fits
        marker = MARKER_TEXT[: n - 6]
        rep = b"!0/*" + marker + b"*/"
        rep = rep[:-2] + b" " * (n - len(rep)) + b"*/"
    else:
        rep = b"!0" + b" " * (n - 2)
    assert len(rep) == n, (rep, n)
    return rep


def main() -> int:
    cands = candidate_binaries()
    if not cands:
        print("no claude binary found (unknown install layout)", file=sys.stderr)
        return 1
    binp = cands[0]
    data = binp.read_bytes()

    if find_site(data, PATCHED_TAIL):
        print(f"auto-background: confirmed already patched ({binp})", file=sys.stderr)
        return 0

    sites = find_site(data, STOCK_TAIL)
    if len(sites) != 1:
        print(
            f"expected exactly 1 Bash auto-background site, found {len(sites)} "
            f"({FLAG!r} occurs {data.count(FLAG)}x) — upstream code changed; "
            f"re-investigate around 'canAutoBackground' in {binp}: find where the "
            f"Bash tool computes the flag it passes to the timeout helper, and force "
            f"that predicate call true",
            file=sys.stderr,
        )
        return 1
    s, e = sites[0]
    old = data[s:e]
    new = replacement(e - s)
    patched = data[:s] + new + data[e:]
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        if (
            len(written) != len(data)
            or len(find_site(written, PATCHED_TAIL)) != 1
            or find_site(written, STOCK_TAIL)
        ):
            raise RuntimeError("post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)
    print(
        f"auto-background: applied patch to {binp} ({old!r} -> {new!r}; "
        f"pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
