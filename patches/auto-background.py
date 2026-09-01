#!/usr/bin/env python3
"""CLI patch: every Bash command is eligible for auto-background at sync timeout.

When a synchronous Bash call hits its timeout, Claude Code does one of two
things, decided BEFORE spawn by a pure function of the command string:

    Pt=!cn&&obr(Ce),gn=!cn&&!/git/i.test(Ce),Tn=mUt({...,canAutoBackground:Pt})
       ^^^^^^^^^^^^ this patch: `obr(Ce)` -> `!0` (+ same-length comment)

- `Pt` true  -> at timeout the tool registers the command as a background task:
  partial output is kept, the process keeps running, a completion notification
  fires ("Command did not complete within its Ns timeout and was moved to the
  background").
- `Pt` false -> SIGTERM to the process group, exit 143, "Command timed out
  after Ns", and NO output at all — not even the lines already printed.

`obr` (the eligibility predicate) rejects any command the static shell
analyzer can't decompose as "simple" (a `$VAR` or backtick in a redirect
target, an unquoted heredoc, a quoted heredoc with a redirect after the
operator, process substitution), any git subcommand, and a leading `sleep`.
The rule is undocumented and fiddly enough that models keep writing
kill-class commands without noticing, and the kill converts a measurement
into a guess (https://github.com/anthropics/claude-code/issues/79879).

The flag's only consumers (traced on 2.1.257): the timeout handler class
(`if(#y&&#d) #d(background) else kill`), the caller's `if(Ct.onTimeout&&Pt)`
registration of that callback, and the CLAUDE_CODE_AUTO_BACKGROUND_TIMEOUT_MS
clamp. Nothing downstream needs the analyzer's decomposition, so forcing the
flag just routes every timeout down the existing background path. `cn`
(non-interactive / background forbidden) still wins — `-p` mode is unchanged.
`gn` (kill git commands on turn abort) is a separate flag and is untouched.
The PowerShell tool has a sibling site (`ue=!re&&await Ys(v)`) that is left
alone: untestable here.

Same-length edit (Bun single-file executable, JS blob carries length
metadata). `obr(Ce)` is only ~7 bytes, so the marker is whatever prefix of
`autobg` fits inside `!0/*...*/` — the patched shape itself
(`&&!0/*a*/,gn=!cn&&!/git/i.test(`) is the idempotency signal.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import apply_patch, candidate_binaries

# Stable literal that survives identifier renames; the Bash site and the
# PowerShell site both carry it, the regex below only matches the Bash one
# (PowerShell's predicate is `await Ys(v)`, not a bare call).
GIT_TEST = b"&&!/git/i.test("
WINDOW = 80  # bytes before GIT_TEST that must hold `X=!cn&&pred(cmd),Y=!cn`
STOCK_RE = re.compile(rb"=!(\w+)&&(\w+\(\w+\)),(\w+)=!\1$")
PATCHED_RE = re.compile(rb"=!(\w+)&&(!0(?:/\*[a-z]*\*/| *)),(\w+)=!\1$")
MARKER_TEXT = b"autobg"


def find_site(data: bytes, pattern: re.Pattern[bytes]) -> list[tuple[int, int]]:
    """(start, end) of the predicate-call region (group 2) at each GIT_TEST
    occurrence whose preceding window matches `pattern`."""
    hits = []
    pos = 0
    while (i := data.find(GIT_TEST, pos)) != -1:
        m = pattern.search(data, max(0, i - WINDOW), i)
        if m:
            hits.append((m.start(2), m.end(2)))
        pos = i + 1
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

    if find_site(data, PATCHED_RE):
        print(f"auto-background: confirmed already patched ({binp})", file=sys.stderr)
        return 0

    sites = find_site(data, STOCK_RE)
    if len(sites) != 1:
        print(
            f"expected exactly 1 Bash auto-background site, found {len(sites)} "
            f"({GIT_TEST!r} occurs {data.count(GIT_TEST)}x) — upstream code changed; "
            f"re-investigate around 'canAutoBackground' / '/git/i.test(' in {binp}",
            file=sys.stderr,
        )
        return 1
    s, e = sites[0]
    old = data[s:e]
    new = replacement(e - s)
    patched = data[:s] + new + data[e:]
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        if len(written) != len(data) or len(find_site(written, PATCHED_RE)) != 1 or find_site(written, STOCK_RE):
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
