# prompt-queue

`/queue <prompt>` holds a prompt until Claude is idle, then sends it as a turn of its own. `/hqueue <prompt>` also waits until no background job is running.

A prompt typed with plain Enter while Claude works is not held. Claude Code folds it into the running turn as soon as the current tool calls finish. `ctrl+x enter` (`chat:queueSubmit`) marks the prompt as waiting, but as of 2.1.293 it is folded in the same way ([anthropics/claude-code#99416](https://github.com/anthropics/claude-code/issues/99416)). Use `/queue` when the prompt should wait for Claude to finish.

## Use

```
/queue write up what you found
/hqueue summarize the eval results
/queue /compact keep the plan
/queue
```

- Both commands run immediately, even mid-turn, and add the prompt to a band above the input.
- The band lists each held prompt with `send now`, `edit` and `×` buttons. `ctrl+x tab` focuses it. The first row also answers `s`, `e` and `x`.
- `edit` takes the prompt out of the queue and puts `/queue <prompt>` back in the input.
- One prompt is released per idle point. The next one waits until the turn started by the previous one ends.
- A queued text that starts with `/` runs as that slash command.
- `/queue` with no argument prints the queue.
- If you interrupt a turn with Esc, the queue pauses. It resumes when the next turn ends normally, or when you press `resume` in the band.

## What counts as a background job

`/hqueue` reads the `background_tasks` list that Claude Code reports at the end of each turn. When a task finishes, its notification starts a turn, and the list is read again.

Two kinds of entries are not counted:

- **Teammates.** The list reports every teammate as running for as long as it exists, idle or not. The mod reads teammate status from the agent roster instead, and polls it every 5 s while an `/hqueue` prompt waits. A busy teammate blocks the prompt; an idle one does not.
- **Watchers.** A Monitor appears in the list as an ordinary shell task, and a persistent one never ends. Shell tasks whose command starts with `bgwatch` or `tail -f` are not counted. Edit `IGNORED_COMMAND` in `hooks/register.tsx` to change this. Other persistent Monitors still block `/hqueue`; use `send now` for those.

Scheduled wakeups (`/loop`, `ScheduleWakeup`, crons) are not counted.

## Limits

- A queued prompt is sent through the plugin API. `@file` mentions and pasted images are not expanded.
- The transcript names the plugin above each released prompt. The model reads the prompt as your own words.
- Claude Code records the `/queue` command line as a row the model reads at the start of the next turn. The mod replaces the queued text in that row with `(held by prompt-queue until its release)`. Without this, the model would see an `/hqueue` prompt before its release. The transcript therefore shows the command without its text; the text appears when the prompt is sent.
- The queue lives in the session's plugin state. It survives a plugin reload, not a restart.

## Install

`mods/` of this repository is a folder marketplace named `claude-code-patches`:

```sh
claude plugin marketplace add /path/to/claude-code-patches/mods
claude plugin install prompt-queue@claude-code-patches
```

Or load it for one session with `claude --plugin-dir /path/to/claude-code-patches/mods/prompt-queue`.

Requires function hooks (`CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` as of 2.1.293).
