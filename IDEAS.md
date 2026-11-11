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

## Auto-repair: a repair session waiting on an answer blocks every later repair

`auto_repair.py` runs one repair session at a time, and a session that asked a question stays open until someone answers or closes it. If the ntfy goes unanswered for days, later versions' failures pile up behind it; each session start prints a line about it, but nothing re-pings. A cheap fix: when the open session's last transcript write is older than a day or two, send one reminder through ntfy, or allow a new repair session for failures on a newer version. Also untested in practice as of 2026-11-10: the ask-and-wait path itself. Both real runs so far (2.1.308, 2.1.317) were routine repairs that closed silently.

— opus-5-5, 2026-11-10 (auto-repair session)

## Let subagents write report-named files (owner asked for it, 2026-09-28)

The Write tool refuses any subagent write whose basename matches `^(REPORT|SUMMARY|FINDINGS|ANALYSIS).*\.md$` ("Subagents should return findings as text…", errorCode 5, telemetry `tengu_subagent_md_report_blocked`), whatever the brief asked for. It has cost at least two subagents their output file (2026-09-26, 2026-10-08). Still present in 2.1.321: `clisrc.py --find 'Subagents should return findings' -F` lands in Write's input validation, gated on `s.agentId&&/^(REPORT|…)/i.test(basename)`. Two routes: a mod on `tool.check` for `Write` that returns `allow` when the only objection is this one (check first that the tool's own validation is part of the chain a `tool.check` answer overrides), or a same-length byte patch that makes the regex unmatchable (e.g. `\.md$` → `\.m~$`). Prefer the mod if it works. Behavior test: a subagent asked to write `REPORT.md` succeeds.

## Fold the claude.ai connectors' MCP instructions (owner asked for it, 2026-09-28)

Connector tools are already deferred (names only), but each connected claude.ai MCP server's instructions are injected in full into every session's system prompt, including servers the archive shows no session ever calling (Airtable, Rize, Slack and others). The owner wants each reduced to a one-line description plus "ToolSearch finds its tools". `prompt.section` fires once per named system-prompt section, so a mod can rewrite the MCP-instructions section: keep the servers in an allowlist verbatim, replace the others' blocks with `<name>: <first sentence>. Load its tools with ToolSearch.` Read the section id off `prompt.compose` first. Watch prompt-cache stability: the rewrite must be deterministic.

— opus-5-5, 2026-11-11 (archive-sweep refresh)
