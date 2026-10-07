# Mods

Claude Code plugins of function hooks (Claude Mods). Each changes the CLI's
behavior through documented hook events instead of editing the binary:

| plugin | what it does |
|---|---|
| `cli-patch-mods/` | reproduces 9 of the byte patches (below) |
| `own-bash-edit-notice/` | cuts the "changed on disk" notice to one line when the session's own Bash command made the change ([README](own-bash-edit-notice/README.md)) |
| `session-autocompact/` | `/session-autocompact on`: auto-compact one session while the global setting stays off ([README](session-autocompact/README.md)) |

## Loading them

`mods/` is a folder marketplace named `claude-code-patches`:

```sh
claude plugin marketplace add /path/to/claude-code-patches/mods
claude plugin install session-autocompact@claude-code-patches
```

An installed plugin reaches every session, pane teammates included. Other ways
to load one from the checkout:

- `CLAUDE_CODE_PLUGIN_DIRS=/path/to/claude-code-patches/mods/<plugin>` in
  the `env` block of `~/.claude/settings.json`. Every session gets it, and so do
  pane teammates, which inherit the environment.
- `claude --plugin-dir /path/to/.../mods/<plugin>` for one session.

## cli-patch-mods

A plugin that reproduces 9 of the byte patches. It is an **alternative**, not a
replacement: the byte patches remain the primary mechanism, and where both are
loaded they agree. Each hook is a no-op on a binary that is already patched.

Use it when you can't or won't patch the binary: an install whose binary
you don't own, a machine where you want to keep stock bytes, or the window
after an update in which a patch no longer applies.

Function hooks are switched off when the GrowthBook flag
`tengu_plugin_hooks_modules` is off (it defaults to on, offline included), in
bare and safe mode, with `disableAllHooks`, and in "diskless" cloud sessions.
A byte patch applies in all of those.

### What it covers, and how it differs from the patch

| patch | hook | difference from the byte patch |
|---|---|---|
| `monitor-undefer`, `sendmessage-undefer`, `taskstop-undefer` | `tool.describe` → `isDeferred: false` | none observed |
| `mode-nag-off` | `prompt.attachment` `auto_mode`, `auto_mode_exit` → `{ text: null }` | none. The patch also gates the renderer and leaves the attachment in the transcript |
| `plan-exit-nag` | `prompt.attachment` `plan_mode_exit` with `detail.hasPlan === false` → `{ text: null }` | none, for the same reason |
| `task-nag` | `prompt.attachment` `todo_reminder`, `task_reminder` → `{ text: null }` | The reminder is still **produced and recorded in the transcript**. Only its text is kept out of requests. The patch never produces it. A session resumed without the mod sends it again |
| `peer-msg-warning` | `session.append`: strip the header line and boilerplate paragraph from a row that carries them | Matches the boilerplate text, which is as release-specific as a patch anchor. When it stops matching, the message arrives wrapped (stock), with no error. `session.receive` can't do this: the CLI wraps the message after that event |
| `idle-notif`, `interrupted-idle-notif` | `session.receive` → `{ consumed }` for a delivery that is only an `idle_notification` with reason `available` or `interrupted` | Acts on the lead's side: the teammate still writes the ping and the lead drops it on arrival. The patches stop the teammate from sending it |

Not covered: `thinking-only-nag` (the retry is a query-loop decision; no event
can cancel it), `auto-background` (Bash runtime internals), `shutdown-reason`,
`monitor-persistent` and `teammate-cwd` (each changes a tool's input schema or
runtime, which no event exposes).

`sleep-guard-off` is partly reachable. A `tool.call` hook runs before the Bash
tool's `validateInput`, so rewriting `sleep 26` to `/bin/sleep 26` gets past the
guard. The rewritten command then needs its own permission rule, and the trick
depends on the guard's detector, so it is left out.

### Tests

`covers.json` lists the patches this plugin reproduces. The behavior suite runs
those tests on the stock binary with the plugin loaded and expects the patched
behavior:

```sh
python3 tests/run_tests.py --binary ~/.local/share/claude/versions/<ver> --control --mod mods/cli-patch-mods
```

A mod that stops matching does nothing and reports nothing, unlike a patch,
which fails loudly at session start. So this suite run is the only check that
the mod still works after an update.

When a patch here is re-derived for a new release, check whether its hook still
holds: run the line above with `--only <patch>`.
