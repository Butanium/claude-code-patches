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
#
# CLAUDE_CLI_PATCHES_EXTRA_DIRS adds directories *alongside* the built-in one
# (colon-separated, PATH-style) instead of replacing it. That is the seam for
# patches that can't live in this repo because it is public — keep them in a
# private checkout and point the variable at it. A relative entry resolves
# against $CLAUDE_CONFIG_DIR (default ~/.claude) so the same settings.json
# works on every machine; absolute and ~/-prefixed entries are taken as given.
# Directories are scanned in order, built-in first.
#
# CLAUDE_CLI_PATCH_EXTRA_TARGETS names further binaries to patch after the
# installed one (colon-separated): a file, or a directory whose newest
# non-backup file is taken — the layout of the Claude Desktop app's bundled CLI
# (`~/.claude/remote/ccd-cli/<version>`), which `which claude` never resolves
# to. Relative entries resolve against $CLAUDE_CONFIG_DIR like EXTRA_DIRS. Each
# target gets the whole patch pass through CLAUDE_CLI_PATCH_TARGET, including
# zz-bytecode-off, and its own `.orig` backup next to it.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="${CLAUDE_CLI_PATCHES_DIR:-$HERE/patches}"
SKIP=",${CLAUDE_CLI_PATCHES_SKIP:-},"
CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

DIRS=()
[ -d "$DIR" ] && DIRS+=("$DIR")

IFS=':' read -r -a _extra <<< "${CLAUDE_CLI_PATCHES_EXTRA_DIRS:-}"
for _d in ${_extra+"${_extra[@]}"}; do
    [ -n "$_d" ] || continue
    case "$_d" in
        /*)    ;;
        "~/"*) _d="$HOME/${_d#\~/}" ;;
        *)     _d="$CONFIG_DIR/$_d" ;;
    esac
    if [ -d "$_d" ]; then
        DIRS+=("$_d")
    else
        # stdout: a misconfigured private patch dir means its patches are
        # silently absent, which is exactly the failure worth surfacing.
        echo "CLAUDE_CLI_PATCHES_EXTRA_DIRS entry is not a directory: $_d"
    fi
done

[ "${#DIRS[@]}" -gt 0 ] || exit 0

# `python3` can be on PATH but non-functional — e.g. the Windows Store app
# execution alias, which exits nonzero without running anything. Probe once
# up front and fall back to `python` if that's the case.
PYTHON=python3
if ! python3 -c "" >/dev/null 2>&1; then
    if command -v python >/dev/null 2>&1 && python -c "" >/dev/null 2>&1; then
        PYTHON=python
    fi
fi

# Order by BASENAME across every directory, not directory-by-directory: the
# `zz-` prefix means "runs last" (zz-bytecode-off.py makes the other patches'
# text edits actually execute), and a per-directory walk would run it before
# the extra dirs' patches, leaving every one of them inert.
mapfile -t ORDERED < <(
    for dir in "${DIRS[@]}"; do
        for patch in "$dir"/*; do
            [ -f "$patch" ] && printf '%s\t%s\n' "$(basename "$patch")" "$patch"
        done
    done | LC_ALL=C sort -t"$(printf '\t')" -k1,1 -s | cut -f2-
)

# apply_all [target]: one pass over every patch. With a target, the patches aim
# at that file (CLAUDE_CLI_PATCH_TARGET) and messages name it; without, they
# find the installed binary themselves, or honor a CLAUDE_CLI_PATCH_TARGET
# already in the environment (one-off aiming at a copy). apply_patches.py runs
# the pass in one process so the binary is read and written once, not once per
# patch; it prints the per-patch success (stderr) and failure (stdout) messages.
apply_all() {
    local target="${1:-}" label="" patch name
    local todo=()
    if [ -n "$target" ]; then
        export CLAUDE_CLI_PATCH_TARGET="$target"
        label=" [$(basename "$target")]"
    fi
    for patch in ${ORDERED+"${ORDERED[@]}"}; do
        name="$(basename "$patch")"
        case "$name" in __pycache__|*.pyc|.*) continue;; esac
        case "$SKIP" in *",$name,"*) echo "cli-patch $name$label: skipped (CLAUDE_CLI_PATCHES_SKIP)" >&2; continue;; esac
        todo+=("$patch")
    done
    [ "${#todo[@]}" -gt 0 ] || return 0
    "$PYTHON" -B "$HERE/apply_patches.py" --label="$label" "${todo[@]}" \
        || echo "CLI patches$label: apply_patches.py exited $? — which patches are active is unknown"
}

apply_all

# newest_binary DIR: the newest regular file in DIR that is not a backup or a
# patch temp — the same rule _binpatch.candidate_binaries() uses for versions/.
newest_binary() {
    local f best=""
    for f in "$1"/*; do
        [ -f "$f" ] || continue
        case "$f" in *.orig|*.patch.*|*.patchold.*) continue;; esac
        if [ -z "$best" ] || [ "$f" -nt "$best" ]; then best="$f"; fi
    done
    [ -n "$best" ] && printf '%s\n' "$best"
}

IFS=':' read -r -a _targets <<< "${CLAUDE_CLI_PATCH_EXTRA_TARGETS:-}"
for _t in ${_targets+"${_targets[@]}"}; do
    [ -n "$_t" ] || continue
    case "$_t" in
        /*)    ;;
        "~/"*) _t="$HOME/${_t#\~/}" ;;
        *)     _t="$CONFIG_DIR/$_t" ;;
    esac
    if [ -d "$_t" ]; then
        _bin="$(newest_binary "$_t")"
        if [ -z "$_bin" ]; then
            echo "CLAUDE_CLI_PATCH_EXTRA_TARGETS entry has no binary in it: $_t"
            continue
        fi
    elif [ -f "$_t" ]; then
        _bin="$_t"
    else
        # stdout: a target that is gone means its patches are silently absent.
        echo "CLAUDE_CLI_PATCH_EXTRA_TARGETS entry is neither a file nor a directory: $_t"
        continue
    fi
    ( apply_all "$_bin" )
done

# stdout findings: a `\w` identifier capture works until the minifier emits a `$` name
"$PYTHON" -B "$HERE/lint_patches.py" "${DIRS[@]}" 2>&1

# Opt-in: prove the patches by behavior, not by anchor. When the live binary is
# in a state no suite run has seen yet (a patch just applied, or claude updated),
# start tests/run_tests.py in the background; it posts a pass/fail table to
# ntfy.sh/$CLI_PATCH_TESTS_NTFY_TOPIC if set. It spends real model calls for
# several minutes, hence opt-in.
if [ "${CLAUDE_CLI_PATCH_TESTS:-}" = "1" ]; then
    "$PYTHON" -B "$HERE/tests/after_patch.py" 2>&1
fi

# Opt-in: after an update, diff what the new binary exposes against the previous
# version (settings keys, env vars, hook events and payload fields, status-line
# payload, tools, commands, function-hook events, tengu_* flags) so additions
# don't sit unused for releases. Runs once per version pair (~10 s, synchronous so
# the summary lands in this session's context); the report goes next to the
# behavior-test results. CLAUDE_CLI_SURFACE_DIFF_NTFY_TOPIC, if set, also gets the
# summary when a surface a customized harness reads grew or extraction failed.
# stderr carries only unpack chatter; failures are printed to stdout.
if [ "${CLAUDE_CLI_SURFACE_DIFF:-}" = "1" ]; then
    "$PYTHON" -B "$HERE/surface_diff.py" --auto 2>/dev/null
fi

exit 0
