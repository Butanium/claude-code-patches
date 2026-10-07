// Replay the mod's decision over archived transcripts: for every `edited_text_file` notice since
// --since, would `wrote()` (imported from hooks/register.ts) have shortened it? Lists the kept
// notices whose same-request Bash commands name the file's basename, for eyeballing misses;
// --kept lists every kept notice with the request's tool calls and the previous request's commands.
// Run: node --experimental-strip-types replay.mts [--since 2026-09-26] [--show N] [--kept]
import { readdirSync, readFileSync, statSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { wrote } from "./hooks/register.ts"

const arg = (k: string, d: string) => { const i = process.argv.indexOf(k); return i > 0 ? process.argv[i + 1] : d }
const since = Date.parse(arg("--since", "2026-09-26") + "T00:00:00Z"), show = Number(arg("--show", "15"))
const HOME = homedir(), NOTE = /Note: (.+?) changed on disk since you last read it/
const root = join(HOME, ".claude/projects")
const seen = new Set<string>() // forks copy rows: count each notice once
let shortened = 0, kept = 0, keptChars = 0, shortChars = 0
const misses: string[] = [], allKept: string[] = []
const brief = (c: string) => c.replace(/\n/g, "⏎ ").slice(0, 250)
for (const proj of readdirSync(root)) {
  let files: string[] = []
  try { files = readdirSync(join(root, proj)).filter((f) => f.endsWith(".jsonl")) } catch { continue }
  for (const f of files) {
    const path = join(root, proj, f)
    if (statSync(path).mtimeMs < since) continue
    let cmds: string[] = [], prevCmds: string[] = [], tools: string[] = [], lastMsg: string | undefined
    for (const line of readFileSync(path, "utf8").split("\n")) {
      let row: any
      try { row = JSON.parse(line) } catch { continue }
      if (row.isSidechain) continue
      if (row.type === "assistant") {
        if (row.message?.id !== lastMsg) { lastMsg = row.message?.id; prevCmds = cmds; cmds = []; tools = [] }
        for (const b of row.message?.content ?? []) {
          if (b?.type !== "tool_use") continue
          if (b.name === "Bash") cmds.push(String(b.input?.command ?? ""))
          else tools.push(`${b.name} ${b.input?.file_path ?? b.input?.description ?? ""}`)
        }
        continue
      }
      const att = row.attachment
      if (row.type !== "attachment" || att?.type !== "edited_text_file" || !(Date.parse(row.timestamp) >= since)) continue
      if (seen.has(row.uuid)) continue
      seen.add(row.uuid)
      const text = (row.rendered ?? []).map((r: any) => r?.content ?? "").join("")
      const abs: string = att.filename, shown = NOTE.exec(text)?.[1] ?? abs, cwd: string = row.cwd ?? HOME
      const chars = (att.snippet ?? "").length
      if (cmds.some((c) => wrote(c, shown, abs, HOME, cwd))) { shortened++; shortChars += chars; continue }
      kept++; keptChars += chars
      allKept.push(`${f.slice(0, 8)} ${row.timestamp} ${abs} (${chars} chars)\n`
        + cmds.map((c) => "   $ " + brief(c)).join("\n") + (cmds.length ? "\n" : "")
        + tools.map((t) => "   · " + t).join("\n") + (tools.length ? "\n" : "")
        + prevCmds.map((c) => "   (previous request) $ " + brief(c)).join("\n"))
      const base = abs.slice(abs.lastIndexOf("/") + 1)
      if (cmds.some((c) => c.includes(base)))
        misses.push(`${f.slice(0, 8)} ${row.timestamp} ${abs} (${chars} chars)\n` + cmds.map((c) => "   $ " + brief(c)).join("\n"))
    }
  }
}
console.log(`${shortened + kept} notices: shortened ${shortened} (${shortChars} snippet chars), kept ${kept} (${keptChars} snippet chars); ` +
  `kept with a same-request Bash naming the basename: ${misses.length}`)
for (const m of (process.argv.includes("--kept") ? allKept : misses.slice(0, show))) console.log("\n" + m)
