#!/usr/bin/env python3
"""CLI patch: silence phantom "## Exited Plan Mode" reminders.

Cycling permission modes (shift+tab: default -> plan -> bypass -> ...) passes
*through* plan mode, and the plan_mode_exit attachment producer (ZPl in 2.1.197)
only checks "mode is no longer plan" + a just-exited flag — so a session that
never actually planned gets a mid-turn system reminder:

    ## Exited Plan Mode
    You have exited plan mode. You can now make edits, run tools, and take
    actions.

Claude then (reasonably) reports it was never in plan mode. Pure noise.

The attachment already carries the discriminating bit: `planExists` — whether a
real plan file is on disk for this session. Phantom pass-throughs have
planExists=false; a genuine "user left plan mode with a plan written" has
planExists=true. This patch gates the attachment RENDERER on it:

    plan_mode_exit:(e)=>{let t=e.planExists?` The plan file is located at
        ${e.planFilePath} if you need to reference it.`:"";return Ep([...])}
->  plan_mode_exit:(e)=>{if(!e.planExists)return[];let t=` The plan file is
        located at ${e.planFilePath}.`;return Ep([...])}

so no-plan exits render to nothing while with-plan exits keep the reminder and
the plan-file pointer (tail slightly shortened to pay for the added guard —
same-length in-place edit, since the Bun single-file executable stores the JS
blob with length metadata). Patching the renderer rather than the producer
keeps the attachment in the transcript, so the backward-scan throttling logic
around plan_mode attachments behaves exactly as stock.

Trade-off accepted: leaving plan mode BEFORE any plan file was written is also
silenced. There's nothing to reference in that case, and mode enforcement
happens at the permission layer anyway.

Idempotency: the patched guard string is unique and doubles as the marker
(same approach as task-nag.sh's PATCHED string).

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Anchor ONLY on the arrow head + the `planExists` ternary — all stable
# identifiers (`plan_mode_exit`, `e.planExists`, `e.planFilePath`) and the stable
# English plan-file string — and STOP before `return X([Y({content:...`. The
# wrapper/element-constructor names there (`Ep([Dn(` in 2.1.197, `Hp([Ln(` in
# 2.1.201) are minified and renamed every build, so we neither match nor re-emit
# them: the patch just injects an early `return[]` guard and leaves the original
# return statement untouched. Rename-proof across updates.
PATTERN = (
    b"plan_mode_exit:(e)=>{let t=e.planExists?"
    b"` The plan file is located at ${e.planFilePath} if you need to reference it.`"
    b':"";'
)
GUARD = b"plan_mode_exit:(e)=>{if(!e.planExists)return[];"
CORE = GUARD + b"let t=` The plan file is located at ${e.planFilePath}.`;"


def build_replacement() -> bytes:
    pad = len(PATTERN) - len(CORE)
    if pad < 0:
        raise RuntimeError("replacement longer than pattern — recompute")
    # Pad with spaces at the end (between the injected `let t=…;` and the original
    # untouched `return …` statement) — JS-legal inter-statement whitespace.
    rep = CORE + b" " * pad
    assert len(rep) == len(PATTERN)
    return rep


def candidate_binaries() -> list[Path]:
    """The single ACTIVE binary (`which claude` resolved), else newest in versions/.

    Returning ONLY the active binary avoids the stale-old-version masking bug:
    an old patched binary lingering in versions/ (e.g. 2.1.197 after an update to
    2.1.201) must not let a patch report 'already patched' and skip the live one.
    """
    which = shutil.which("claude")
    if which:
        real = Path(which).resolve()
        if real.is_file():
            return [real]
    vdir = Path.home() / ".local/share/claude/versions"
    if vdir.is_dir():
        files = [
            p
            for p in vdir.iterdir()
            if p.is_file() and p.suffix not in (".orig",) and ".patch." not in p.name
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return [files[0]]
    return []


def main() -> int:
    rep = build_replacement()

    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if GUARD in data:
            print(f"plan-exit-nag: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        if PATTERN in data:
            target = (binp, data)
            break

    if target is None:
        print(
            f"pattern not found in any candidate binary "
            f"({[str(p) for p in candidate_binaries()]}) — upstream code changed "
            f"or unknown install layout; re-investigate around the string "
            f"'Exited Plan Mode' (attachment renderer map) in the binary",
            file=sys.stderr,
        )
        return 1

    binp, data = target
    n = data.count(PATTERN)
    if n != 1:
        print(
            f"expected exactly 1 occurrence of the renderer pattern, found {n} "
            f"in {binp} — upstream code changed; refusing to patch",
            file=sys.stderr,
        )
        return 1

    patched = data.replace(PATTERN, rep)
    assert len(patched) == len(data)

    # Write to a temp copy then atomically swap in (in-place write on a live
    # binary hits ETXTBSY if a claude process is running from it).
    fd, tmp = tempfile.mkstemp(prefix=binp.name + ".patch.", dir=str(binp.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(patched)
        written = Path(tmp).read_bytes()
        if len(written) != len(data) or GUARD not in written or PATTERN in written:
            raise RuntimeError(
                f"post-write verification failed on {tmp} — live binary untouched"
            )
        shutil.copymode(binp, tmp)
        orig = binp.with_name(binp.name + ".orig")
        if not orig.exists():
            shutil.copy2(binp, orig)
        os.replace(tmp, binp)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    print(
        f"plan-exit-nag: applied patch to {binp} "
        f"(pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
