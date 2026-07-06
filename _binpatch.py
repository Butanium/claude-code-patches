"""Shared helpers for the same-length `claude` binary patches in patches/.

Two things every Python patch needs, factored out so the platform-specific
Windows handling lives in ONE place instead of being copy-pasted three times:

1. `candidate_binaries()` — locate the single LIVE Bun bundle to patch.
   The trap on Windows: `shutil.which("claude")` returns a launcher *shim*
   (`C:\\...\\commands\\claude.cmd`, a one-line `@"...\\claude.exe" ... %*`
   wrapper), NOT the 240 MB JS bundle the patches anchor in. Reading the shim's
   bytes finds no anchor and every patch aborts. We follow the shim to the real
   `.exe` it invokes; on Unix `which` resolves through the symlink as before.

2. `apply_patch()` — atomically swap patched bytes into place.
   The trap on Windows: a running `.exe` is locked, so `os.replace(tmp, binp)`
   fails with `PermissionError [WinError 5]`. You CAN rename a running exe aside
   though (that is exactly how the installer produces `claude.exe.old.<ts>`), so
   we rename the live binary out of the way and drop the patched copy into the
   vacated slot. On Unix the plain `os.replace` inode-swap path is kept.

Only ONE candidate is ever returned, on purpose: an old patched binary lingering
in versions/ must not let a patch report 'already patched' and skip the live one
(the stale-old-version masking bug).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

_SHIM_SUFFIXES = {".cmd", ".bat", ".ps1"}
# Absolute paths ending in .exe inside a launcher shim (`@"C:\...\x.exe" %*`).
_SHIM_EXE_RE = re.compile(r'([A-Za-z]:[\\/][^"\r\n]*?\.exe)')


def _shim_target(p: Path) -> Path | None:
    """If `p` is a Windows launcher shim wrapping a real `.exe`, return that exe.

    A fancier shim (e.g. a `.ps1` that names `powershell.exe` before the target)
    can list several `.exe`s, so prefer one whose path mentions 'claude' and fall
    back to the first — then require it to exist on disk.
    """
    if p.suffix.lower() not in _SHIM_SUFFIXES:
        return None
    try:
        text = p.read_text(errors="replace")
    except OSError:
        return None
    matches = _SHIM_EXE_RE.findall(text)
    if not matches:
        return None
    chosen = next((m for m in matches if "claude" in m.lower()), matches[0])
    exe = Path(chosen)
    return exe if exe.is_file() else None


def candidate_binaries() -> list[Path]:
    """Return `[the single live claude bundle]`, or `[]` if not locatable.

    Resolution order:
      1. `which claude`, following a `.cmd`/`.bat`/`.ps1` launcher shim to the
         real `.exe` it invokes (Windows). A resolved shim we can't dereference is
         rejected so we fall through rather than patch a 90-byte wrapper.
      2. the native-installer live copy at `~/.local/bin/claude[.exe]`, resolved
         through the symlink (on Linux bin/claude points into versions/; replacing
         the link itself instead of its target would detach a patched regular file
         and leave the real binary untouched).
      3. newest file in `~/.local/share/claude/versions` (last resort; an inert
         copy on the native Windows layout, but correct where bin/ is a symlink).
    """
    which = shutil.which("claude")
    if which:
        p = Path(which)
        target = _shim_target(p) or p.resolve()
        if target.is_file() and target.suffix.lower() not in _SHIM_SUFFIXES:
            return [target]

    for cand in (
        Path.home() / ".local/bin/claude.exe",
        Path.home() / ".local/bin/claude",
    ):
        if cand.is_file():
            return [cand.resolve()]

    vdir = Path.home() / ".local/share/claude/versions"
    if vdir.is_dir():
        files = [
            f
            for f in vdir.iterdir()
            if f.is_file() and f.suffix != ".orig" and ".patch" not in f.name
        ]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        if files:
            return [files[0]]

    return []


# Only sweep orphans older than this. A concurrent SessionStart in another
# session may have just written a `.patch.*` temp it is about to rename in — its
# fd is briefly closed during verify / `.orig` copy, so on Linux (and in that
# window on Windows) it is deletable. Age-gating leaves recent files alone, so a
# live run's temp is never yanked out from under it; genuinely stale orphans are
# all well past this.
_ORPHAN_MIN_AGE_S = 3600


def _sweep_orphans(binp: Path) -> None:
    """Best-effort delete of stale `.patch.*` temps and `.patchold.*` aside files
    from prior runs. A still-running old image is locked and refuses to delete
    (OSError) — skipped here; a later session sweeps it once that process exits.
    System-boundary cleanup — swallow per-file."""
    now = time.time()
    for pattern in (binp.name + ".patch.*", binp.name + ".patchold.*"):
        for f in binp.parent.glob(pattern):
            try:
                if now - f.stat().st_mtime < _ORPHAN_MIN_AGE_S:
                    continue  # too fresh — may be a concurrent run's live temp
                f.unlink()
            except OSError:
                pass


def _atomic_swap(tmp: Path, binp: Path) -> None:
    """Move `tmp` -> `binp`. On a locked running `.exe` (Windows `WinError 5`),
    rename the live binary aside first, then move `tmp` into the vacated slot.

    Brief empty-slot window (Windows only): between the rename and the move,
    `binp` does not exist, so a `claude` launched in that instant fails to start.
    It is milliseconds and unavoidable while the running image holds the lock.
    """
    try:
        os.replace(tmp, binp)
        return
    except PermissionError:
        if os.name != "nt":
            raise  # not the running-exe lock — a real problem, surface it

    # Unique per-invocation name: pid alone collides across sessions (Windows
    # recycles pids, and an aside from a still-running claude lingers between
    # updates) — a bare unlink of such a locked leftover would abort the patch.
    aside = binp.with_name(binp.name + f".patchold.{os.getpid()}.{int(time.time())}")
    os.rename(binp, aside)  # allowed even while the exe is running
    try:
        os.replace(tmp, binp)  # vacated slot -> plain create, no lock
    except BaseException:
        os.replace(aside, binp)  # roll back to the original
        raise
    try:
        aside.unlink()  # usually locked (still-running image) -> swept next session
    except OSError:
        pass


def apply_patch(
    binp: Path,
    original: bytes,
    patched: bytes,
    verify: Callable[[bytes], None],
) -> None:
    """Atomically replace `binp`'s bytes with `patched` (same length as original).

    `verify(written_bytes)` must raise on a bad/partial write; it runs against the
    temp copy re-read from disk, before anything touches the live binary. A
    pristine `.orig` backup is (re)made whenever its on-disk size differs from the
    current binary's — so it refreshes after a claude update instead of going
    stale. Windows running-exe lock is handled via rename-aside. Temp files are
    cleaned up on any failure.
    """
    if len(patched) != len(original):
        raise RuntimeError(
            f"patched length {len(patched)} != original {len(original)} — refusing "
            f"(same-length in-place edit is the whole safety contract)"
        )

    _sweep_orphans(binp)
    fd, tmp_name = tempfile.mkstemp(prefix=binp.name + ".patch.", dir=str(binp.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(patched)
        verify(tmp.read_bytes())  # caller-supplied post-write check; raises on bad
        shutil.copymode(binp, tmp)
        orig = binp.with_name(binp.name + ".orig")
        if not orig.exists() or orig.stat().st_size != binp.stat().st_size:
            shutil.copy2(binp, orig)  # capture pristine-for-this-version bytes
        _atomic_swap(tmp, binp)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
