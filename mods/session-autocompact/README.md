# session-autocompact (Claude Mod)

Auto-compact for one session while the global `autoCompactEnabled` stays `false`.
`/config` and `/autocompact` both write `~/.claude/settings.json`, so they change every
session; `claude --settings '{"autoCompactEnabled":true}'` is per-process but only at launch.

```
/session-autocompact              status (off by default)
/session-autocompact on           compact when a turn ends with context ≥ 80%
/session-autocompact 70 | 70%     same, at 70% of the window
/session-autocompact 400k | 1m    same, at a token count
/session-autocompact off
```

When on, a `turn.complete` hook on the main loop (not subagents, not interrupted turns)
reads `$.session.usage().context` and calls `$.session.compact()`, the same compaction
`/compact` runs, 200 ms after the turn ends. A failed compaction is not retried until the
next turn ends: a compaction of a near-full window is expensive. Turning it on while already
past the threshold compacts right away. The status line shows `session-autocompact: on (≥80%)`.

The setting lives in `$.store` under `on:<session id>`, so it survives `claude -r`;
`/clear` and `--fork-session` start a new id, which starts off.

Limit: a mod cannot compact mid-turn (`$.session.compact` rejects while a turn runs, and
`turn.step` can't replace the conversation). A single turn that grows from below the
threshold past the whole window still hits the context limit; the mod then compacts when
that turn ends, and "continue" picks up.

Tested 2026-10-07 on 2.1.289 in an interactive Haiku session (`--plugin-dir`): status,
on at 5% → compacted 200 ms after the first turn, setting kept across `claude -r`, off →
no compaction. `claude plugin test .` runs `hooks/register.test.ts` (6 pass on 2.1.293;
on 2.1.289 the command refused to run, "the rollout switch was saved off by an earlier
session", although mods loaded in interactive sessions).

Install from this repo's `mods/` marketplace (see `../README.md`):
`claude plugin install session-autocompact@claude-code-patches`. Installs are
version-keyed copies: bump `version` in `.claude-plugin/plugin.json` and
`claude plugin update` after editing.
