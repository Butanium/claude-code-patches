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
# Patches are invoked by extension (.py -> python3, .sh -> bash) rather than by
# the executable bit. The bit is git metadata that a submodule checkout, a umask
# or an editor can drop, and losing it used to park a patch silently — the
# supported way to park one is CLAUDE_CLI_PATCHES_SKIP, below.
#
# Override the patch directory with CLAUDE_CLI_PATCHES_DIR if you keep your own
# set elsewhere. To park a patch without deleting it, list its filename in
# CLAUDE_CLI_PATCHES_SKIP (comma-separated, e.g. "idle-notif.py,task-nag.py");
# skipped patches are neither applied nor reported. Note this only stops
# re-applying: a patch already baked into the current binary stays until the
# next claude update ships a fresh one (or you restore from the .orig backup).
set -u

DIR="${CLAUDE_CLI_PATCHES_DIR:-$(cd "$(dirname "$0")" && pwd)/patches}"
[ -d "$DIR" ] || exit 0
SKIP=",${CLAUDE_CLI_PATCHES_SKIP:-},"

# `python3` can be on PATH but non-functional — e.g. the Windows Store app
# execution alias, which exits nonzero without running anything. Probe once
# up front and fall back to `python` if that's the case.
PYTHON=python3
if ! python3 -c "" >/dev/null 2>&1; then
    if command -v python >/dev/null 2>&1 && python -c "" >/dev/null 2>&1; then
        PYTHON=python
    fi
fi

run_patch() {
    case "$1" in
        *.py) "$PYTHON" "$1" 2>&1 ;;
        *.sh) bash "$1" 2>&1 ;;
        *)    if [ -x "$1" ]; then "$1" 2>&1
              else echo "no interpreter for this extension and the file is not executable"; return 126
              fi ;;
    esac
}

for patch in "$DIR"/*; do
    [ -f "$patch" ] || continue
    name="$(basename "$patch")"
    case "$name" in __pycache__|*.pyc|.*) continue;; esac
    case "$SKIP" in *",$name,"*) echo "cli-patch $name: skipped (CLAUDE_CLI_PATCHES_SKIP)" >&2; continue;; esac

    output="$(run_patch "$patch")"
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
