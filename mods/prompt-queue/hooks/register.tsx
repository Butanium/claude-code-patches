import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Blocker, Gate, QueueItem } from '../types'

// Prompts held by this module until the main loop is idle, then submitted one per idle
// point as a turn of their own. A prompt typed with plain Enter mid-turn is folded into
// the running turn at its next tool round; these are not. `/queue` waits for the turn
// to end, `/hqueue` also for background work. Both commands are `immediate` so they
// land in the band at once instead of waiting for the turn themselves.
//
// Release happens at `turn.complete`, not `classic.Stop`: a Stop hook can block and keep
// the turn going. `background_tasks` is only reported at Stop, so the snapshot taken
// there is what the quiet gate reads; a finishing task notifies, which runs a turn and
// refreshes it. That list has two blind spots (2.1.293): every teammate is `running`
// for as long as it exists, idle or not, and a Monitor is a `shell` task like any
// other. Teammates are therefore read from `$.agent.list()` instead, polled while a
// quiet item waits (a teammate going idle does not wake the lead), and shell tasks
// whose command matches IGNORED_COMMAND count as watchers, not work.

const RELEASE_DELAY_MS = 300
const POLL_MS = 5000
const IGNORED_TASK_TYPES = new Set(['monitor', 'teammate'])
const IGNORED_COMMAND = /^\s*(bgwatch|tail\s+-[fF])\b/

const items = atom({ plugin: 'prompt-queue', key: 'items' } as const, [])
const nextId = atom({ plugin: 'prompt-queue', key: 'nextId' } as const, 1)
const isRunning = atom({ plugin: 'prompt-queue', key: 'isRunning' } as const, false)
const isPaused = atom({ plugin: 'prompt-queue', key: 'isPaused' } as const, false)
const blockers = atom({ plugin: 'prompt-queue', key: 'blockers' } as const, [])
const teammates = atom({ plugin: 'prompt-queue', key: 'teammates' } as const, [])

// The engine records `/queue <text>` as a row the model reads at the start of the next
// turn, which for an `/hqueue` item can be long before its release: the model would act
// on the text early. The record keeps the command and loses the text.
const RECORD = /<command-name>\/h?queue<\/command-name>/
const RECORD_ARGS = /<command-args>[\s\S]*?<\/command-args>/
const HELD = '<command-args>(held by prompt-queue until its release)</command-args>'

