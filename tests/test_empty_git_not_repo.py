#!/usr/bin/env python3
"""empty-git-not-repo: an empty `.git` directory under a trusted repository doesn't bring back the trust prompt.

Scenario: a git repository whose root is trusted, and a subfolder holding an
empty `.git` directory (what a tmpfs-masking sandbox leaves behind). An
interactive session starts in that subfolder.
  patched: no folder-trust prompt (the walk passes the empty `.git` and reaches the trusted root)
  stock:   the prompt appears (the subfolder counts as its own, untrusted repository)
Linux/macOS only (tmux).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import INCONCLUSIVE, PATCHED, STOCK, Sandbox, Verdict, main

NEEDS_TMUX = True


def run(binary: Path) -> Verdict:
    with Sandbox(binary, "empty-git-not-repo") as sb:
        repo = sb.root / "repo"
        sub = repo / "sub"
        (sub / ".git").mkdir(parents=True)
        git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README").write_text("x\n", encoding="utf-8")
        subprocess.run([*git, "add", "README"], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        sb.trust(repo)
        try:
            saw_trust = sb.start_interactive(["--model", "haiku"], cwd=sub)
        except RuntimeError as e:
            return Verdict(INCONCLUSIVE, f"session never reached the prompt: {str(e)[:200]}")
        keys = json.loads((sb.config / ".claude.json").read_text(encoding="utf-8")).get("projects", {})
        sub_trusted = any(Path(k) == sub and v.get("hasTrustDialogAccepted") for k, v in keys.items())
        if saw_trust:
            return Verdict(STOCK, f"trust prompt shown in {sub.name}/ (subfolder key trusted after accept: {sub_trusted})")
        return Verdict(PATCHED, "no trust prompt; the trusted repo root covered the subfolder")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
