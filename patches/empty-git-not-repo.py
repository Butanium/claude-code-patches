#!/usr/bin/env python3
"""CLI patch: an empty `.git` directory no longer makes a folder a git repository root.

Claude Code finds a project's root by walking up from the working directory and
stopping at the first directory that holds a `.git`. The check it uses accepts
any file or directory:

    function We(e,n){try{let r=Vn(e);if(r.isSymbolicLink()){...}
                     return r.isDirectory()||r.isFile()}catch{return!1}}

git itself ignores a `.git` directory without `HEAD`. Claude Code does not, so
an empty `.git` left by another tool makes that folder its own project. Folder
trust and the per-project config are keyed on that root. A session started
there shows the folder-trust prompt again, even inside a trusted repository,
and stores its config under the subfolder. Sandboxes that mask `.git` with a
tmpfs create such directories on the host (posit-dev/mcp-console#181,
openai/codex#28631).

The patch rewrites the final, non-symlink return of that check to

    return r.isFile()||!!Vn(e+"/HEAD")

A `.git` file (worktree, submodule) still counts. A `.git` directory counts
only when `HEAD` exists in it; a missing `HEAD` makes the lstat throw, which the
existing catch turns into false. The symlinked-`.git` branch is unchanged.
Every caller of the check (the git-root walk, worktree root resolution, the
git-config path lookup) wants "is this a real git dir", so all of them get
git's answer.

Anchors: the checker is found through the stable log event name
`find_git_root_started` in the function that calls it; the minified names
(`We`, `Vn`, parameter names) are captured, not hard-coded. The patched code is
its own marker.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (the runner relays stdout to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries, read_binary, splice

NAME = "empty-git-not-repo"
ANCHOR = b'find_git_root_started'
# The git-root walk: ...find_git_root_started");...let s=L(r,".git");if(o++,We(s,r))...
CALLER = re.compile(rb'find_git_root_started.{0,400}?"\.git"\);if\(' + JSID + rb'\+\+,(' + JSID + rb')\(', re.S)


def checker_def(name: bytes) -> re.Pattern[bytes]:
    """The checker's definition; group 'ret' is the final non-symlink return expression."""
    n = re.escape(name)
    return re.compile(
        rb'function ' + n + rb'\((?P<p>' + JSID + rb'),' + JSID + rb'\)\{try\{let (?P<r>' + JSID + rb')=(?P<st>'
        + JSID + rb')\((?P=p)\);if\((?P=r)\.isSymbolicLink\(\)\)\{[^}]*\}return (?P<ret>[^}]*)\}catch\{return!1\}\}'
    )


def fail(msg: str) -> int:
    print(
        f"{NAME}: {msg} — upstream code changed; re-investigate around the log event "
        f"'find_git_root_started' (the git-root walk) and the .git checker it calls",
    )
    return 1


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = read_binary(binp)
        if ANCHOR in data:
            target = (binp, data)
            break
    if target is None:
        return fail(f"anchor {ANCHOR!r} not found in any candidate binary "
                    f"({[str(p) for p in candidate_binaries()]})")
    binp, data = target

    callers = {m.group(1) for m in CALLER.finditer(data)}
    if len(callers) != 1:
        return fail(f"expected one .git checker name in the git-root walk, found {sorted(callers)}")
    name = callers.pop()
    defs = list(checker_def(name).finditer(data))
    if len(defs) != 1:
        return fail(f"expected exactly 1 definition of the checker {name!r}, found {len(defs)}")
    m = defs[0]
    r, p, st, ret = m.group("r"), m.group("p"), m.group("st"), m.group("ret")

    stock = r + b'.isDirectory()||' + r + b'.isFile()'
    new = r + b'.isFile()||!!' + st + b'(' + p + b'+"/HEAD")'
    if ret.rstrip(b' ') == new:
        print(f"{NAME}: confirmed already patched ({binp})", file=sys.stderr)
        return 0
    if ret != stock:
        return fail(f"checker {name!r} returns {ret!r}, expected {stock!r}")
    pad = len(stock) - len(new)
    if pad < 0:
        return fail(f"replacement {new!r} is {-pad} bytes longer than {stock!r}")
    rep = new + b' ' * pad  # spaces between the expression and the closing brace are inert
    assert len(rep) == len(stock)

    start = m.start("ret")
    patched = splice(data, start, start + len(stock), rep)
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        ok = len(written) == len(data) and len(list(checker_def(name).finditer(written))) == 1 \
            and written.count(rep) >= 1 and written.count(stock) == data.count(stock) - 1
        if not ok:
            raise RuntimeError(f"{NAME}: post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)
    print(f"{NAME}: applied to {binp} (checker {name.decode()}: {stock.decode()} -> {rep.decode()!r})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