const COMMANDS: Record<string, Gate> = { queue: 'idle', hqueue: 'quiet' }
const GATE_LABEL: Record<Gate, string> = { idle: 'idle', quiet: 'idle + no bg' }

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`

export function toBlockers(tasks: readonly { type: string; description: string; command?: string }[] | undefined): Blocker[] {
  return (tasks ?? [])
    .filter(t => !IGNORED_TASK_TYPES.has(t.type) && !(t.command !== undefined && IGNORED_COMMAND.test(t.command)))
    .map(t => ({ type: t.type, description: t.command ?? t.description }))
}

export function pickReleasable(list: readonly QueueItem[], blocked: readonly Blocker[]): QueueItem | undefined {
  return list.find(item => item.gate === 'idle' || blocked.length === 0)
}

// A queued `/name args` runs as that slash command; anything else is a prompt.
export function asCommand(text: string): { command: string; args: string } | undefined {
  const m = /^\/([A-Za-z0-9_:-]+)(?:\s+([\s\S]*))?$/.exec(text.trim())
  return m?.[1] ? { command: m[1], args: m[2] ?? '' } : undefined
}

export function toTeammateBlockers(agents: readonly { teammateId?: string; description: string; status: string }[]): Blocker[] {
  return agents
    .filter(a => a.teammateId !== undefined && a.status === 'running')
    .map(a => ({ type: 'teammate', description: a.teammateId ?? a.description }))
}

async function refreshTeammates($: EngineInterface): Promise<void> {
  const next = toTeammateBlockers(await $.agent.list())
  const prev = await read($, teammates)
  if (JSON.stringify(prev) !== JSON.stringify(next)) await update($, teammates, () => next)
}

async function allBlockers($: EngineInterface): Promise<Blocker[]> {
  return [...(await read($, blockers)), ...(await read($, teammates))]
}

export function stripRecordArgs<B extends { type: string; text?: string }>(content: readonly B[]): B[] | undefined {
  let changed = false
  const out = content.map(b => {
    if (b.type !== 'text' || b.text === undefined || !RECORD.test(b.text)) return b
    changed = true
    return { ...b, text: b.text.replace(RECORD_ARGS, HELD) }
  })
  return changed ? out : undefined
}

async function refreshStatus($: EngineInterface): Promise<void> {
  const n = (await read($, items)).length
  $.ui.status(n === 0 ? undefined : `${n} queued${(await read($, isPaused)) ? ' (paused)' : ''}`)
}

async function remove($: EngineInterface, id: number): Promise<QueueItem | undefined> {
  const item = (await read($, items)).find(i => i.id === id)
  await update($, items, list => list.filter(i => i.id !== id))
  await refreshStatus($)
  return item
}

// Not awaited by callers inside a hook: `$.prompt.submit` resolves when the new turn
// starts, which cannot happen while the hook that called it is still running.
async function submit($: EngineInterface, item: QueueItem): Promise<void> {
  try {
    const cmd = asCommand(item.text)
    if (cmd) {
      await $.command.run(cmd)
      return
    }
    const r = await $.prompt.submit({ text: item.text, asUser: true })
    if ('drop' in r && r.drop !== undefined) $.ui.toast(`queue: prompt #${item.id} was dropped by a hook: ${r.drop}`)
  } catch (err) {
    await update($, items, list => [item, ...list])
    await update($, isPaused, () => true)
    await refreshStatus($)
    $.ui.toast(`queue: could not send #${item.id}, paused: ${err instanceof Error ? err.message : String(err)}`)
  }
}

async function release($: EngineInterface): Promise<void> {
  if ((await read($, isRunning)) || (await read($, isPaused))) return
  const item = pickReleasable(await read($, items), await allBlockers($))
  if (!item) return
  await remove($, item.id)
  void submit($, item)
}

const scheduleRelease = ($: EngineInterface) => $.clock.after(RELEASE_DELAY_MS, () => void release($))

async function enqueue($: EngineInterface, text: string, gate: Gate): Promise<QueueItem> {
  const id = await read($, nextId)
  await update($, nextId, n => n + 1)
  const item: QueueItem = { id, text, gate }
  await update($, items, list => [...list, item])
  await refreshStatus($)
  scheduleRelease($)
  return item
}

async function describeQueue($: EngineInterface): Promise<string> {
  const list = await read($, items)
  if (list.length === 0) return 'Queue is empty. /queue <prompt> waits for idle; /hqueue <prompt> also waits for background tasks.'
  const blocked = await allBlockers($)
  const lines = list.map((i, k) => `${k + 1}. [${GATE_LABEL[i.gate]}] #${i.id}`)
  const why = (await read($, isPaused)) ? ' Paused: the last turn was interrupted.' : ''
  const bg = blocked.length ? ` Background: ${blocked.map(b => b.type).join(', ')}.` : ''
  return `${plural(list.length, 'queued prompt')} (texts in the band above the prompt).${why}${bg}\n${lines.join('\n')}`
}

async function poll($: EngineInterface): Promise<void> {
  if (await read($, isRunning)) return
  if (!(await read($, items)).some(i => i.gate === 'quiet')) return
  await refreshTeammates($)
  await release($)
}

