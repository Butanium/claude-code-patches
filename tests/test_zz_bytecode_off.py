#!/usr/bin/env python3
"""zz-bytecode-off: every module whose JS text was patched runs from source.

Structural, not behavioral: it diffs the binary against its pristine `.orig`
and reads the Bun module table. The behavioral proof is every other test in
this directory, which would all observe stock behavior if this failed.
  patched: >= 1 module differs from pristine and each has its bytecode length zeroed
  stock:   no module differs, or some patched module still carries bytecode
           (its text edit is inert)
Cross-platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import _bungraph  # noqa: E402
from _harness import INCONCLUSIVE, PATCHED, STOCK, Verdict, main  # noqa: E402

CHUNK = 1 << 20


def run(binary: Path) -> Verdict:
    orig = binary.with_name(binary.name + ".orig")
    if not orig.exists():
        return Verdict(INCONCLUSIVE, f"no pristine backup at {orig}")
    data, stock = binary.read_bytes(), orig.read_bytes()
    if len(data) != len(stock):
        return Verdict(INCONCLUSIVE, "binary and .orig differ in size")
    graph = _bungraph.parse(data)
    touched = {}
    for start in range(0, len(data), CHUNK):
        a, b = data[start:start + CHUNK], stock[start:start + CHUNK]
        if a == b:
            continue
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y and (m := graph.module_for(start + i)) is not None:
                touched.setdefault(m.index, m)
    if not touched:
        return Verdict(STOCK, "no module differs from pristine")
    live = [m for m in touched.values() if m.bytecode[1] != 0]
    if live:
        names = ", ".join(m.name.decode(errors="replace").rsplit("/", 1)[-1] for m in live)
        return Verdict(STOCK, f"{len(live)}/{len(touched)} patched modules still run bytecode: {names}")
    return Verdict(PATCHED, f"{len(touched)} patched modules, all run from source")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
