# own-bash-edit-notice (Claude Mod)

After a session changes a file it had Written/Edited through its own Bash command
(`python3 - <<EOF` rewrites, `sed -i`, `cat >> f`), the engine injects an
`edited_text_file` notice with a diff snippet of up to 8 KB. The session made the
change, so the snippet is mostly repeated context: in the author's 2026-08/09 session
archive, 413 of 738 such notices (56%) followed a Bash call of the same session naming
the file, ~600k tokens.

Swallowing a real outside change (another session editing the file in this shared tree)
is worse than paying the tokens, so the notice is cut to one line only when all hold:
- a Bash command of the same loop ran **since the previous model request** (`turn.step`
  counts requests; the notice is computed for the first request after a change);
- it names the file by absolute path, `~` path or the path as the notice shows it, or by its
  path relative to `$.session.cwd()` or to a directory the command `cd`s into
  (`cd ~/x && python3 - <<EOF … Path("sub/f.py")`);
- it plausibly **wrote** it: a redirect into that path, or a writing verb (`sed -i`, `tee`,
  `mv`/`cp`, `patch`, `truncate`, `git checkout/restore/apply/stash/reset`, a
  python/node/perl/ruby script) in the command naming it. `cat`, `jq .`, `git diff`,
  `grep` don't count.
Everything else passes through untouched. The transcript keeps the engine's record; only
the request changes. Residual risk: a writing command naming the file (e.g. `cp f /tmp/x`,
f being the source) in the same step as an outside edit of f.

Needs `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` (Claude Mods early access, 2.1.271+) and the
plugin installed from this repo's `mods/` marketplace (see `../README.md`): `claude plugin
install own-bash-edit-notice@claude-code-patches` (an installed plugin reaches tmux
teammates; `--plugin-dir` doesn't). Installs are version-keyed copies: bump `version` in
`.claude-plugin/plugin.json` and `claude plugin update` after editing the mod. API
constraints met along the way: no imports but relative files and `"claude-code"`;
`turn.step` needs an `async function*` that `return yield* next(e)`; `$` must be spelled
`$.noun.event(...)` at the call site (no `$.ui?.log`).

Probed 2026-09-25 on 2.1.280 (`probe.sh`: haiku, requests captured by
`probe_logproxy.py`, an outside editor process triggered by marker files):
f.txt `python3 -c "…f.txt…"` → one line; g.txt changed by `mod.sh` (not named) → full;
h.txt `cat h.txt` then an outside edit → full; k.txt `sed -i … k.txt` → one line;
n.txt `cp n.txt /tmp/…` two requests before an outside edit → full. Each decision saw
exactly one candidate command (the debug log's `own-bash-edit-notice:` lines), so
`turn.step` fires before the request's attachments resolve. `prompt.attachment` ~1.4 ms.

0.0.3 (2026-09-26): the `~` form never matched before, because a hooks module has no
`process` (HOME now comes from `$.env.get("HOME")`), so `cat >> ~/x/f <<EOF` kept the full
notice. Re-probed on 2.1.280 with a `~`-path heredoc append, with and without tmux-teammate
CLI flags (`--agent-id/--team-name`): shortened both times.

0.0.4 (2026-09-29): `replay.mts` runs `wrote()` over every archived notice. Over 176 notices
from 09-26 to 09-29, 0.0.3 shortened about 1 in 6. Most misses were `cd <dir> && python3 - <<EOF`
edits naming the file relative to that dir (or `~/dir` + basename, which the absolute-only
directory check missed). 0.0.4 resolves `cd` targets and the cwd instead, and shortens 122 of
the 176. Of the notices still kept, those with a same-request command naming the file are real
outside changes: Overleaf `git pull`s, background-task output files, the probe's outside
editor. The older basename+directory rule is gone: with the `~` form added it matched `c.py`
inside `xc.py`. `test_wrote.mts` holds the positive/negative naming cases; `probe.sh` gained
`sub/q.txt` (`cd` + relative `sed -i`), and passes on 2.1.283 with `--plugin-dir` and installed.

Also checked: the CLI's own "Bash edit diff" (`CLAUDE_CODE_BASH_EDIT_DIFF=1`,
`bashEditDiffEnabled`) records a `bashEditDiff` in the tool result but does not stop the
notice.
