#!/usr/bin/env python3
"""sendmessage-undefer: SendMessage ships loaded, not deferred behind ToolSearch.

One trivial `claude -p` turn, then the dumped request body is read.
  patched: SendMessage and its full input schema are in the request's `tools`
  stock:   SendMessage is absent there (the model must ToolSearch for it first)
Inconclusive when tool search is off for the session (a canary tool that is
always deferred shows up loaded). Cross-platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import Verdict, main
from _scenarios import undefer_verdict


def run(binary: Path) -> Verdict:
    return undefer_verdict(binary, "SendMessage")


if __name__ == "__main__":
    sys.exit(main(run, __doc__))
