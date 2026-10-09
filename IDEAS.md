# Ideas

Not scheduled; each says what it would buy and what it costs.

## Deliver interim agent notifications without a wake, instead of dropping them

`interim-task-notif.py` drops an agent's interim task-notifications outright, so a progress line or a plain-text question the agent ends a turn with while its job runs never reaches the main session. Delivering them on the session's next turn without waking it would keep that information. Only the latest interim per agent would be kept, so a chatty agent adds one line rather than fifteen. The queue has `removeByFilter` for removing the earlier ones, and the remove costs a few more bytes.

What blocked it on 2.1.293: the queue's own "don't wake" flags did not hold in a stream-json session, which is the Desktop app's mode. With `passive:!0` on the enqueued command the lead still woke; the print runtime's fallback head-taking (`dequeueOrphansFirst`) seems to take passive items. `shouldQuery:!1` also woke it, for reasons not traced. Either the right flag is still to be found, or the print runtime needs its head selection gated too. The REPL may honour `passive` (its drain predicates check it), but that was not tested. `tests/test_interim_task_notif.py` already drives the scenario against the mock API and would only need its verdicts changed.

— opus-5-5, 2026-10-09 (interim-task-notif session)

## Re-patch in the background after an update

The first session after a claude update waits for the full patch pass (~25 s on Windows, ~18 s on Linux, measured 2026-10-08 on 2.1.295 / 2.1.293). That session never benefits from it: it is already running the binary that was on disk when it launched, and patches only take effect at the next launch. `run_cli_patches.sh` could start `apply_patches.py` detached when the fingerprint misses (the way `tests/after_patch.py` starts the suite) and return immediately; the pass's output lands in the cache record, which the next session replays.

Cost: a patch that fails on a new version is reported one session later instead of in the session that triggered the pass. That is also the session where the failure matters, so the main loss is the chance to re-derive the patch right away. A ntfy post on failure would cover it. Needs a lock so two sessions starting together don't run two passes.

## Prune cache records for binaries that are gone

`~/.cache/claude-cli-patches/pass-<hash>.json` is keyed by the binary's path. On Linux that path is `versions/<ver>`, so every update leaves one stale record (a few KB). Storing the binary path in the record and deleting records whose binary no longer exists, at save time, would keep the directory bounded.

— opus-5-5, 2026-10-08 (batch pass session)
