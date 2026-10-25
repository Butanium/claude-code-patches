#!/usr/bin/env python3
"""Start a Claude session in tmux to repair patches that stopped applying.

Called by run_cli_patches.sh after the patch passes when CLAUDE_CLI_PATCH_AUTOREPAIR=1,
with the extra target binaries as arguments; the installed binary is found the usual
way. It reads each binary's pass record (the `failed` list apply_patches.py saves) and,
for failures no repair session was started for yet, starts one interactive `claude` in
a detached tmux session whose first prompt carries the failure messages. That session
reports through ntfy and either closes itself once every fix is tested and committed,
or asks its question and waits at its prompt for an answer.

One attempt per (binary basename, patch). The basename is the version on both install
layouts (`versions/2.1.293`, `remote/ccd-cli/2.1.308-<hash>`), so a patch that breaks
again on the next update gets a new attempt, while the repair session's own writes to
the binary (which change its mtime, not its name) don't start another. While a repair
session is still running, no second one starts; failures that are new meanwhile wait
for it to end.

Environment (all optional):
  CLI_PATCH_REPAIR_NTFY_TOPIC    ntfy.sh topic the session reports to; without it the
                                 session leaves its report as its last message
  CLAUDE_CLI_PATCH_REPAIR_ARGS   arguments for `claude`, shell-quoted
                                 (default: --permission-mode bypassPermissions; the
                                 session is unattended, so it must never prompt)
  CLAUDE_CLI_PATCH_REPAIR_CMD    command that starts claude (default: claude); it runs
                                 under `bash -ic`, so shell wrapper functions apply
  CLAUDE_CLI_PATCH_REPAIR_CWD    directory the session starts in (default: this repo)

Prints a line to stdout (SessionStart context) when it starts a session, and, while the
current failures are someone's job already, a line naming that session, so the session
that reads the failure doesn't start a repair of its own. Linux/macOS only (tmux).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _binpatch import candidate_binaries  # noqa: E402
from apply_patches import cache_path  # noqa: E402

GUARD = "CLAUDE_CLI_PATCH_REPAIR_SESSION"
TOPIC = "CLI_PATCH_REPAIR_NTFY_TOPIC"
LOCK_STALE_S = 120


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "claude-cli-patch-repair"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def current_failures(extra: list[Path]) -> list[tuple[Path, dict]]:
    targets = []
    try:
        targets.append(candidate_binaries()[0])
    except (IndexError, RuntimeError):
        pass
    targets += extra
    out = []
    for binp in dict.fromkeys(targets):
        rec = load_json(cache_path(binp))
        if rec.get("binary") == str(binp):
            out += [(binp, f) for f in rec.get("failed", [])]
    return out


def tmux_alive(name: str | None) -> bool:
    return bool(name) and subprocess.run(["tmux", "has-session", "-t", f"={name}"],
                                         capture_output=True).returncode == 0


def build_prompt(failures: list[tuple[Path, dict]], tname: str, topic: bool) -> str:
    if topic:
        notify = (f'with `curl -s -H "Title: cli-patch repair" -d "<message>" "ntfy.sh/${TOPIC}"` '
                  f"(the topic is exported in this session's environment)")
    else:
        notify = "as your final message (no notification channel is configured)"
    blocks = "\n\n".join(
        f"### `{f['name']}` on `{binp}`\n\nScript: `{f['path']}`\n\n```\n{f['message']}\n```"
        for binp, f in failures)
    return f"""\
You were started automatically by the cli-patches SessionStart runner (`{HERE / 'auto_repair.py'}`): \
the patches below stopped applying, most likely because a claude update changed the code they \
anchor on. Nobody is watching this session live. It runs in tmux session `{tname}`.

## What failed

{blocks}

## The job

Re-derive each failing patch so it applies to that binary and changes the same behavior it \
changed before, while still applying to the older binaries it supports (keep the old anchor next \
to the new one when the code changed shape). The repo is `{HERE}`; its README ("How it works", \
"Writing your own") states the patch contract, and the other patches are worked examples. The \
previous version's binary and its `.orig` often still sit next to the new one; comparing the code \
around the anchor between the two versions is usually the quickest way to see what moved.

