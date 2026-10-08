#!/usr/bin/env python3
"""Run cli-patches in one process, reading and writing the binary once per pass.

run_cli_patches.sh calls this with the patches in apply order. Each Python patch
runs as `__main__` (a patch is still a plain script) while `_binpatch` is in
batch mode: `read_binary()` hands out the in-memory copy, `apply_patch()` only
replaces it, and the result is written, re-read, compared and swapped in once at
the end. Non-Python patches (.sh, executables) work on the file, so the batch is
committed before each one and re-read after it.

Output follows the runner's contract: a `cli-patch <name>: <output>` line per
success on stderr, a failure block per failure on stdout (SessionStart stdout is
injected into Claude's context). Exits 0 unless the driver itself breaks.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import runpy
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _binpatch  # noqa: E402


def run_python(path: Path) -> tuple[int, str]:
    """Run `path` as __main__ with stdout and stderr captured together (the
    runner's `2>&1`). Lets `_binpatch.StaleRead` through to the caller."""
    buf = io.StringIO()
    rc = 0
    argv = sys.argv
    sys.argv = [str(path)]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                runpy.run_path(str(path), run_name="__main__")
            except SystemExit as e:
                if e.code is None or isinstance(e.code, int):
                    rc = e.code or 0
                else:
                    print(e.code, file=sys.stderr)
                    rc = 1
            except _binpatch.StaleRead:
                raise
            except Exception:
                traceback.print_exc()
                rc = 1
    finally:
        sys.argv = argv
    return rc, buf.getvalue()


def run_other(path: Path) -> tuple[int, str]:
    if path.suffix == ".sh":
        cmd = ["bash", str(path)]
    elif os.access(path, os.X_OK):
        cmd = [str(path)]
    else:
        return 126, "no interpreter for this extension and the file is not executable"
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    except OSError as e:
        return 126, f"could not run it: {e}"
    return p.returncode, p.stdout


class Pass:
    def __init__(self, label: str):
        self.label = label
        self.staged: list[str] = []  # patches whose edits wait for the next commit

    def report(self, name: str, path: Path, rc: int, out: str) -> None:
        out = out.rstrip("\n")
        if rc == 0:
            print(f"cli-patch {name}{self.label}: {out}", file=sys.stderr)
        else:
            print(f"CLI patch '{name}' FAILED (exit {rc}) — its behavior change is NOT active "
                  f"for the claude binary{self.label or ' currently installed'}:")
            print(out)
            print(f"Script: {path}")

    def commit(self) -> None:
        try:
            _binpatch.commit_batch()
        except Exception as e:
            print(f"CLI patches{self.label}: writing the patched binary failed ({e!r}), so these "
                  f"patches' edits were not written and are NOT active: {', '.join(self.staged)}")
            _binpatch.reload_batch()
        self.staged.clear()

    def run(self, path: Path) -> None:
        b = _binpatch._batch
        before = b.data if b else None
        if path.suffix != ".py":
            self.commit()
            rc, out = run_other(path)
            _binpatch.reload_batch()
            before = None
        else:
            try:
                rc, out = run_python(path)
            except _binpatch.StaleRead:
                # A patch that reads the file itself: give it the file, up to date.
                self.commit()
                with _binpatch.outside_batch():
                    rc, out = run_python(path)
                _binpatch.reload_batch()
                before = None
        if b is not None and before is not None and b.data is not before:
            self.staged.append(path.name)
        self.report(path.name, path, rc, out)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--label", default="", help="appended to each patch name in messages, e.g. ' [claude.exe]'")
    ap.add_argument("patches", nargs="*", type=Path)
    a = ap.parse_args()

    p = Pass(a.label)
    try:
        binp = _binpatch.candidate_binaries()[0]
        batch = _binpatch.batch(binp)
    except (IndexError, RuntimeError):  # no binary, or a bogus CLAUDE_CLI_PATCH_TARGET
        batch = contextlib.nullcontext()  # each patch reports it in its own words
    try:
        with batch:
            for path in a.patches:
                p.run(path)
            p.commit()
    except Exception:
        print(f"CLI patches{a.label}: apply_patches.py crashed, so which patches are active is unknown:")
        traceback.print_exc(file=sys.stdout)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