async function runQueue($: EngineInterface, args: string, gate: Gate) {
  const text = args.trim()
  if (text === '') return { text: await describeQueue($) }
  const item = await enqueue($, text, gate)
  const waits = gate === 'idle' ? 'until Claude is idle' : 'until Claude is idle with no background tasks'
  return { text: `Queued #${item.id}; it waits ${waits}.` }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    for (const [name, gate] of Object.entries(COMMANDS)) {
      await $.command.register({
        name,
        description:
          gate === 'idle'
            ? 'Send a prompt once Claude is idle (not folded into the running turn)'
            : 'Send a prompt once Claude is idle and no background task is running',
        argumentHint: '<prompt>',
        immediate: true,
      })
    }
    await refreshStatus($)
    $.clock.every(POLL_MS, () => void poll($))
    return next(e)
  })

  // The answer is a transcript row the model reads, possibly inside the running turn:
  // it names the item's number, never its text.
  on('command.run', { command: 'queue' }, ($, e) => runQueue($, e.args, 'idle'))
  on('command.run', { command: 'hqueue' }, ($, e) => runQueue($, e.args, 'quiet'))

  on('session.append', ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    const content = stripRecordArgs(e.message.content as { type: string; text?: string }[])
    return content ? next({ ...e, message: { ...e.message, content: content as typeof e.message.content } }) : next(e)
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    await update($, isRunning, () => true)
    return next(e)
  })

  on('classic.Stop', async ($, e, next) => {
    const r = await next(e)
    await update($, blockers, () => toBlockers(e.background_tasks))
    await refreshTeammates($)
    return r
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId !== undefined) return done
    await update($, isRunning, () => false)
    // An interrupt means the person took the wheel; firing the next prompt then would
    // surprise them. The next turn that ends on its own resumes the queue.
    await update($, isPaused, () => e.isAborted)
    await refreshStatus($)
    scheduleRelease($)
    return done
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const list = await read($, items)
    if (e.props.hasSurvey || list.length === 0) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const paused = await read($, isPaused)
    const blocked = await allBlockers($)
    const hasQuiet = list.some(i => i.gate === 'quiet')

    const state = paused
      ? 'paused: the last turn was interrupted'
      : e.props.isWorking
        ? 'waiting for this turn to end'
        : hasQuiet && blocked.length
          ? 'idle, waiting on background tasks'
          : 'sending'

    const sendNow = (item: QueueItem) => async () => {
      if (await remove($, item.id)) void submit($, item)
    }
    const edit = (item: QueueItem) => async () => {
      if (!(await remove($, item.id))) return
      const cmd = item.gate === 'idle' ? 'queue' : 'hqueue'
      await $.prompt.fill({ text: `/${cmd} ${item.text}`, mode: 'replace' })
    }

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={1}>
          <Text bold color="claude">
            ⧗ {plural(list.length, 'queued prompt')}
          </Text>
          <Text dimColor wrap="truncate-end">
            · {state}
          </Text>
          {paused && (
            <Button key="resume" label="resume" hotkey="r" variant="primary" onPress={async () => {
              await update($, isPaused, () => false)
              await refreshStatus($)
              void release($)
            }} />
          )}
          {list.length > 1 && (
            <Button key="clear" label="clear all" dimColor onPress={async () => {
              await update($, items, () => [])
              await refreshStatus($)
            }} />
          )}
        </Box>
        {hasQuiet && blocked.length > 0 && (
          <Text dimColor wrap="truncate-end">
            {'  '}bg: {blocked.map(b => `${b.type} ${b.description}`).join(' · ')}
          </Text>
        )}
        {list.map((item, k) => (
          <Box key={`item-${item.id}`} flexDirection="row" gap={1}>
            <Text dimColor>{String(k + 1).padStart(2)}</Text>
            <Text color={item.gate === 'idle' ? 'suggestion' : 'warning'}>{item.gate === 'idle' ? 'queue ' : 'hqueue'}</Text>
            <Box flexGrow={1} flexShrink={1}>
              <Text wrap="truncate-end">{item.text.replace(/\s+/g, ' ')}</Text>
            </Box>
            <Button key={`send-${item.id}`} label="send now" {...(k === 0 ? { hotkey: 's' } : {})} dimColor onPress={sendNow(item)} />
            <Button key={`edit-${item.id}`} label="edit" {...(k === 0 ? { hotkey: 'e' } : {})} dimColor onPress={edit(item)} />
            <Button key={`drop-${item.id}`} label="×" {...(k === 0 ? { hotkey: 'x' } : {})} dimColor onPress={async () => void (await remove($, item.id))} />
          </Box>
        ))}
      </Box>
    )
  })
}
