// Run: node --experimental-strip-types test_wrote.mts   (exit code = number of failures)
import { wrote } from "./hooks/register.ts"
const H = "/home/user", CWD = "/home/user/playground"
const cases: [string, string, boolean][] = [
  ["cd ~/.claude/skills/my-skill && python3 - <<'EOF'\np = Path(\"SKILL.md\")\nEOF", "/home/user/.claude/skills/my-skill/SKILL.md", true],
  ["cd ~/research/proj && python3 - <<'EOF'\np = Path('src/proj/mine.py'); s = p.read_text()\nEOF", "/home/user/research/proj/src/proj/mine.py", true],
  ["cd ~/tools/kit && cat >> LESSONS.md <<'EOF'\nx\nEOF", "/home/user/tools/kit/LESSONS.md", true],
  ["cd ~/.claude/hooks && sed -i 's/a/b/' tests/smoke.sh", "/home/user/.claude/hooks/tests/smoke.sh", true],
  ["cd fig1 && python3 - <<'EOF'\np = Path(\"v1/draw.js\")\nEOF", "/home/user/playground/fig1/v1/draw.js", true],
  ["sed -i 's/a/b/' notes/x.md", "/home/user/playground/notes/x.md", true],
  // negatives
  ["cd ~/.claude/skills/my-skill && cat SKILL.md", "/home/user/.claude/skills/my-skill/SKILL.md", false],
  ["cd ~/.claude/skills/my-skill && python3 - <<'EOF'\np = Path(\"../other-skill/SKILL.md\")\nEOF", "/home/user/.claude/skills/my-skill/SKILL.md", false],
  ["cd ~/other && python3 x.py", "/home/user/.claude/x.py", false],
  ["cd ~/a && python3 - <<'EOF'\nPath('b/xc.py')\nEOF", "/home/user/a/c.py", false],
]
let bad = 0
for (const [cmd, abs, want] of cases) {
  const got = wrote(cmd, abs, abs, H, CWD)
  if (got !== want) bad++
  console.log(got === want ? "PASS" : "FAIL", want, cmd.split("\n")[0].slice(0, 70))
}
process.exit(bad)
