import type { EngineInterface, Register } from 'claude-code'

// Auto-compact for one session while the global `autoCompactEnabled` stays off. A mod
// cannot compact mid-turn ($.session.compact rejects while a turn runs), so the check
// runs when a main-loop turn ends. A turn that grows from below the threshold past the
// whole window still hits the context limit; the compaction then runs when it ends.
// The setting is keyed by session id in $.store so it survives `claude -r`; /clear and
// --fork-session start a new id, which starts off.

export type Threshold = { percent: number } | { tokens: number }

const COMMAND = 'session-autocompact'
const DEFAULT: Threshold = { percent: 80 }
const RETRIES = 5

let compacting = false
let turnRunning = false

export function parse(arg: string): Threshold | 'off' | 'status' | undefined {
  const a = arg.trim().toLowerCase()
  if (a === '' || a === 'status') return 'status'
  if (a === 'off') return 'off'
  if (a === 'on') return DEFAULT
  const m = /^(\d+(?:\.\d+)?)\s*(%|k|m)?$/.exec(a)
  if (!m) return undefined
  const n = Number(m[1])
  const unit = m[2]
  if (unit === '%' || (unit === undefined && n <= 100)) return n > 0 && n < 100 ? { percent: n } : undefined
  const tokens = Math.round(n * (unit === 'k' ? 1e3 : unit === 'm' ? 1e6 : 1))
  return tokens >= 1000 ? { tokens } : undefined
}

export function fmtTokens(n: number): string {
  return n >= 1e6 ? `${+(n / 1e6).toFixed(2)}M` : `${Math.round(n / 1e3)}k`
}

export const label = (t: Threshold) => ('percent' in t ? `${t.percent}%` : fmtTokens(t.tokens))

export function isOver(t: Threshold, tokens: number, window: number): boolean {
  return 'percent' in t ? (tokens / window) * 100 >= t.percent : tokens >= t.tokens
}

const storeKey = async ($: EngineInterface) => `on:${await $.session.id()}`

async function load($: EngineInterface): Promise<Threshold | undefined> {
  const v = (await $.store.get(await storeKey($))) as Threshold | undefined
  return v && ('percent' in v || 'tokens' in v) ? v : undefined
}

function showStatus($: EngineInterface, t: Threshold | undefined): void {
  $.ui.status(t ? `on (≥${label(t)})` : undefined)
}

function schedule($: EngineInterface, attempt: number): void {
  $.clock.after(attempt === 0 ? 200 : 1000, () => void check($, attempt))
}

async function check($: EngineInterface, attempt: number): Promise<void> {
  if (compacting || turnRunning) return
  const t = await load($)
  if (!t) return
  const { context } = await $.session.usage()
  if (context.tokens === undefined || !isOver(t, context.tokens, context.window)) return
  if (attempt === 0) {
    const pct = Math.round((context.tokens / context.window) * 100)
    $.ui.log(`context at ${fmtTokens(context.tokens)} (${pct}%), past ${label(t)}; compacting`)
  }
  compacting = true
  let failed: unknown
  try {
    const r = await $.session.compact()
    if (r.skip !== undefined) $.ui.log(`compaction skipped: ${r.skip}`)
  } catch (err) {
    failed = err
  } finally {
    compacting = false
  }
  if (failed === undefined) return
  if (attempt < RETRIES && !turnRunning) schedule($, attempt + 1)
  else $.ui.log(`compaction failed: ${failed instanceof Error ? failed.message : String(failed)}`)
}

async function run($: EngineInterface, args: string): Promise<string> {
  const parsed = parse(args)
  const usage = `/${COMMAND} on (≥${label(DEFAULT)}) | off | <percent> (70, 70%) | <tokens> (400k, 1m)`
  if (parsed === undefined) return `Couldn't parse '${args.trim()}'. Usage: ${usage}`
  const key = await storeKey($)
  if (parsed === 'off') {
    await $.store.delete(key)
    showStatus($, undefined)
    return `off for this session.`
  }
  const { context } = await $.session.usage()
  const now = context.tokens === undefined ? '' : ` (now ${fmtTokens(context.tokens)}, ${Math.round((context.tokens / context.window) * 100)}%)`
  if (parsed === 'status') {
    const t = await load($)
    return t
      ? `on, compacts when a turn ends with context ≥ ${label(t)}${now}.`
      : `off for this session${now}. Usage: ${usage}`
  }
  await $.store.set(key, parsed)
  showStatus($, parsed)
  const isPast = context.tokens !== undefined && isOver(parsed, context.tokens, context.window)
  if (isPast) schedule($, 0)
  return `on, compacts when a turn ends with context ≥ ${label(parsed)}${now}.${isPast ? ' Already past it: compacting now.' : ''}`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: COMMAND,
      description: 'Auto-compact this session only, when a turn ends past a context threshold',
      argumentHint: '[on|off|70%|400k]',
    })
    showStatus($, await load($))
    return next(e)
  })

  on('command.run', { command: COMMAND }, async ($, e) => ({ text: await run($, e.args) }))

  on('turn.start', async ($, e, next) => {
    turnRunning = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId !== undefined) return done
    turnRunning = false
    showStatus($, await load($))
    if (!e.isAborted) schedule($, 0)
    return done
  })
}
