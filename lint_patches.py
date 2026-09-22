#!/usr/bin/env python3
"""Flag `\\w` in patch regexes that isn't inside a `$`-aware character class.

The minifier hands out identifiers like `R$` / `$e`; a bare `\\w+` capture
stops matching them and the patch silently bails on the next build (this broke
shutdown-reason on 2.1.280). Use `_binpatch.JSID` or `[$\\w]+` instead.

Prints one line per finding to stdout (the runner relays it to Claude's
context); exits 0 either way. Docstrings are skipped.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# a `\w` either outside any [...] class, or inside one that lacks `$`
CLASS_RE = re.compile(r"\[(?:\\.|[^\]\\])*\]")


def bad_w(s: str) -> bool:
    for cls in CLASS_RE.findall(s):
        if "\\w" in cls and "$" not in cls:
            return True
    return "\\w" in CLASS_RE.sub("", s)


def lint(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), str(path))
    docstrings = {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and id(n) not in docstrings and isinstance(n.value, (str, bytes)):
            s = n.value.decode("latin-1") if isinstance(n.value, bytes) else n.value
            if bad_w(s):
                out.append(f"{path.name}:{n.lineno}: `\\w` can't match `$` in minified identifiers — use JSID or [$\\w]")
    return out


def main(dirs: list[str]) -> int:
    for d in dirs:
        for p in sorted(Path(d).glob("*.py")):
            for line in lint(p):
                print(f"cli-patches lint: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or [str(Path(__file__).resolve().parent / "patches")]))
