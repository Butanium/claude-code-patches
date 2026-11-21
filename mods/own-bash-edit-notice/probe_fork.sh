#!/bin/bash
# Does a `--resume --fork-session` fork send the same notice text as its parent, and so read
# the parent's prompt cache? Parent: one own Bash edit (shortened notice) then two more turns.
# Fork: one turn. Requests are captured by probe_logproxy.py; usage comes from the transcripts.
# Usage: ./probe_fork.sh [--plugin-dir DIR]   (default: this directory; the installed copy is turned off)
set -u
HERE=$(pwd); P=$(cd "$(dirname "$0")" && pwd); PORT=18778; LOG=$HERE/fork-requests.jsonl; rm -f "$LOG"
PDIR=${2:-$P}
export DISABLE_AUTOUPDATER=1
uv run --no-project --with httpx --with starlette --with uvicorn "$P/probe_logproxy.py" $PORT "$LOG" 2> "$HERE/fork-proxy.err" &
PROXY=$!; sleep 4
D=/var/tmp/bed-mod-fork-probe; rm -rf "$D"; mkdir -p "$D"; cd "$D"
SID=$(python3 -c 'import uuid; print(uuid.uuid4())')
BIN=$(readlink -f "$(command -v claude)")
OFF='{"enabledPlugins":{"own-bash-edit-notice@claude-code-patches":false}}'  # only the --plugin-dir copy runs
ENV=(env CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 _CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1 ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT)
Q="Do these steps with tools, one tool call each: 1) Write tool: create $D/f.txt with 40 lines 'line 1' to 'line 40'. 2) Bash: python3 -c \"import pathlib; p=pathlib.Path('$D/f.txt'); p.write_text(p.read_text().replace('line 5\n','LINE FIVE\n'))\" 3) Bash: echo step3 4) Bash: echo step4. Then reply ok."
"${ENV[@]}" timeout 300 "$BIN" -p --model haiku --dangerously-skip-permissions --session-id "$SID" --settings "$OFF" --plugin-dir "$PDIR" \
  --debug-file "$HERE/fork-parent-debug.log" "$Q" < /dev/null > "$HERE/fork-parent.out" 2>&1
"${ENV[@]}" timeout 300 "$BIN" -p --model haiku --dangerously-skip-permissions --resume "$SID" --fork-session --settings "$OFF" --plugin-dir "$PDIR" \
  --output-format json --debug-file "$HERE/fork-child-debug.log" "Reply with the single word ok." < /dev/null > "$HERE/fork-child.out" 2>&1
kill $PROXY 2>/dev/null
FSID=$(python3 -c "import json,sys; print(json.load(open('$HERE/fork-child.out'))['session_id'])" 2>/dev/null)
python3 - "$LOG" "$SID" "$FSID" <<'PY'
import json, sys, glob
log, sid, fsid = sys.argv[1:]
reqs = [json.loads(l)["body"] for l in open(log)]
def notes(b):
    s = json.dumps(b)
    return ("short" if "after your own Bash command" in s else "") + ("full" if "f.txt changed on disk since you last read it" in s else "")
print("requests:", len(reqs), "| notice form per request:", [notes(b) or "-" for b in reqs])
for s, name in ((sid, "parent"), (fsid, "fork")):
    files = glob.glob(f"/home/c.dumas/.claude/projects/*/{s}.jsonl")
    if not files: print(name, "transcript not found"); continue
    last = {}
    for l in open(files[0]):
        r = json.loads(l)
        if r.get("type") == "assistant":
            last[r["message"]["id"]] = r["message"]["usage"]
    us = list(last.values())
    u = us[-1] if name == "fork" else us[-1]
    print(f"{name}: last call cache_read={u.get('cache_read_input_tokens')} cache_write={u.get('cache_creation_input_tokens')}")
PY
grep -ho "own-bash-edit-notice: [^\"]*" "$HERE"/fork-*-debug.log | sort | uniq -c
