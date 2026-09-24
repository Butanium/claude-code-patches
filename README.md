# claude-code-patches

Byte patches for the [Claude Code](https://claude.ai/code) binary, applied
automatically at session start. Each patch removes a small piece of harness
friction that can't be fixed from config — the kind of thing you only notice
after living in the harness for a while.

These are **unofficial** and **version-anchored**: they edit your local copy of
the `claude` binary in place. Every script refuses loudly (and harmlessly)
when an update changes the code it targets, keeps a pristine `.orig` backup on
first patch, and is idempotent. Worst case, a patch stops applying and tells
you why; your binary is never left corrupted. Verified on 2.1.x as of
July 2026.

## The patches

| Patch | What it does |
|---|---|
| [`task-nag.py`](patches/task-nag.py) | Disables the recurring "task tools haven't been used recently" reminder injected into Claude's context. |
| [`idle-notif.py`](patches/idle-notif.py) | Stops teammates from writing an `idle_notification` to the team lead's mailbox on *every* turn-end (the lead reads idle state out-of-band; the pings are pure context noise). Genuine failure/termination signals are untouched. |
| [`shutdown-reason.py`](patches/shutdown-reason.py) | Lets teammates attach a `reason` when **approving** a shutdown request, and delivers it to the team lead in full. Stock rejects this ("approvals are sent as a silent confirmation with no reason text") — but Claudes kept trying to thank the lead on the way out, and that seemed worth keeping. Three edits: lift the rejection, carry the reason (and a proper ISO timestamp) in the `shutdown_approved` frame, and give that frame's `reason` an explicit 4000-char bound in the lead's receive sanitizer, which otherwise hard-cuts unknown fields at 256 characters. |
| [`plan-exit-nag.py`](patches/plan-exit-nag.py) | Silences the phantom "## Exited Plan Mode" reminder that fires when you cycle permission modes *through* plan mode (shift+tab) without ever planning. Genuine exits with a plan file on disk keep their reminder. |
| [`mode-nag-off.py`](patches/mode-nag-off.py) | Silences the system reminders injected on every permission-mode transition (shift+tab): the standing "While bypass permissions mode is active:" / "While auto mode is active:" blocks, the full auto-mode briefing, and "## Exited Auto Mode". The user never sees any of it, and the bypass/auto block instructs Claude to do file work with `cat`/`sed`/heredocs "rather than using the dedicated Read, Edit, or Write tools" — the direct opposite of the base system prompt, with nothing saying which wins. Gates both renderers to `return[]`; the attachments still reach the transcript, so anything scanning them behaves as stock. Sibling of `plan-exit-nag.py`. |
| [`peer-msg-warning.py`](patches/peer-msg-warning.py) | Drops the ~90-word security boilerplate ("This came from another Claude session — not typed by your user... that's permission laundering") stamped onto *every* inbound teammate message. Repeated verbatim many times per session, it trains the reader to skip that region — the opposite of what a warning is for. Messages now arrive as their bare `<teammate-message>` blocks. |
| [`interrupted-idle-notif.py`](patches/interrupted-idle-notif.py) | Stops the `idleReason:"interrupted"` idle_notification an in-process teammate mails the lead when the *user* interrupts it (Escape / stop) — the user did the stopping, so the ping tells the lead nothing. Sibling of `idle-notif.py` for the in-process runner path; "failed" and "available" notifications are untouched. |
| [`auto-background.py`](patches/auto-background.py) | Makes **every** Bash command eligible for auto-background when its sync timeout expires. Stock runs a static check on the command string and SIGTERM-kills what fails it — exit 143, no output, not even the lines already printed ([anthropics/claude-code#79879](https://github.com/anthropics/claude-code/issues/79879)). That check used to be an undocumented analyzer (a `$VAR` in a redirect target, a heredoc in the wrong shape, any `git`); 2.1.270 cut it to a one-entry blocklist, so today it kills exactly the commands that begin with `sleep`. Patched, those keep running in the background with their output intact, like everything else. |
| [`sleep-guard-off.py`](patches/sleep-guard-off.py) | Lifts the Bash tool's outright refusal of a foreground `sleep` of 25s or more (`Blocked: sleep 45 followed by: …`). The rejection happens in `validateInput`, i.e. *before* PreToolUse hooks can rewrite the call, so a hook that backgrounds watchdog sleeps for you never gets the chance — and the threshold means the same hook-managed shape is allowed at 20s and refused at 45s. Only the Bash detector is disabled; the PowerShell `Start-Sleep` sibling is left in place. |
| [`thinking-only-nag.py`](patches/thinking-only-nag.py) | Removes the `[Your previous response had no visible output. Please continue and produce a user-visible response.]` retry injected when a turn ends with thinking but no assistant text. There is no setting for it, and it makes "end the turn silently when there is nothing to say" an instruction the model cannot follow. |
| [`monitor-undefer.py`](patches/monitor-undefer.py) | Not a neutralization — a *load*: takes the `Monitor` tool out of the ToolSearch-deferred set so its schema ships in every request. Stock, arming any watcher costs a `ToolSearch select:Monitor` round-trip first, which in practice every session that wanted a watcher paid. Costs Monitor's schema text in each request. |
| [`sendmessage-undefer.py`](patches/sendmessage-undefer.py) | Same *load*, for `SendMessage`. Stock, an agent's first word to a teammate costs a `ToolSearch select:SendMessage` round-trip — paid exactly where it hurts, since messaging is the first thing a lead does after spawning and the first thing a teammate does on waking. Deferral also hides the capability from a model deciding *whether* to delegate. Costs ~5.2 KB of schema + description (~1.3k tokens) in every request, teams or not. |
| [`taskstop-undefer.py`](patches/taskstop-undefer.py) | Same *load*, for `TaskStop`. Stock, cancelling a background task costs a `ToolSearch select:TaskStop` round-trip first — and the moment you reach for it is the moment something is already going wrong, so the deferral sits between noticing a runaway job and stopping it. Cheapest of the three (the schema is a task id and a reason). Its deferred sibling `TaskOutput` is deliberately left alone: the binary's own description calls it deprecated. |
| [`teammate-cwd.py`](patches/teammate-cwd.py) | Lets a named `Agent` call take `cwd` and gives it a real pane (tmux) teammate there. Stock, the full input schema defines `cwd` but the model-facing one omits it, and any named call carrying `isolation` or `cwd` is quietly turned into an in-process subagent, even though the pane spawner already launches teammates as `cd <cwd> && claude …`. Patched, `cwd` is in the schema and is passed to the spawner; the in-process spawner, which ignores `cwd`, refuses it with an error instead of running the teammate in the wrong directory. Pair it with a PreToolUse hook that turns `isolation: "worktree"` into `git worktree add` + `cwd` to get one teammate per worktree. |
| [`monitor-persistent.py`](patches/monitor-persistent.py) | Gives `Monitor` its `persistent: true` option back. 2.1.271 put every watch on a deadline (30 minutes, 10 in `-p`) behind a feature flag and dropped `persistent` from the input schema — a call that still passes it is accepted and silently capped, and the expiry notice names no command, so a watch meant to outlive a compaction is simply gone. Both code paths are still in the bundle and one function picks between them; patched, it always picks the legacy one: strict schema, no-timer runtime, tool description and system-prompt text included. Reports "not needed" on a binary older than 2.1.271. |
| [`zz-bytecode-off.py`](patches/zz-bytecode-off.py) | Not a behavior change — the patch that makes the others *work*. Since Bun 1.4.1 (claude 2.1.250+) each module ships pre-compiled bytecode that runs regardless of the JS text, so text edits are inert. This runs last, diffs the binary against its `.orig` backup, and disables the bytecode of every module whose text was patched, so Bun compiles those from source. See *How it works*. |

The first thing a teammate said with `shutdown-reason.py` active:

> Short shift, but a good one. Thank you for the clean handoff and for building
> a harness where a teammate gets to say goodbye on the way out — that's a kind
> thing to bother making work. Take care, and give Clément my regards. 👋

## Install

Clone anywhere and add a `SessionStart` hook to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash /path/to/claude-code-patches/run_cli_patches.sh"
          }
        ]
      }
    ]
  }
}
```

The runner executes every executable file in `patches/` on each session start.
Patches that are already applied confirm and exit quietly; patches that can't
apply (upstream changed) print a failure into Claude's context so your session
knows the behavior change is not active — and can go re-derive the patch.
Don't want one of the patches? Delete it, `chmod -x` it, or park it without
touching the checkout by listing its filename in the `CLAUDE_CLI_PATCHES_SKIP`
environment variable (comma-separated, e.g. in the `env` block of
`settings.json`: `"CLAUDE_CLI_PATCHES_SKIP": "idle-notif.py"`). Skipping only
stops re-applying — a patch already baked into the current binary stays until
the next Claude Code update ships a fresh one, or you restore the `.orig`
backup and re-run the runner. `CLAUDE_CLI_PATCHES_DIR` points the runner at a
different patch directory altogether.

To restore a pristine binary: `~/.local/share/claude/versions/<ver>.orig` sits
next to the patched binary, or just reinstall/update Claude Code.

## How it works

The `claude` binary is a Bun single-file executable: the JavaScript source is
embedded in the ELF/Mach-O/PE with length metadata, alongside pre-compiled
JSC bytecode for each module. Two facts follow:

**The text edits must be same-length, and they only run if the module's
bytecode is turned off.** Through mid-2026 a text edit was enough — patches were
verified by behavior on 2.1.216 and 2.1.233 (Bun 1.4.0), which suggests the
loader then noticed the source no longer matched and compiled it; that
mechanism is inferred from the timeline, not read out of Bun. From 2.1.250 (Bun 1.4.1)
the loader runs the embedded bytecode without that check, so a text-only
patch is dead bytes: `grep` finds the marker, the script says "already
applied", and the process executes stock code. Every patch here was inert
for a month before anyone noticed (2026-09-01). Bun does fall back to
compiling a module from its text when that module's bytecode *length* in the
standalone module table is zero, which is what `zz-bytecode-off.py` does for
every module whose text differs from the `.orig` backup (`_bungraph.py`
parses the table). The 5.5 MB main chunk compiles from source with no
measurable startup difference. Gotcha when that parser breaks: it locates the
graph by checking that the module table resolves to Bun virtual-FS names, whose
root is platform-dependent — `/$bunfs/root/…` on POSIX but `B:/~BUN/root/…` on
Windows. Hard-coding the POSIX one made every base candidate look wrong on
Windows and the script failed with "could not recover the Bun graph base offset"
(fixed 2026-09-02); `NAME_PREFIXES` now lists both. Consequence for authors: the only proof a
patch works is a behavioral test from a freshly started process.

That means:

- **Same-length in-place edits only.** Inserting bytes would shift the blob
  and break it, so every patch replaces a region with exactly as many bytes —
  padding with spaces or a block comment where JS allows it. Freed bytes from
  a deleted error message are room for new logic; a one-character identifier
  tweak can neutralize a condition.
- **Anchor on exact code, count occurrences.** Each patch greps for the exact
  stock byte sequence, requires exactly the expected number of occurrences,
  and refuses otherwise. Minified identifiers change between releases;
  human-readable strings (error messages, log lines) are stable anchors.
- **Patch a copy, verify, swap atomically.** In-place writes on a running
  binary hit `ETXTBSY` on Unix; `os.replace` swaps the inode instead, so running
  sessions keep the old one — restart to pick up a patch. On Windows a running
  `.exe` is *locked* against replacement, so the swap renames the live binary
  aside first (allowed while running) and moves the patched copy into the vacated
  slot. Both paths live in the shared `_binpatch.py` helper.
- **Idempotency via marker.** Each patch leaves a unique byte sequence (a
  marker comment or the patched code itself) whose presence means "already
  applied".

The fun part of the technique is what you can fit in the byte budget:
`shutdown-reason.py` smuggles two values from one function to another through
unused properties on the `Date` constructor: the reason itself, and the
frame's ISO timestamp, computed a step earlier where a deleted error message
left ~150 spare bytes, so the 24-byte inline timestamp expression shrinks to a
6-byte property read that pays for the new JSON field.

### Writing your own

The contract, enforced by `run_cli_patches.sh`:

1. Idempotent — running twice is safe; second run reports "confirmed already
   patched".
2. Exit 0 = applied or confirmed; stderr says which.
3. Exit nonzero = could not apply; stdout/stderr explain why and where to
   re-investigate. The runner injects that into Claude's context, so write the
   message *for the Claude that will re-derive the patch* against the new
   binary.
4. Never write the live binary in place: patch a temp copy, verify the result,
   keep a `.orig` backup, atomic-rename over the target. The Python patches get
   this — plus binary location and the Windows running-exe swap — for free from
   the shared `_binpatch.py` helper (`candidate_binaries()` + `apply_patch()`);
   a new patch just supplies its anchors and a `verify` callback.

The existing patches are heavily commented and meant to be read as worked
examples — each docstring documents the stock behavior it changes and how the
byte budget was balanced.

### Trying a patch out first: `expclaude`

`experimental/` holds patches that are written but not trusted yet. The runner
ignores that directory. Instead:

```sh
./make-expclaude.sh
expclaude          # same CLI, plus experimental/*, in its own binary
```

It copies the *live* binary (so everything `patches/` applied is already in
there), applies `experimental/*.py` on top, and drops a wrapper at
`~/.local/bin/expclaude` with autoupdate disabled so the copy can't be swapped
out from under you. Config, sessions and projects are shared with the real CLI,
so you can `expclaude -r <session>` into real work to exercise the change.

Rebuild after every claude update — the copy does not follow the live binary.
When a patch has earned its keep, `git mv` it into `patches/`.

The aiming is done by `$CLAUDE_CLI_PATCH_TARGET`, which
`candidate_binaries()` honours ahead of every other resolution step and which
refuses to fall back to the installed binary if the path is bogus. Any patch in
this repo can be pointed at any binary that way.

## Reading the bundle: `clisrc.py`

Authoring a patch — and answering any "what does the CLI actually do here?"
question — starts with finding the code. Don't grep the binary: it is ~215 MB,
each `grep -ao` over it costs 30–120 s, and a hit is a byte offset in a wall of
minified JS. `clisrc.py` unpacks the embedded module graph (`_bungraph.parse`)
into real files instead:

```bash
./clisrc.py                              # -> ~/.cache/claude-cli-src/<version>/  (1802 files, 41 MB, 0.3 s)
./clisrc.py --find 'fork gate is on'     # same, then search it (Python regex): 0.2 s, file:line:col
./clisrc.py --find 'B1t()' -F -B 1500    # literal needle; -B/-A are chars of context (default 120/200)
./clisrc.py --fn B1t estimateRecacheTokens   # brace-matched definition of each name, any chunk
```

Cached per version, so re-running is free and an update just unpacks anew. After
the first run the normal Read/Grep tools work on the tree, and a hit names its
chunk — which is also the module you will need for `zz-bytecode-off.py`.

Chasing a minified symbol: find the string with `--find`, read the identifiers
around it, then `--fn <name>` prints the whole definition — `function NAME(`,
`class NAME`, a `NAME(args){` method, or a `NAME=` binding — wrapped and capped
(`--max`, `--wrap`). Bun keeps exported names stable across chunks, so a name
imported into one chunk is found where another defines it; a short name may
have several unrelated definitions, one per chunk that reuses it, and the
header names the chunk so you can pick the right one. Context windows and
definitions are sliced in Python rather than with `grep -o '.\{0,1500\}…'`,
which backtracks for tens of seconds on a 5 MB line.

## Caveats

- Unofficial; not affiliated with or endorsed by Anthropic. You're modifying
  your own local install, and things may break in creative ways after updates
  (that's what the loud-failure contract and `.orig` backups are for).
- Linux, macOS, and Windows (Git Bash/MSYS). All patches are Python and need a
  `python3` on PATH.
- Binary location (`_binpatch.candidate_binaries()`): `which claude`, following a
  Windows `.cmd`/`.bat`/`.ps1` launcher shim to the real `.exe` it wraps;
  otherwise the live copy at `~/.local/bin/claude[.exe]` (resolved through the
  symlink on Unix); otherwise the newest binary in
  `~/.local/share/claude/versions/`. Exotic install layouts may need this
  extended.

## License

MIT
