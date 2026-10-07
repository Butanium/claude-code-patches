// The engine's `edited_text_file` notice ("Note: <file> changed on disk since you last
// read it…" plus a diff snippet) also fires when the session changed the file itself
// through Bash (python/sed/heredoc). When the session made the change the snippet is
// up to 8 KB of repeated context; when someone else did, it is the only way the session
// learns the file moved under it, so swallowing a real outside change is the dangerous
// direction. The notice is cut to one line only when ALL of these hold:
//   - a Bash command of this loop ran since the previous model request (the notice is
//     computed for the first request after the change);
//   - that command names the file by its absolute path, `~` path or the path as the
//     notice shows it, or by its path relative to the session cwd or to a directory the
//     command `cd`s into;
//   - it plausibly WROTE it: a redirect into that path, or a writing verb (sed -i, tee,
//     mv/cp, patch, truncate, git checkout/restore/apply/stash/reset, a python/node/perl/
//     ruby script) in a command naming it. `cat`, `jq .`, `git diff`, `grep` don't count.

const WRITE_VERB = /\bsed\s+(?:-\S+\s+)*-i|\bperl\s+-\S*i|\btee\b|\bmv\b|\bcp\b|\bpatch\b|\btruncate\b|\bgit\s+(?:checkout|restore|apply|stash|reset)\b|\bpython3?\b|\bnode\b|\bruby\b|\bperl\b/
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")

type Ran = { cmd: string; step: number }
const steps = new Map<string, number>() // loop -> model requests sent so far
const ran = new Map<string, Ran[]>()   // loop -> Bash commands, tagged with the step they followed
const loop = (e: any): string => e.agentId ?? "main"

function forms(shown: string, abs: string, home: string): string[] {
  const out = new Set([shown, abs])
  if (home && abs.startsWith(home + "/")) out.add("~" + abs.slice(home.length))
  return [...out].filter(Boolean)
}

function resolve(p: string, home: string, cwd: string): string {
  const raw = home && (p === "~" || p.startsWith("~/")) ? home + p.slice(1) : p
  const out: string[] = []
  for (const s of (raw.startsWith("/") ? raw : `${cwd}/${raw}`).split("/")) {
    if (s === "..") out.pop()
    else if (s && s !== ".") out.push(s)
  }
  return "/" + out.join("/")
}

// The file's path relative to the session cwd and to every directory the command `cd`s into.
function relatives(c: string, abs: string, home: string, cwd: string): string[] {
  const dirs = [cwd]
  for (const m of c.matchAll(/(?:^|[;&|(\n])\s*cd\s+(['"]?)([^\s'";&|)]+)\1/g)) dirs.push(resolve(m[2], home, cwd))
  return [...new Set(dirs.filter((d) => d && abs.startsWith(d + "/")).map((d) => abs.slice(d.length + 1)))]
}

export function wrote(cmd: string, shown: string, abs: string, home: string, cwd: string): boolean {
  const c = cmd.replace(/\d?>&\d|&>\s*\/dev\/null|\d?>>?\s*\/dev\/null/g, " ")
  const names = forms(shown, abs, home)
  const rels = relatives(c, abs, home, cwd)
  const named = names.some((f) => c.includes(f))
    || rels.some((r) => new RegExp(`(?:^|[\\s'"=(,:])${esc(r)}(?:$|[\\s'"),;|&:])`).test(c))
  if (!named) return false
  if ([...names, ...rels].some((f) => new RegExp(`>>?\\s*['"]?${esc(f)}(?:['"\\s;|&)]|$)`).test(c))) return true
  return WRITE_VERB.test(c)
}

export function register(on: any): void {
  on("turn.step", async function* ($: any, e: any, next: any) {
    steps.set(loop(e), (steps.get(loop(e)) ?? 0) + 1)
    return yield* next(e) // turn.step streams: forward the chunks untouched
  })

  on("tool.call", { tool: "Bash" }, async ($: any, e: any, next: any) => {
    const list = ran.get(loop(e)) ?? []
    list.push({ cmd: String(e.command ?? ""), step: steps.get(loop(e)) ?? 0 })
    ran.set(loop(e), list.slice(-20))
    return next(e)
  })

  on("prompt.attachment", { type: "edited_text_file" }, async ($: any, e: any, next: any) => {
    const m = /^Note: (.+?) changed on disk since you last read it/.exec(e.text)
    if (!m) return next(e)
    const shown = m[1]
    // A hooks module has no `process`: HOME comes through $.env.
    const home = (await $.env.get("HOME")) ?? ""
    const cwd = await $.session.cwd()
    let abs = shown.startsWith("~/") && home ? home + shown.slice(1) : shown
    if (!abs.startsWith("/")) abs = `${cwd}/${abs.replace(/^\.\//, "")}`
    // turn.step for this request fires before its attachments are resolved, so the
    // previous request is step - 1: commands tagged with it ran after that request.
    const since = (steps.get(loop(e)) ?? 0) - 1
    const recent = (ran.get(loop(e)) ?? []).filter((r) => r.step >= since)
    const cmd = recent.map((r) => r.cmd).findLast((c) => wrote(c, shown, abs, home, cwd))
    $.ui.log(`own-bash-edit-notice: ${abs} at step ${steps.get(loop(e))}, ${recent.length} candidate command(s), `
      + (cmd === undefined ? "full notice kept" : "shortened"))
    if (cmd === undefined) return next(e)
    const head = cmd.replace(/\s+/g, " ").slice(0, 80)
    return next({
      ...e,
      text: `Note: ${shown} changed on disk after your own Bash command (\`${head}\`), so its diff is left out. `
        + `The copy of it in your context is stale; Read it before relying on its exact current content.`,
    })
  })
}
