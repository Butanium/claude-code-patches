#!/usr/bin/env python3
"""Unpack the `claude` binary's embedded JS into a directory of real files.

Why: the bundle is a ~215 MB Bun standalone executable, so every `grep -ao` over
it costs 30-120 s and answers with an offset in a wall of minified code. Unpack
once (~2 s) and the normal Read/Grep tools work on per-module files instead,
with module names as orientation.

    ./clisrc.py                            # unpack the active CLI, print the directory
    ./clisrc.py --find 'fork gate is on'   # unpack if needed, then search it (regex)
    ./clisrc.py --find 'B1t()' -F -B 1500  # literal needle, 1500 chars of context before
    ./clisrc.py --fn B1t F1t               # brace-matched definitions of minified symbols

`--fn` is the tool for chasing a minified name: it finds `function NAME(`,
`class NAME`, a `NAME(args){` method, or a `NAME=` binding in any chunk and
prints the whole definition (brace-matched, wrapped), so tracing
`prompt_cache -> B1t -> tY().estimateRecacheTokens` is three calls, not a grep
window guessed by hand. Exported names are stable across chunks (Bun keeps
them), so a name imported into chunk A is found where chunk B defines it.

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
import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bungraph

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-cli-src"
TEXT_SUFFIXES = {".js", ".md", ".txt"}


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


def text_files(out: Path) -> Iterator[tuple[Path, str]]:
    for f in sorted(out.rglob("*")):
        if f.is_file() and f.suffix in TEXT_SUFFIXES:
            yield f, f.read_text(errors="replace")


def line_col(src: str, off: int) -> tuple[int, int]:
    line = src.count("\n", 0, off) + 1
    col = off - (src.rfind("\n", 0, off) + 1) + 1
    return line, col


def wrap(s: str, width: int) -> str:
    if width <= 0:
        return s
    return "\n".join(s[i : i + width] for i in range(0, len(s), width)) if s else s


def scan_definition(src: str, start: int) -> int:
    """End offset of the JS definition starting at `start`.

    Tracks {}/()/[] depth and skips string / template literals. Stops at a
    depth-0 `;` or `,`, or right after a `}` that closes to depth 0 unless the
    next char keeps the expression going (`.` `(` `[` `=` `?` `:`). Regex
    literals are not understood; a `/[{]/` inside a body can skew the depth.
    """
    depth = 0
    i = start
    n = len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            q = c
            i += 1
            while i < n and src[i] != q:
                i += 2 if src[i] == "\\" else 1
            i += 1
            continue
        if c in "{([":
            depth += 1
        elif c in "})]":
            depth -= 1
            if depth == 0 and c == "}":
                nxt = src[i + 1 : i + 2]
                if nxt not in {".", "(", "[", "=", "?", ":"}:
                    return i + 1
            if depth < 0:
                return i
        elif depth == 0 and c in ";,\n":
            return i
        i += 1
    return n


def definition_pattern(name: str) -> re.Pattern[str]:
    q = re.escape(name)
    return re.compile(
        rf"(?:"
        rf"(?<![\w$.])(?:async\s+)?function\s*\*?\s*{q}\s*\("  # function NAME(
        rf"|(?<![\w$.])class\s+{q}(?![\w$])"  # class NAME
        rf"|(?<![\w$.])(?:var|let|const)\s+{q}\s*=(?![=>])"  # var NAME=
        rf"|(?<=[,;])\s*{q}\s*=(?![=>])"  # ,NAME=  (later binding in a comma list)
        # method NAME(args){ -- only after { } ; , so `if(NAME(x)){` is not one
        rf"|(?<=[{{}};,])(?:async\s+|static\s+|get\s+|set\s+|\*\s*)?{q}\s*\([^()]*\)\s*\{{"
        rf")"
    )


def find_definitions(sources: dict[Path, str], name: str) -> Iterator[tuple[Path, int, str]]:
    pat = definition_pattern(name)
    for f, src in sources.items():
        if f.suffix != ".js" or name not in src:
            continue
        for m in pat.finditer(src):
            yield f, m.start(), src[m.start() : scan_definition(src, m.start())]


def cmd_fn(out: Path, names: list[str], max_chars: int, width: int) -> int:
    sources = dict(text_files(out))
    rc = 1
    for name in names:
        hits = list(find_definitions(sources, name))
        if not hits:
            print(f"=== {name}: no definition found ===")
            continue
        rc = 0
        for f, off, body in hits:
            src_line, src_col = line_col(sources[f], off)
            print(f"=== {name}  {f.relative_to(out)}:{src_line}:{src_col}  (offset {off}, {len(body)} chars) ===")
            shown = body if max_chars <= 0 or len(body) <= max_chars else body[:max_chars]
            print(wrap(shown, width))
            if len(shown) < len(body):
                print(f"… [truncated {len(body) - len(shown)} chars; --max 0 shows all]")
            print()
    return rc


def cmd_find(out: Path, pattern: str, fixed: bool, before: int, after: int, width: int) -> int:
    pat = re.compile(re.escape(pattern) if fixed else pattern)
    found = False
    for f, src in text_files(out):
        for m in pat.finditer(src):
            found = True
            line, col = line_col(src, m.start())
            lo, hi = max(0, m.start() - before), min(len(src), m.end() + after)
            print(f"{f.relative_to(out)}:{line}:{col}: (offset {m.start()})")
            print(wrap(src[lo:hi].replace("\n", "⏎"), width))
            print()
    return 0 if found else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", type=Path, help="bundle to unpack (default: the active claude)")
    ap.add_argument("--force", action="store_true", help="re-unpack even if cached")
    ap.add_argument("--find", metavar="PATTERN", help="search the unpacked tree for PATTERN (Python regex)")
    ap.add_argument("-F", "--fixed", action="store_true", help="treat --find PATTERN as a literal string")
    ap.add_argument("-B", "--before", type=int, default=120, help="chars of context before a --find hit (default 120)")
    ap.add_argument("-A", "--after", type=int, default=200, help="chars of context after a --find hit (default 200)")
    ap.add_argument("-C", "--context", type=int, help="set both -B and -A")
    ap.add_argument("--fn", metavar="NAME", nargs="+", help="print the brace-matched definition(s) of each NAME")
    ap.add_argument("--max", type=int, default=6000, help="cap chars printed per --fn definition (0 = no cap)")
    ap.add_argument("--wrap", type=int, default=200, help="wrap output lines at N chars (0 = no wrap)")
    args = ap.parse_args()

    out = unpack(args.binary or active_cli(), force=args.force)
    if args.context is not None:
        args.before = args.after = args.context
    if args.fn:
        sys.exit(cmd_fn(out, args.fn, args.max, args.wrap))
    if args.find is not None:
        sys.exit(cmd_find(out, args.find, args.fixed, args.before, args.after, args.wrap))
    print(out)


if __name__ == "__main__":
    main()
