#!/usr/bin/env python3
"""Run the patch behavior tests against one claude binary and print a pass/fail table.

    python3 tests/run_tests.py --binary ~/.local/share/claude/versions/2.1.280
    python3 tests/run_tests.py --binary <path> --control          # + stock arm from <path>.orig
    python3 tests/run_tests.py --binary <path> --only shutdown-reason,idle-notif
    python3 tests/run_tests.py --binary <path> --ntfy <topic>     # also post the table to ntfy.sh

A test passes when it observes the patched behavior on --binary and, with
--control, the stock behavior on an executable copy of <binary>.orig (a test
that also "passes" on stock proves nothing). Tests live next to this file as
test_<patch>.py; extra test dirs come from --tests-dir and from the `tests/`
sibling of every CLAUDE_CLI_PATCHES_EXTRA_DIRS entry (private patch repos).

Each test module exposes `run(binary) -> Verdict`; shared expensive sessions
(one team session, one baseline `-p` turn) run once per binary per invocation.
Tests that set NEEDS_TMUX need tmux (Linux/macOS); they are skipped where it
is missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _harness import INCONCLUSIVE, PATCHED, STOCK, Verdict, binary_version, stock_control  # noqa: E402


def extra_test_dirs() -> list[Path]:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    out = []
    for entry in filter(None, os.environ.get("CLAUDE_CLI_PATCHES_EXTRA_DIRS", "").split(":")):
        p = Path(entry).expanduser()
        p = p if p.is_absolute() else config / p
        if (p.parent / "tests").is_dir():
            out.append(p.parent / "tests")
    return out


def load_tests(dirs: list[Path]) -> dict[str, object]:
    tests = {}
    for d in dirs:
        sys.path.insert(0, str(d))
        for f in sorted(d.glob("test_*.py")):
            spec = importlib.util.spec_from_file_location(f"clipatch_{f.stem}_{abs(hash(d))}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            name = getattr(mod, "PATCH", f.stem[len("test_"):].replace("_", "-"))
            tests[name] = mod
    return tests


def run_one(mod, binary: Path) -> Verdict:
    try:
        return mod.run(binary)
    except Exception as e:  # a crashed test is a finding, not a crash of the suite
        return Verdict(INCONCLUSIVE, f"test crashed: {e!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--binary", required=True, type=Path)
    ap.add_argument("--control", action="store_true", help="also run each test on <binary>.orig, expecting stock")
    ap.add_argument("--only", default="", help="comma-separated patch names")
    ap.add_argument("--tests-dir", action="append", type=Path, default=[])
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--json", type=Path, help="write the results here")
    ap.add_argument("--ntfy", default=os.environ.get("CLI_PATCH_TESTS_NTFY_TOPIC", ""),
                    help="ntfy.sh topic to post the table to (default: $CLI_PATCH_TESTS_NTFY_TOPIC)")
    ap.add_argument("--release-lock", type=Path, help=argparse.SUPPRESS)  # after_patch.py's run lock
    a = ap.parse_args()
    try:
        return _main(a)
    finally:
        if a.release_lock:
            a.release_lock.unlink(missing_ok=True)


def _main(a: argparse.Namespace) -> int:
    binary = a.binary.expanduser().absolute()
    tests = load_tests([HERE, *a.tests_dir, *extra_test_dirs()])
    if a.only:
        wanted = {w.strip().removesuffix(".py") for w in a.only.split(",")}
        tests = {k: v for k, v in tests.items() if k in wanted}
    has_tmux = shutil.which("tmux") is not None

    arms = [("patched", binary, PATCHED)]
    control = None
    if a.control:
        control = stock_control(binary)
        arms.append(("stock", control, STOCK))

    jobs = []
    for name, mod in tests.items():
        for arm, b, expect in arms:
            jobs.append((name, mod, arm, b, expect))

    t0 = time.time()
    results: dict[tuple[str, str], tuple[Verdict, str]] = {}

    def work(job):
        name, mod, arm, b, expect = job
        if getattr(mod, "NEEDS_TMUX", False) and not has_tmux:
            return job, Verdict(INCONCLUSIVE, "skipped: tmux not available"), expect
        return job, run_one(mod, b), expect

    try:
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            for job, v, expect in ex.map(work, jobs):
                results[(job[0], job[2])] = (v, expect)
                print(f"  {job[0]:24} {job[2]:7} {v.status:12} {v.detail}", flush=True)
    finally:
        if control is not None:  # a full binary copy; don't leave it in a RAM-backed /tmp
            for f in (control, control.with_name(control.name + ".orig")):
                f.unlink(missing_ok=True)

    rows, passed = [], 0
    for name in tests:
        bad = [(arm, results[(name, arm)][0]) for arm, _, expect in arms
               if results[(name, arm)][0].status != expect]
        passed += not bad
        why = "; ".join(f"{arm} arm observed {v.status}: {v.detail}" for arm, v in bad)
        rows.append(f"PASS  {name}" if not bad else f"FAIL  {name} — {why}")
    ok_all = passed == len(tests)
    arms_txt = "patched + stock control" if len(arms) > 1 else "patched only"
    table = "\n".join([f"claude {binary_version(binary)}: {passed}/{len(tests)} patch tests pass "
                       f"({arms_txt}, {int(time.time() - t0)}s)", *sorted(rows, key=lambda r: r[:4] == "PASS")])
    print("\n" + table)

    if a.json:
        a.json.write_text(json.dumps({
            "binary": str(binary), "version": binary_version(binary), "ok": ok_all,
            "results": {f"{n}|{arm}": {**v.to_json(), "expected": e} for (n, arm), (v, e) in results.items()},
        }, indent=1), encoding="utf-8")
    if a.ntfy:
        req = urllib.request.Request(
            f"https://ntfy.sh/{a.ntfy}", data=table.encode(),
            headers={"Title": f"claude {binary_version(binary)} patch tests: {'all pass' if ok_all else 'FAILURES'}",
                     "Tags": "white_check_mark" if ok_all else "warning"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except OSError as e:
            print(f"ntfy post failed: {e}", file=sys.stderr)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
