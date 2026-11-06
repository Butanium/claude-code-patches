#!/usr/bin/env python3
"""CLI patch: forks may spawn subagents, and their boilerplate is rewritten.

Stock forks get a hard rule, "Do NOT spawn subagents with the Agent tool", in
the <fork-boilerplate> user message. The system prompt and the Agent tool
description repeat it as "execute directly, don't re-delegate". That is prompt
text only: the Agent tool works inside a fork, and the binary itself refuses
just fork-in-fork ("Fork is not available inside a forked worker"). A directive
saying "use subagents freely" loses to the hard rule, so forks do bulk reading
alone or through ad-hoc scripts.

This patch rewrites the boilerplate body and narrows both repeats to "do not
re-fork". The enforced fork-in-fork refusal and the spawn depth limit stay. The
report guideline tells the fork the parent already has the shared context, and
the "Be concise" guideline is gone.

The boilerplate is rebuilt whole, keeping its `${n}` slot. The <fork-boilerplate>
tags and the "Your directive: " line stay stock, because the CLI matches on them.

Anchors: "You are a worker fork." and "re-delegate" with "fork" before it.
Contract (cli-patches): stderr reports, exit 0; exit 1 if it can't be applied.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries, read_binary

START = b"You are a worker fork."
END = b"</${"
RULE = re.compile(
    rb'- Do NOT spawn subagents with the \$\{' + JSID + rb'\} tool\. The "default to forking" guidance is for '
    rb"the parent; you ARE the fork, execute directly\.(\$\{" + JSID + rb"\})\n"
)
REQUIRE = [b"Execute ONE directive, then stop.", b"Hard rules:", b"list the paths and commit hashes in your report."]
MAX_BODY = 2000

NEW_INTRO = b"You just split yourself as a fork"
NEW_BODY = (
    NEW_INTRO + b", the above is what happened before the split. "
    b"Think about yourself as a branch expanding and giving back a fruit to the main trunk :)\n\n"
    b"Hard rules:\n"
    b"- Do not re-fork; you ARE the fork. Your only task is the directive below.%s\n"
    b"- One shot: report once and stop. No follow-up questions, no proposed next steps, no waiting for the user.\n\n"
    b"Guidelines (your directive may override any of these):\n"
    b"- Stay in scope. Other forks may be handling adjacent work; if you spot something outside your directive, "
    b"note it in a sentence and move on.\n"
    b"- Open with one line restating your task, so the parent can spot scope drift at a glance.\n"
    b"- If you committed changes, list the paths and commit hashes in your report.\n"
    b"- Your report will be sent to the main branch, which has the same context as you had when you started, "
    b"so no need to repeat context or explanations already present."
)

# All three sites are template literals, so a `${""/*...*/}` expression pads them without changing the rendered text.
REPEATS = [
    (b"**If you ARE the fork** \\u2014 execute directly; do not re-delegate.", b"**If you ARE the fork** do not re-fork."),
    (b"If you are the fork, execute directly \\u2014 don't re-delegate.", b"If you are the fork, do not re-fork."),
]


PAD_HEAD, PAD_TAIL = b'${""/*', b"*/}"
PAD_MIN = len(PAD_HEAD) + len(PAD_TAIL)


def pad_expr(n: int) -> bytes:
    spaces = n - PAD_MIN
    assert spaces >= 0, n
    return PAD_HEAD + b" " * spaces + PAD_TAIL


def new_boilerplate(old: bytes) -> bytes:
    m = RULE.search(old)
    if m is None:
        raise RuntimeError("the 'Do NOT spawn subagents' rule changed; re-read the fork boilerplate")
    for tok in REQUIRE:
        if tok not in old:
            raise RuntimeError(f"{tok!r} missing from the fork boilerplate; upstream rewrote it")
    new = NEW_BODY % m.group(1)
    pad = len(old) - len(new) - 1
    if pad < PAD_MIN:
        raise RuntimeError(f"new boilerplate leaves {pad} bytes, needs {PAD_MIN}; trim NEW_BODY")
    new += pad_expr(pad) + b"\n"
    assert len(new) == len(old) and old.endswith(b"\n")
    return new


def main() -> int:
    binp = candidate_binaries()[0]
    data = read_binary(binp)
    if NEW_INTRO in data:
        print(f"fork-subagents: confirmed already patched ({binp})", file=sys.stderr)
        return 0
    if data.count(START) != 1:
        print(f"expected one {START!r} in {binp}, found {data.count(START)}; find the boilerplate via "
              "'<fork-boilerplate>' or 'Execute ONE directive'")
        return 1
    start = data.index(START)
    end = data.find(END, start, start + MAX_BODY)
    if end == -1:
        print(f"no closing tag within {MAX_BODY} bytes of {START!r}; the boilerplate changed shape")
        return 1
    try:
        patched = data[:start] + new_boilerplate(data[start:end]) + data[end:]
    except RuntimeError as e:
        print(f"fork-subagents: {e}")
        return 1
    for old, new in REPEATS:
        if old not in patched:
            print(f"{old!r} not found; grep 're-delegate' for the new wording of the no-delegation repeat")
            return 1
        patched = patched.replace(old, new + pad_expr(len(old) - len(new)))
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        if len(written) != len(data) or NEW_INTRO not in written or any(old in written for old, _ in REPEATS):
            raise RuntimeError("post-write verification failed; live binary untouched")

    apply_patch(binp, data, patched, _verify)
    print(f"fork-subagents: applied to {binp}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