- Develop against a copy: `CLAUDE_CLI_PATCH_TARGET=<copy> python3 patches/<name>` aims a patch at \
any file.
- A patch reporting "applied" proves nothing: the binary runs precompiled bytecode, and only a \
behavior test from a fresh process shows the change is live. Run \
`python3 tests/run_tests.py --binary <patched binary> --control --only <patch>`. It must observe \
the change on the patched binary and the stock behavior on its `.orig`.
- This session runs whatever `claude` is on PATH, which may be a different version from the binary \
you are repairing, and a patch only affects processes started after it is applied. Verify through \
the test harness, not by restarting yourself.
- Once the tests pass, run `run_cli_patches.sh` so the live binaries get the fix, and commit the \
change, following the conventions your instructions give for this repo (pushing and bumping a \
parent repo's submodule pointer included).

## When to stop and ask

Ask instead of guessing whenever the fix is not obvious: the behavior the patch changes was \
removed or redesigned upstream, the anchor matches more than one plausible site, the byte budget \
does not fit without changing what the patch does, or the behavior test cannot run or fails in a \
way you cannot explain. A patch that applies but whose behavior test you could not run is not \
fixed; ask about it too.

To ask, send the question {notify}, naming tmux session `{tname}` so it can be attached, then end \
your turn. This session stays open at its prompt and the answer will arrive here; do not close it. \
Fix what you can before asking, and put every open question in one message.

## When everything is fixed

Only when every failure above applies and its behavior test passes on both arms: commit, send a \
short summary {notify}, and then close this session with `tmux kill-session -t '{tname}'` (the \
transcript is kept). Say in the summary that a claude process that is already running keeps the \
old code until it restarts, which for the Desktop app's bundled CLI means restarting the app.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("targets", nargs="*", type=Path, help="extra target binaries (the runner's _bins)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the tmux command and the prompt; start nothing, record nothing")
    a = ap.parse_args()
    if os.environ.get("CLAUDE_CLI_PATCH_TARGET") or os.environ.get(GUARD):
        return 0  # a dev copy is being patched, or this is the repair session itself

    failures = current_failures(a.targets)
    if not failures:
        return 0
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    lock = d / "lock"
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < LOCK_STALE_S:
            return 0  # another session start is deciding right now
        lock.touch()
    try:
        return decide(failures, d, a.dry_run)
    finally:
        lock.unlink(missing_ok=True)


def decide(failures: list[tuple[Path, dict]], d: Path, dry_run: bool) -> int:
    state_file = d / "state.json"
    attempts: dict = load_json(state_file).get("attempts", {})
    have_tmux = shutil.which("tmux") is not None
    keyed = [(f"{binp.name}/{f['name']}", binp, f) for binp, f in failures]
    new = [(binp, f) for k, binp, f in keyed if k not in attempts]

    earlier: dict[tuple, set] = {}  # (tmux, when, error) of an earlier attempt -> its patches still failing
    for k, _, f in keyed:
        if k in attempts:
            att = attempts[k]
            earlier.setdefault((att.get("tmux"), att.get("when"), att.get("error")), set()).add(f["name"])
    # Any repair session still open blocks a new one, including one started for an older version.
    running = [t for t in dict.fromkeys(a.get("tmux") for a in attempts.values()) if have_tmux and tmux_alive(t)]
    for (t, when, err), names in earlier.items():
        if err:
            what = f"starting a repair session at {when} failed ({err}); still failing"
        elif t in running:
            what = f"the repair session started at {when} in tmux '{t}' is working on"
        else:
            what = f"the repair session started at {when} in tmux '{t}' has ended; still failing"
        print(f"cli-patch auto-repair: {what}: {', '.join(sorted(names))}. "
              f"No new repair session starts for these until the next claude version.")

    if not new:
        return 0
    names = ", ".join(sorted({f["name"] for _, f in new}))
    if not have_tmux:
        print(f"cli-patch auto-repair: tmux is not on PATH, so no repair session was started for {names}.")
        return 0
    if running:
        print(f"cli-patch auto-repair: {names} also failed; that waits until the repair session in "
              f"tmux '{running[0]}' ends.")
        return 0

    tname = time.strftime("cli-patch-repair-%m%d-%H%M")
    while tmux_alive(tname):
        tname += "x"
    topic = os.environ.get(TOPIC, "")
    prompt_file = d / f"{tname}.prompt.md"
    exports = {GUARD: "1", **({TOPIC: topic} if topic else {})}
    cwd = Path(os.environ.get("CLAUDE_CLI_PATCH_REPAIR_CWD") or HERE).expanduser()
    args = os.environ.get("CLAUDE_CLI_PATCH_REPAIR_ARGS", "--permission-mode bypassPermissions")
    claude = os.environ.get("CLAUDE_CLI_PATCH_REPAIR_CMD", "claude")
    # tmux gives a new session the server's environment, not ours, so the exports go in the command.
    cmd = "; ".join([f"export {k}={shlex.quote(v)}" for k, v in exports.items()]
                    + [f"{claude} {args} --name 'cli-patch repair' \"$(cat {shlex.quote(str(prompt_file))})\""])
    tmux = ["tmux", "new-session", "-d", "-s", tname, "-c", str(cwd), "bash", "-ic", cmd]
    prompt = build_prompt(new, tname, bool(topic))
    if dry_run:
        print(shlex.join(tmux), prompt, sep="\n\n")
        return 0

    prompt_file.write_text(prompt, encoding="utf-8")
    r = subprocess.run(tmux, capture_output=True, text=True)
    when = time.strftime("%Y-%m-%d %H:%M")
    err = None if r.returncode == 0 else (r.stderr.strip() or f"tmux exited {r.returncode}")
    for binp, f in new:
        attempts[f"{binp.name}/{f['name']}"] = {"when": when, "tmux": tname, **({"error": err} if err else {})}
    tmp = state_file.with_name(f"state.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"attempts": attempts}, indent=1), encoding="utf-8")
    os.replace(tmp, state_file)
    if err:
        print(f"cli-patch auto-repair: could not start a repair session for {names}: {err}")
    else:
        print(f"cli-patch auto-repair: started a repair session for {names} in tmux '{tname}' "
              f"(prompt: {prompt_file}). Don't start another repair of these patches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
