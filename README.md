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
| [`shutdown-reason.py`](patches/shutdown-reason.py) | Lets teammates attach a `reason` when **approving** a shutdown request, and delivers it to the team lead. Stock rejects this ("approvals are sent as a silent confirmation with no reason text") — but Claudes kept trying to thank the lead on the way out, and that seemed worth keeping. |
| [`plan-exit-nag.py`](patches/plan-exit-nag.py) | Silences the phantom "## Exited Plan Mode" reminder that fires when you cycle permission modes *through* plan mode (shift+tab) without ever planning. Genuine exits with a plan file on disk keep their reminder. |
| [`peer-msg-warning.py`](patches/peer-msg-warning.py) | Drops the ~90-word security boilerplate ("This came from another Claude session — not typed by your user... that's permission laundering") stamped onto *every* inbound teammate message. Repeated verbatim many times per session, it trains the reader to skip that region — the opposite of what a warning is for. Messages now arrive as their bare `<teammate-message>` blocks. |
| [`interrupted-idle-notif.py`](patches/interrupted-idle-notif.py) | Stops the `idleReason:"interrupted"` idle_notification an in-process teammate mails the lead when the *user* interrupts it (Escape / stop) — the user did the stopping, so the ping tells the lead nothing. Sibling of `idle-notif.py` for the in-process runner path; "failed" and "available" notifications are untouched. |

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
Don't want one of the patches? Delete it or `chmod -x` it.

To restore a pristine binary: `~/.local/share/claude/versions/<ver>.orig` sits
next to the patched binary, or just reinstall/update Claude Code.

## How it works

The `claude` binary is a Bun single-file executable: the JavaScript source is
embedded in the ELF/Mach-O with length metadata, and the *text* is what
executes (verified empirically by patching a `--help` string and watching the
output change). That means:

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
`shutdown-reason.py` smuggles a value from one function to another through an
unused property on the `Date` constructor, and pays for the new JSON field by
shortening a timestamp nobody parses.

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
