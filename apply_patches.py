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
import hashlib
import io
import json
import os
import runpy
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
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


CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "claude-cli-patches"


def fingerprint(binp: Path, patches: list[Path], label: str, lint: bool) -> str:
    """What a pass's outcome depends on: the binary and its `.orig` backup, the patch
    files and everything next to them (a private patch dir's own helpers), and this
    repo's helper modules. Any write to the binary changes its mtime."""
    files = {binp, binp.with_name(binp.name + ".orig"), *HERE.glob("*.py")}
    for d in {p.resolve().parent for p in patches}:
        files.update(f for f in d.iterdir() if f.is_file())

    def st(f: Path) -> list:
        try:
            s = f.stat()
            return [str(f), s.st_size, s.st_mtime_ns]
        except OSError:
            return [str(f), None, None]

    return json.dumps([label, lint, [str(p) for p in patches], sorted(st(f) for f in files)])


class Pass:
    def __init__(self, label: str):
        self.label = label
        self.staged: list[str] = []  # patches whose edits wait for the next commit
        self.commit_failed = False
        self.out: list[str] = []  # what reached stdout / stderr, replayed while nothing changes
        self.err: list[str] = []

    def say(self, text: str, err: bool = False) -> None:
        (self.err if err else self.out).append(text)
        print(text, file=sys.stderr if err else sys.stdout)

    def report(self, name: str, path: Path, rc: int, out: str) -> None:
        out = out.rstrip("\n")
        if rc == 0:
            self.say(f"cli-patch {name}{self.label}: {out}", err=True)
        else:
            self.say(f"CLI patch '{name}' FAILED (exit {rc}) — its behavior change is NOT active "
                     f"for the claude binary{self.label or ' currently installed'}:\n{out}\nScript: {path}")

    def commit(self) -> None:
        try:
            _binpatch.commit_batch()
        except Exception as e:
            self.commit_failed = True
            self.say(f"CLI patches{self.label}: writing the patched binary failed ({e!r}), so these "
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

    def lint(self, patches: list[Path]) -> None:
        import lint_patches
        for path in patches:
            if path.suffix != ".py":
                continue
            try:
                findings = lint_patches.lint(path)
            except Exception as e:
                findings = [f"{path.name}: could not lint it ({e!r})"]
            for line in findings:
                self.say(f"cli-patches lint: {line}")

    def save(self, cache: Path, key: str) -> None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"key": key, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                                       "out": self.out, "err": self.err}), encoding="utf-8")
            os.replace(tmp, cache)
        except OSError:
            pass  # the cache only saves time


def replay(cache: Path, key: str, label: str) -> bool:
    """If the last pass on this binary saw exactly this state, print what it printed
    (failures included, so they keep reaching Claude) and report a hit."""
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if saved.get("key") != key:
        return False
    for line in saved["err"]:
        print(line, file=sys.stderr)
    for line in saved["out"]:
        print(line)
    print(f"cli-patches{label}: binary and patches unchanged since the pass at {saved['when']}; "
          f"replayed its output", file=sys.stderr)
    return True


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--label", default="", help="appended to each patch name in messages, e.g. ' [claude.exe]'")
    ap.add_argument("--lint", action="store_true", help="also run lint_patches.py on the patches")
    ap.add_argument("--no-cache", action="store_true", help="run the pass even if nothing changed since the last one")
    ap.add_argument("patches", nargs="*", type=Path)
    a = ap.parse_args()

    try:
        binp = _binpatch.candidate_binaries()[0]
    except (IndexError, RuntimeError):  # no binary, or a bogus CLAUDE_CLI_PATCH_TARGET:
        binp = None  # run unbatched and uncached; each patch reports it in its own words
    cache = None
    if binp is not None:
        cache = CACHE_DIR / f"pass-{hashlib.sha1(str(binp).encode()).hexdigest()[:16]}.json"
        if not a.no_cache and replay(cache, fingerprint(binp, a.patches, a.label, a.lint), a.label):
            return 0

    p = Pass(a.label)
    try:
        with _binpatch.batch(binp) if binp is not None else contextlib.nullcontext():
            for path in a.patches:
                p.run(path)
            p.commit()
        if a.lint:
            p.lint(a.patches)
    except Exception:
        print(f"CLI patches{a.label}: apply_patches.py crashed, so which patches are active is unknown:")
        traceback.print_exc(file=sys.stdout)
        return 1
    if cache is not None and not p.commit_failed:
        p.save(cache, fingerprint(binp, a.patches, a.label, a.lint))  # after: the commit moved the mtime
    return 0


if __name__ == "__main__":
    sys.exit(main())
