#!/usr/bin/env python3
"""Start the behavior suite in the background when the live binary changed.

Called by run_cli_patches.sh after the patches ran, when CLAUDE_CLI_PATCH_TESTS=1.
The binary is replaced whenever a patch applies (or claude updates), so its
path + size + mtime name one patched state; each state is tested once. The
suite runs detached (it takes minutes and starts real sessions), with a stock
control arm, and posts its table to ntfy.sh/$CLI_PATCH_TESTS_NTFY_TOPIC when
that is set. Results and the log land in the state dir:

    ${XDG_CACHE_HOME:-~/.cache}/claude-cli-patch-tests/<version>-<size>-<mtime>.{json,log}

Prints one line to stdout (SessionStart context) only when it starts a run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from _binpatch import candidate_binaries  # noqa: E402

LOCK_STALE_S = 2 * 3600


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "claude-cli-patch-tests"


def main() -> int:
    cands = candidate_binaries()
    if not cands:
        return 0
    binp = cands[0]
    if not binp.with_name(binp.name + ".orig").exists():
        return 0  # nothing is patched yet, or zz-bytecode-off already reported the missing backup
    st = binp.stat()
    key = f"{binp.name}-{st.st_size}-{st.st_mtime_ns}"
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    result, log, lock = d / f"{key}.json", d / f"{key}.log", d / f"{key}.lock"
    if result.exists():
        return 0
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < LOCK_STALE_S:
            return 0  # another session started this run
        lock.touch()

    cmd = [sys.executable, "-B", str(HERE / "run_tests.py"), "--binary", str(binp), "--control",
           "--json", str(result), "--release-lock", str(lock)]
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": open(log, "w"), "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    print(f"cli-patch tests: started the behavior suite for {binp} in the background "
          f"(results: {result}; log: {log})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
