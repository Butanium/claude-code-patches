# Behavior tests

A patch script saying "confirmed already patched" only means its anchor
matched. Since claude 2.1.250 that proves nothing on its own: a text edit is
dead bytes unless `zz-bytecode-off.py` turned that module's bytecode off (see
*How it works* in the top README). These tests start a fresh Claude Code process
from an explicit binary path and check that the change is actually observable,
one test per patch.

```sh
python3 tests/run_tests.py --binary ~/.local/share/claude/versions/<ver> --control
python3 tests/run_tests.py --binary <path> --only shutdown-reason,idle-notif
python3 tests/test_task_nag.py --binary <path> --control      # a single test
```

`--control` adds a second arm: an executable copy of `<binary>.orig`, where
every test must observe the **stock** behavior. A test that also passes on
stock proves nothing, so run with `--control` whenever a test is new or changed.
The whole suite takes about two minutes with both arms, using the account the
CLI is logged in with.

## What a test can observe

Every session runs in a throwaway sandbox (`_harness.Sandbox`): its own
`CLAUDE_CONFIG_DIR` with a copy of your credentials, no hooks, no MCP servers,
`DISABLE_AUTOUPDATER=1`, and a scrubbed environment (every `CLAUDE*` / `OTEL_*`
variable and every key of your `settings.json` `env` block is dropped, so a run
started from inside a Claude session doesn't inherit that session's settings).

- **API request/response bodies.** The sandbox sets
  `OTEL_LOG_RAW_API_BODIES=file:<dir>`. The CLI then writes every request and
  response body as JSON; no telemetry exporter is needed. This is the ground
  truth for what the model sees: tool list and schemas (deferred tools are
  absent from `tools`), system reminders, and teammate messages as rendered to
  the lead. Response bodies also hold tool calls that a transcript can miss.
  Example: a teammate's approval of a shutdown request is its last act before
  it exits, so its transcript doesn't record it.
- **Transcripts**: tool calls, tool results, attachments, teammates included.
- **tmux panes** of a private server (`tmux -L cli-patch-test-…`), for the
  interactive tests: teams, shift+tab mode cycling, Escape.
- **A mock Messages API** (`_harness.MockMessagesAPI`) for behavior a real model
  won't produce on demand. For example, `thinking-only-nag` needs a turn that
  ends with a thinking block and no text. These runs spend no tokens.

Shared sessions (`_scenarios.py`) run once per binary per `run_tests.py`
invocation: idle-notif, shutdown-reason and peer-msg-warning all read one lead
plus one pane teammate session, and the three undefer tests plus teammate-cwd
read one trivial `-p` turn.

## Tests

| Test | How | Platforms |
|---|---|---|
| monitor-undefer, sendmessage-undefer, taskstop-undefer | the tool is in the first request's `tools` (a tool that stays deferred on every binary guards against tool search being off) | all |
| teammate-cwd | Agent's schema offers `cwd` (schema half only; the pane-routing half is not exercised) | all |
| monitor-persistent | Monitor with `persistent: true` arms a watch with no deadline | all |
| sleep-guard-off | `sleep 26; echo …` runs instead of "Blocked: sleep 26" | all with Bash |
| auto-background | `sleep 8; …` with a 3 s timeout is backgrounded, not killed with exit 143 | all with Bash |
| task-nag | 14 Bash turns on haiku with `CLAUDE_CODE_TODO_REMINDER_MODE=baseline` produce no task_reminder | all |
| thinking-only-nag | mock API serves a thinking-only turn; no retry request carries the nag | all |
| zz-bytecode-off | structural: every module that differs from `.orig` has its bytecode length zeroed | all |
| idle-notif, peer-msg-warning, shutdown-reason | one lead + one pane teammate | Linux/macOS (tmux) |
| plan-exit-nag, mode-nag-off | shift+tab from default through plan into bypass mode, then one prompt | Linux/macOS (tmux) |
| interrupted-idle-notif | in-process teammate, interrupted with ↓ ↓ Enter Esc Esc | Linux/macOS (tmux) |

The tmux tests are not ported to Windows; `run_tests.py` reports them
inconclusive where tmux is missing. Credentials are copied from
`<config>/.credentials.json`. On macOS, where the login usually lives in the
keychain, set `ANTHROPIC_API_KEY` instead.

## Running automatically after patching

With `CLAUDE_CLI_PATCH_TESTS=1` in the patch runner's environment,
`run_cli_patches.sh` calls `tests/after_patch.py` after the patches. A patch
applying or a claude update replaces the live binary, and each new state of it
gets one background run with `--control`. The results go to
`${XDG_CACHE_HOME:-~/.cache}/claude-cli-patch-tests/`, and the table is posted
to `ntfy.sh/$CLI_PATCH_TESTS_NTFY_TOPIC` when that variable is set. Example
hook command:

```sh
CLAUDE_CLI_PATCH_TESTS=1 CLI_PATCH_TESTS_NTFY_TOPIC="$MY_TOPIC" bash /path/to/run_cli_patches.sh
```

Other knobs: `CLI_PATCH_TESTS_TMPDIR` (where sandboxes and the ~250 MB control
copy go; defaults to the system temp dir, which may be RAM-backed) and
`CLI_PATCH_TESTS_KEEP=1` (keep sandboxes for inspection).

## Writing a test

`tests/test_<patch_name_with_underscores>.py` with `run(binary) -> Verdict`
returning `PATCHED`, `STOCK` or `INCONCLUSIVE` (the scenario didn't produce
evidence either way), plus `NEEDS_TMUX = True` if it drives a TUI. End with
`sys.exit(main(run, __doc__))` so it also runs on its own. Private patch repos
put theirs in a `tests/` next to their `patches/`, and `run_tests.py` finds
them through `CLAUDE_CLI_PATCHES_EXTRA_DIRS`.

Gotchas met while building these:

- **Idle detection**: in a detached tmux server the pane title can keep its idle
  glyph `✳` through a whole turn. `pane_state` treats the footer's "esc to
  interrupt" as the working signal instead.
- **Dialogs and ghost text** eat keystrokes. The sandbox sets `tui: "default"`
  (an explicit value skips the "Try the new fullscreen renderer?" dialog) and
  disables prompt suggestions. `send()` checks that the turn actually started
  and retries if it didn't.
- **`tmux display-message -t %N`** on a closed pane exits 0 with empty output.
  Check the pane list instead.
- **Gated producers.** The bypass-mode block that mode-nag-off silences is
  only produced when the "bash-first" gate is on. The test forces it with
  `CLAUDE_CODE_THRIFTY_SONIC=1`; without that, stock produces nothing to
  suppress.
- **TaskStop is not an interrupt.** It kills an in-process teammate, and
  neither binary sends an idle ping then. The "interrupted" ping comes from
  the user pressing Escape in the teammate's view.
