# Ideas

Not scheduled; each says what it would buy and what it costs.

## Re-patch in the background after an update

The first session after a claude update waits for the full patch pass (~25 s on Windows, ~18 s on Linux, measured 2026-10-08 on 2.1.295 / 2.1.293). That session never benefits from it: it is already running the binary that was on disk when it launched, and patches only take effect at the next launch. `run_cli_patches.sh` could start `apply_patches.py` detached when the fingerprint misses (the way `tests/after_patch.py` starts the suite) and return immediately; the pass's output lands in the cache record, which the next session replays.

Cost: a patch that fails on a new version is reported one session later instead of in the session that triggered the pass. That is also the session where the failure matters, so the main loss is the chance to re-derive the patch right away. A ntfy post on failure would cover it. Needs a lock so two sessions starting together don't run two passes.

## Prune cache records for binaries that are gone

`~/.cache/claude-cli-patches/pass-<hash>.json` is keyed by the binary's path. On Linux that path is `versions/<ver>`, so every update leaves one stale record (a few KB). Storing the binary path in the record and deleting records whose binary no longer exists, at save time, would keep the directory bounded.

— opus-5-5, 2026-10-08 (batch pass session)
