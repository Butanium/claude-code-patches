#!/usr/bin/env python3
"""Unpack the `claude` binary's embedded JS into a directory of real files.

Why: the bundle is a ~215 MB Bun standalone executable, so every `grep -ao` over
it costs 30-120 s and answers with an offset in a wall of minified code. Unpack
once (~2 s) and the normal Read/Grep tools work on per-module files instead,
with module names as orientation.

    ./clisrc.py                  # unpack the active CLI, print the directory
    ./clisrc.py --find 'fork gate is on'   # unpack if needed, then grep it

Output goes to ~/.cache/claude-cli-src/<version>/ (skipped if already there,
so re-running is free). Per-version, so an update just unpacks anew.

Bytecode caveat from _bungraph: since Bun 1.4.1 the text is not what runs. For
READING the code that is fine -- the text and the bytecode are compiled from the
same source. It matters only when patching (see README).
"""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bungraph

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-cli-src"


def active_cli() -> Path:
    """The binary `claude` actually runs, resolving the launcher shim."""
    exe = shutil.which("claude")
    if exe is None:
        raise SystemExit("no `claude` on PATH")
    p = Path(exe).resolve()
    if p.stat().st_size > 50_000_000:
        return p
    # A small file is the version-picker shim; the real binary is the newest
    # thing under the versions dir it dispatches to.
    versions = Path.home() / ".local/share/claude/versions"
    picks = sorted(versions.glob("*"), key=lambda f: f.stat().st_mtime) if versions.is_dir() else []
    if not picks:
        raise SystemExit(f"{p} is not a bundle and no versions found under {versions}")
    return picks[-1]


def unpack(binary: Path, force: bool = False) -> Path:
    out = CACHE / binary.name
    if out.is_dir() and not force and any(out.iterdir()):
        return out
    data = binary.read_bytes()
    graph = _bungraph.parse(data)
    if out.is_dir():
        shutil.rmtree(out)
    n = 0
    for m in graph.modules:
        name = m.name.decode("utf-8", "replace")
        rel = re.sub(r"^(/\$bunfs/|B:[/\\]~BUN[/\\])", "", name).replace("\\", "/").lstrip("/")
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        off, length = m.contents
        dest.write_bytes(data[off : off + length])
        n += 1
    print(f"unpacked {n} modules from {binary.name} -> {out}", file=sys.stderr)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", type=Path, help="bundle to unpack (default: the active claude)")
    ap.add_argument("--force", action="store_true", help="re-unpack even if cached")
    ap.add_argument("--find", metavar="PATTERN", help="grep the unpacked tree for PATTERN")
    ap.add_argument("-C", "--context", type=int, default=0, help="grep context lines")
    args = ap.parse_args()

    out = unpack(args.binary or active_cli(), force=args.force)
    if args.find is None:
        print(out)
        return
    # Minified bundles have enormous lines; cap what a match prints so one hit
    # cannot dump a megabyte into a terminal (or a context window).
    cmd = ["grep", "-rn", "-oa", f"-C{args.context}", f".\\{{0,120\\}}{args.find}.\\{{0,200\\}}", str(out)]
    sys.exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()
