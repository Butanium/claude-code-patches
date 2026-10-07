#!/bin/bash
# Probe the own-bash-edit-notice mod on the wire (haiku -p through a logging proxy).
# INSTALLED=1 ./probe.sh tests the installed plugin (no --plugin-dir) in a fresh session.
#   f.txt  own write naming it (python3 -c)                  -> one-line note
#   g.txt  own script that doesn't name it (mod.sh)          -> full notice
#   h.txt  read-only command naming it, then an OUTSIDE edit -> full notice
#   k.txt  own sed -i                                        -> one-line note
#   sub/q.txt  `cd` into the dir, sed -i on the relative path -> one-line note
#   n.txt  `cp n.txt …` (names it, write verb, n unchanged), then two requests later an
#          OUTSIDE edit                                       -> full notice (the window)
set -u
HERE=$(pwd); P=$(cd "$(dirname "$0")" && pwd); PORT=18777; LOG=$HERE/requests.jsonl; rm -f "$LOG"
export DISABLE_AUTOUPDATER=1
uv run --no-project --with httpx --with starlette --with uvicorn "$P/probe_logproxy.py" $PORT "$LOG" 2> "$HERE/proxy.err" &
PROXY=$!; sleep 4
D=/var/tmp/bed-mod-probe; rm -rf "$D"; mkdir -p "$D"
printf '#!/bin/bash\nsed -i "s/^line 7$/LINE SEVEN/" %s/g.txt\n' "$D" > "$D/mod.sh"; chmod +x "$D/mod.sh"
# the outside editor: another process, touching files only when the session drops a trigger
( for i in $(seq 600); do
    [ -e "$D/trigger1" ] && [ ! -e "$D/done1" ] && { echo "outside edit" >> "$D/h.txt"; touch "$D/done1"; }
    [ -e "$D/trigger3" ] && [ ! -e "$D/done3" ] && { echo "outside edit" >> "$D/n.txt"; touch "$D/done3"; }
    sleep 0.2; done ) &
WATCH=$!
Q="Do these steps with tools, in order, one tool call each: 1) Write tool: create $D/f.txt with 40 lines 'line 1' to 'line 40'. 2) Write tool: create $D/g.txt, $D/h.txt, $D/k.txt, $D/n.txt and $D/sub/q.txt the same way (five Write calls). 3) Bash: python3 -c \"import pathlib; p=pathlib.Path('$D/f.txt'); p.write_text(p.read_text().replace('line 5\n','LINE FIVE\n'))\" 4) Bash: $D/mod.sh 5) Bash: cat $D/h.txt > /dev/null; touch $D/trigger1; sleep 2 6) Bash: sed -i 's/^line 3$/LINE THREE/' $D/k.txt 7) Bash: cd $D && sed -i 's/^line 2$/LINE TWO/' sub/q.txt 8) Bash: cp $D/n.txt /tmp/n-backup.txt 9) Bash: echo step9 10) Bash: touch $D/trigger3; sleep 2 11) Bash: echo step11. Then reply ok."
BIN=$(readlink -f "$(command -v claude)")
PD=(--plugin-dir "$P"); [ -n "${INSTALLED:-}" ] && PD=()
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 _CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1 ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT timeout 400 "$BIN" -p --model haiku \
  --dangerously-skip-permissions --no-session-persistence "${PD[@]}" --debug-file "$HERE/debug.log" "$Q" < /dev/null > "$HERE/mod.out" 2>&1
kill $PROXY $WATCH 2>/dev/null
for f in f g h k n sub/q; do
  echo "$f.txt: $(grep -o "$D/$f.txt changed on disk [a-z ]*" "$LOG" | sort | uniq -c | tr '\n' ';')"
done
grep -o "own-bash-edit-notice: [^\"]*" "$HERE/debug.log" | sort -u
