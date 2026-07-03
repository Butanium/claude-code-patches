#!/bin/bash
# SessionStart hook: apply every CLI patch in this repo's patches/ directory.
#
# Patch contract: each script is idempotent, prints to stderr whether it applied
# the patch or confirmed it was already applied, and exits 0. Exit nonzero means
# the patch could NOT be applied (e.g. upstream code changed after an update).
#
# Failures are printed to stdout so they land in Claude's context (SessionStart
# stdout is injected as context); successes go to stderr (verbose-mode only).
#
# Override the patch directory with CLAUDE_CLI_PATCHES_DIR if you keep your own
# set elsewhere.
set -u

DIR="${CLAUDE_CLI_PATCHES_DIR:-$(cd "$(dirname "$0")" && pwd)/patches}"
[ -d "$DIR" ] || exit 0

for patch in "$DIR"/*; do
    [ -f "$patch" ] || continue
    name="$(basename "$patch")"
    if [ ! -x "$patch" ]; then
        echo "CLI patch '$name' is not executable — skipped (chmod +x it or remove it): $patch"
        continue
    fi
    output="$("$patch" 2>&1)"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "cli-patch $name: $output" >&2
    else
        echo "CLI patch '$name' FAILED (exit $rc) — its behavior change is NOT active for the current claude binary:"
        echo "$output"
        echo "Script: $patch"
    fi
done

exit 0
