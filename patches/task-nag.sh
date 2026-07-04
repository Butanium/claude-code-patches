#!/bin/bash
# CLI patch: disable the "task tools haven't been used recently" reminder.
#
# The reminder (both todo_reminder and task_reminder attachments) is gated by
# OC$={TURNS_SINCE_WRITE:10,TURNS_BETWEEN_REMINDERS:10} in the claude binary.
# Both conditions must hold, so setting SINCE_WRITE to 1e9 makes it unreachable.
# '1e9,...:9' is byte-for-byte the same length as '10,...:10' — safe in-place
# text patch (verified 2026-06-10 that the binary executes its embedded JS text,
# not bytecode, by patching a --help string and observing the output change).
#
# Contract (cli-patches): stderr reports applied/confirmed, exit 0.
# Exit 1 if the patch can't be applied (runner relays the message to Claude).
set -euo pipefail

PATTERN='TURNS_SINCE_WRITE:10,TURNS_BETWEEN_REMINDERS:10'
PATCHED='TURNS_SINCE_WRITE:1e9,TURNS_BETWEEN_REMINDERS:9'

# Target ONLY the active binary — `command -v claude` resolved through the
# symlink on Linux. Using a single active candidate (not a list including old
# versions) avoids the stale-old-version masking bug: an old patched binary
# lingering in versions/ (e.g. 2.1.197 after an update to 2.1.201) must not make
# the loop below confirm 'already patched' and skip the live one. Fall back to
# the newest file in versions/ only when `command -v claude` fails (e.g. Windows
# Git-Bash hitting a launcher shim); excludes .orig backups and .patch.* temps.
CANDIDATES=""
W="$(command -v claude || true)"
[ -n "$W" ] && CANDIDATES="$(readlink -f "$W")"
if [ -z "$CANDIDATES" ] || [ ! -f "$CANDIDATES" ]; then
    VDIR="$HOME/.local/share/claude/versions"
    if [ -d "$VDIR" ]; then
        CANDIDATES="$(find "$VDIR" -maxdepth 1 -type f ! -name '*.orig' ! -name '*.patch.*' -printf '%T@ %p\n' | sort -rn | sed -n '1s/^[^ ]* //p')"
    fi
fi

BIN=""
while IFS= read -r C; do
    [ -f "$C" ] || continue
    # MSYS/Git-Bash spoofs `.exe` for -f/grep but NOT for cp/mktemp/dd, so cp
    # would silently write the copy to "$TMP.exe" while $TMP stays a 0-byte
    # file — grep -abo on that empty file returns nothing, OFFSET goes empty,
    # dd errors, and pipefail kills the script before any error branch fires.
    # `find` doesn't spoof, so use it to canonicalize C to its real filename.
    if [ -z "$(find "$(dirname "$C")" -maxdepth 1 -name "$(basename "$C")" 2>/dev/null)" ] \
       && [ -n "$(find "$(dirname "$C")" -maxdepth 1 -name "$(basename "$C").exe" 2>/dev/null)" ]; then
        C="${C}.exe"
    fi
    if grep -aq "$PATCHED" "$C"; then
        echo "task-nag: confirmed already patched ($C)" >&2
        exit 0
    fi
    if grep -aq "$PATTERN" "$C"; then
        BIN="$C"
        break
    fi
done <<< "$CANDIDATES"

if [ -z "$BIN" ]; then
    echo "pattern '$PATTERN' not found in any candidate binary (tried: $(echo "$CANDIDATES" | tr '\n' ' ')) — upstream code changed or unknown install layout; re-investigate around the 'task_reminder' string in the binary" >&2
    exit 1
fi

COUNT=$(grep -ac "$PATTERN" "$BIN")
if [ "$COUNT" -ne 1 ]; then
    echo "expected exactly 1 occurrence of pattern in $BIN, found $COUNT — refusing to patch" >&2
    exit 1
fi

# Patch a copy, verify, then atomically swap in (in-place write on the live
# binary would hit ETXTBSY if a claude process is running from it).
TMP="$(mktemp "${BIN}.patch.XXXX")"
cp "$BIN" "$TMP"
OFFSET=$(grep -abo "$PATTERN" "$TMP" | cut -d: -f1)
printf '%s' "$PATCHED" | dd of="$TMP" bs=1 seek="$OFFSET" conv=notrunc status=none

if ! grep -aq "$PATCHED" "$TMP"; then
    rm -f "$TMP"
    echo "patch verification failed on $TMP — live binary untouched" >&2
    exit 1
fi

[ -e "${BIN}.orig" ] || cp "$BIN" "${BIN}.orig"
chmod +x "$TMP"
mv "$TMP" "$BIN"
echo "task-nag: applied patch to $BIN (pristine backup at ${BIN}.orig)" >&2
