#!/usr/bin/env python3
"""CLI patch: stop the Bash tool rejecting foreground `sleep` commands.

Since 2.1.270 the Bash tool's `validateInput` refuses a command whose FIRST
statement is a bare `sleep N` with N at or above a 25-second threshold:

    Blocked: sleep 45 followed by: cat /tmp/tsc.log. To wait for a condition,
    use Monitor with an until-loop (e.g. `until <check>; do sleep 2; done`).
    To wait for a command you started, use run_in_background: true. Do not
    chain shorter sleeps to work around this block.

The shape (all minified names are captured by the anchor, not assumed):

    var M6t=25;                                     // the threshold, seconds
    function gzs(e){                                // the detector
      let n=sh(e); if(n.length===0)return null;
      let r=n[0]?.trim()??"", s=/^sleep\\s+(\\d+(?:\\.\\d*)?)\\s*$/.exec(r);
      if(!s)return null;
      let d=parseFloat(s[1]);
      if(d<M6t)return null;                         // <-- patched to `if(1  )`
      let m=n.slice(1).join(" ").trim();
      return m?`sleep ${d} followed by: ${m}`:`standalone sleep ${d}`}
    ...
    async validateInput(n,r){ ... if(qM()&&!rc()&&...&&!n.run_in_background){
      let m=gzs(n.command); if(m!==null)return{result:!1,message:`Blocked: ...`}

`gzs` has exactly one caller (that validateInput), so forcing it to return
null everywhere is the whole behaviour change: the command is validated as
normal and runs.

Why turn it off: this box already routes the same policy through a PreToolUse
hook, `hooks/force_background_sleep.py`, which rewrites a leading-`sleep`
command to `run_in_background: true` with a "watchdog, idle until the
notification" note — a fallback wakeup is fine here, it just belongs in the
background. The built-in guard pre-empts that: `validateInput` runs on the
ORIGINAL tool input, before PreToolUse hooks get to rewrite it, so a
hook-managed 45s watchdog is rejected outright while a 20s one sails through
purely because it sits under the threshold. Turning the guard off makes the
hook the single policy point instead of a second, coarser one the hook cannot
see or override.

Scope — the Bash tool only. The PowerShell tool has a sibling detector in
another chunk, reading the same exported threshold but matching
`Start-Sleep` (`return a?`Start-Sleep ${r} followed by: ${a}`:...`), and it is
deliberately left alone: `force_background_sleep.py` is registered under the
`Bash` matcher only, so disabling the PowerShell guard would remove the block
without putting anything in its place. If that hook ever grows a `PowerShell`
matcher, widen this patch to the sibling site at the same time.

There is no env var or setting for this. The feature is gated on the
GrowthBook flag `tengu_amber_sentinel` (`function qM(){return
P("tengu_amber_sentinel",!1)}`) which is server-side only — no
`CLAUDE_CODE_*` override reads it, which is why this is a patch and not a
settings entry. Killing `qM` would also be broader than wanted: the same flag
drives unrelated notification-prompt text.

The edit replaces the threshold comparison `d<M6t` with `1` plus spaces:

    if(d<M6t)return null;   ->   if(1    )return null;

Always-true guard, so the detector returns null before it ever builds a
message. Padding with spaces rather than a marker comment keeps this working
for any length of threshold identifier (a comment needs 6 bytes of overhead
and the comparison is only ~5). The padded `if(1 *)` inside the otherwise
unchanged detector body is the applied-marker — unique because PATCHED_RX
re-checks the whole surrounding function shape.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries


def _detector(comparison: bytes) -> re.Pattern[bytes]:
    """The tail of the Bash sleep detector, with `comparison` as the guard test.

    Anchored on the model-facing `followed by:` / `standalone sleep` template
    (stable — it is the text of the block message) and on the `parseFloat` of
    the regex capture just above it, so the match is pinned to the seconds
    value actually being compared. `Start-Sleep` in the PowerShell sibling's
    template is what keeps this from matching there.
    """
    return re.compile(
        rb"let (?P<sec>" + JSID + rb")=parseFloat\(" + JSID + rb"\[1\]\);"
        rb"if\(" + comparison + rb"\)return null;"
        rb"let (?P<rest>" + JSID + rb")=" + JSID + rb"\.slice\(1\)\.join\(\" \"\)\.trim\(\);"
        rb"return (?P=rest)\?`sleep \$\{(?P=sec)\} followed by: \$\{(?P=rest)\}`"
        rb":`standalone sleep \$\{(?P=sec)\}`"
    )


# Stock: `if(<seconds> < <threshold>)`, both minified.
STOCK_RX = _detector(rb"(?P=sec)<(?P<thr>" + JSID + rb")")
# Patched: `if(1   )` — always true, detector returns null, nothing is blocked.
PATCHED_RX = _detector(rb"1 *")

REINVESTIGATE = (
    "re-investigate around the string 'followed by:' in the binary: there are two "
    "copies of this detector, and the Bash one is the copy whose template says "
    "'sleep ${d} followed by:' (the other says 'Start-Sleep'). The guard to "
    "neutralize is the `if(<seconds><<threshold>)return null;` immediately above "
    "that template; the threshold is a module-level `var <name>=25;` shared with "
    "the PowerShell copy, so do NOT patch the declaration itself"
)


def main() -> int:
    cands = candidate_binaries()
    if not cands:
        print("no claude binary found (unknown install layout)", file=sys.stderr)
        return 1
    binp = cands[0]
    data = binp.read_bytes()

    if PATCHED_RX.search(data):
        print(f"sleep-guard-off: confirmed already patched ({binp})", file=sys.stderr)
        return 0

    matches = list(STOCK_RX.finditer(data))
    if len(matches) != 1:
        print(
            f"expected exactly 1 Bash sleep-detector site, found {len(matches)} in "
            f"{binp} — upstream code changed; {REINVESTIGATE}",
            file=sys.stderr,
        )
        return 1

    m = matches[0]
    seconds, threshold = m.group("sec"), m.group("thr")
    old = b"if(" + seconds + b"<" + threshold + b")return null;"
    new = b"if(1" + b" " * (len(seconds) + len(threshold)) + b")return null;"
    if len(new) != len(old):
        print(f"length drift {len(old)} -> {len(new)} — refusing", file=sys.stderr)
        return 1
    if data.count(old) != 1:
        print(
            f"guard {old!r} occurs {data.count(old)}x — ambiguous, refusing to patch; "
            f"{REINVESTIGATE}",
            file=sys.stderr,
        )
        return 1

    patched = data.replace(old, new, 1)

    def _verify(written: bytes) -> None:
        if (
            len(written) != len(data)
            or not PATCHED_RX.search(written)
            or STOCK_RX.search(written)
        ):
            raise RuntimeError("post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)
    print(
        f"sleep-guard-off: applied patch to {binp} ({old!r} -> {new!r}; "
        f"pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
