import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, TurnStepChunk } from 'claude-code'

import type { KeystrokeMode } from '../types'

// Keystroke mode: the model composes its reply through three tools (type, backspace,
// send) instead of writing text directly, at most `chunk` characters per `type` call.
// The per-keystroke tool rows are hidden, the draft is drawn live in the band above the
// prompt, and once `send` runs the next model step is answered by this module: the
// draft is yielded as the step's text and the turn ends, so the transcript holds a
// normal assistant message that no model call produced.

const PLUGIN = 'keystroke'
const COMMAND = 'keystroke'
const TOOLS = ['type', 'backspace', 'send'] as const
type ToolName = (typeof TOOLS)[number]
const fullName = (t: ToolName) => `mcp__${PLUGIN}__${t}`
const isOurs = (tool: unknown) => typeof tool === 'string' && tool.startsWith(`mcp__${PLUGIN}__`)

const OFF: KeystrokeMode = { on: false, chunk: 1 }
const MAX_BLOCKS_PER_TURN = 2
const VIEW_CHARS = 60

const mode = atom({ plugin: 'keystroke', key: 'mode' } as const, OFF)
const draft = atom({ plugin: 'keystroke', key: 'draft' } as const, '')

// Per-turn bookkeeping. Module variables are fine here: a hot reload mid-turn loses
// them, and the worst case is one unblocked direct-text turn.
let pendingSend: string | null = null
let synthesizedThisTurn = false
let blocksThisTurn = 0
// The keystroke calls of the latest main-loop response, in order. Only the first is
// typed: a response holding a whole message as 50 calls is the message written in one
// go, which is what the mode exists to prevent.
let stepKeystrokes: string[] = []
const ONE_PER_RESPONSE = 'one keystroke per response: this call was not typed. Make one keystroke call, read its result, then the next.'

const chars = (s: string) => [...s]
const count = (s: string) => chars(s).length
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`

export function parseArgs(args: string): KeystrokeMode | 'status' | undefined {
  const a = args.trim().toLowerCase()
  if (a === '' || a === 'status') return 'status'
  if (a === 'off') return OFF
  if (a === 'on') return { on: true, chunk: 1 }
  const m = /^(\d+)$/.exec(a)
  if (!m) return undefined
  const n = Number(m[1])
  return n >= 1 && n <= 10000 ? { on: true, chunk: n } : undefined
}

// What `type` and `backspace` answer: the end of the current line with a cursor, the
// glance a typist takes at the field. The full draft is already in the model's context
// as its own calls.
export function view(d: string): string {
  const line = d.slice(d.lastIndexOf('\n') + 1)
  const cs = chars(line)
  const tail = cs.slice(-VIEW_CHARS).join('')
  return `${cs.length > VIEW_CHARS ? '…' : ''}${tail}▍`
}

export function section(m: KeystrokeMode): string {
  return [
    '# Keystroke mode',
    '',
    'Your replies to the user are composed through three tools instead of written directly:',
    `- \`${fullName('type')}\` appends text to your draft, at most ${plural(m.chunk, 'character')} per call.`,
    `- \`${fullName('backspace')}\` deletes the last characters of the draft.`,
    `- \`${fullName('send')}\` delivers the draft as your reply and ends your turn.`,
    '',
    'One keystroke per response: each call answers with the draft so far before you choose the next one; of several keystroke calls in one response only the first is typed. Text written outside these tools is not a reply: a turn that ends with direct text, or with an unsent draft, is handed back to you to finish through the tools. Other tools work as usual. Only the main conversation composes this way; subagents reply normally.',
  ].join('\n')
}

const storeKey = async ($: EngineInterface) => `mode:${await $.session.id()}`

async function loadMode($: EngineInterface): Promise<KeystrokeMode> {
  const v = (await $.store.get(await storeKey($))) as Partial<KeystrokeMode> | undefined
  return v && typeof v.on === 'boolean' && typeof v.chunk === 'number' ? { on: v.on, chunk: v.chunk } : OFF
}

async function setMode($: EngineInterface, m: KeystrokeMode): Promise<void> {
  await $.store.set(await storeKey($), m)
  await update($, mode, () => m)
  $.ui.status(m.on ? `keystroke ${m.chunk}/call` : undefined)
  // The system prompt section and the tools' deferral read the mode; both are cached.
  $.ui.invalidate('tool.describe')
  $.ui.invalidate('prompt.section')
}

function describe(t: ToolName, m: KeystrokeMode): string {
  switch (t) {
    case 'type':
      return `Append text to your draft reply: at most ${plural(m.chunk, 'character')} per call, one call per response. Answers with the end of the current line and a cursor.`
    case 'backspace':
      return 'Delete the last `count` characters of your draft reply (default 1). Answers with the end of the current line and a cursor.'
    case 'send':
      return 'Deliver your draft as the reply to the user and end your turn.'
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const m = await loadMode($)
    await update($, mode, () => m)
    await $.tool.register({
      name: 'type',
      description: describe('type', m),
      inputSchema: {
        type: 'object',
        properties: { text: { type: 'string', description: 'The characters to append.' } },
        required: ['text'],
      },
      isDeferred: !m.on,
    })
    await $.tool.register({
      name: 'backspace',
      description: describe('backspace', m),
      inputSchema: {
        type: 'object',
        properties: { count: { type: 'integer', minimum: 1, description: 'How many characters to delete (default 1).' } },
      },
      isDeferred: !m.on,
    })
    await $.tool.register({
      name: 'send',
      description: describe('send', m),
      inputSchema: { type: 'object', properties: {} },
      isDeferred: !m.on,
    })
    await $.command.register({
      name: COMMAND,
      description: 'Compose replies through keystroke tools, a few characters per call',
      argumentHint: 'on | off | <characters per call> | status',
    })
    $.ui.status(m.on ? `keystroke ${m.chunk}/call` : undefined)
    return next(e)
  })

  on('command.run', { command: COMMAND }, async ($, e) => {
    const parsed = parseArgs(e.args)
    const usage = `/${COMMAND} on (1 character per call) | <n> (n characters per call) | off | status`
    if (parsed === undefined) return { text: `Could not read "${e.args.trim()}". ${usage}` }
    if (parsed === 'status') {
      const m = await read($, mode)
      return { text: m.on ? `keystroke is on: ${plural(m.chunk, 'character')} per type() call. ${usage}` : `keystroke is off. ${usage}` }
    }
    await setMode($, parsed)
    pendingSend = null
    await update($, draft, () => '')
    return {
      text: parsed.on
        ? `keystroke on: replies are now composed through type (${plural(parsed.chunk, 'character')} per call), backspace and send.`
        : 'keystroke off: replies are written directly again.',
    }
  })

  for (const t of TOOLS) {
    on('tool.describe', { tool: fullName(t) }, async ($, e, next) => {
      const r = await next(e)
      const m = await read($, mode)
      return { ...r, description: describe(t, m), isDeferred: !m.on }
    })
    // Our own tools never need the person's approval.
    on('tool.check', { tool: fullName(t) }, () => ({ decision: 'allow' }))
  }

  on('prompt.compose', async ($, e, next) => {
    const r = await next(e)
    const m = await read($, mode)
    if (!m.on || e.traits.includes('teammate')) return r
    return { sections: [...r.sections, { id: `${PLUGIN}:mode`, text: section(m), scope: 'session' }] }
  })

  const notFirst = (id: string) => stepKeystrokes.length > 1 && stepKeystrokes[0] !== id

  on('tool.call', { tool: fullName('type') }, async ($, e) => {
    if (e.agentId !== undefined) return { deny: 'keystroke tools serve the main conversation only; reply normally.' }
    const m = await read($, mode)
    if (!m.on) return { deny: 'keystroke mode is off; write your reply directly.' }
    if (notFirst(e.tool_use_id)) return { deny: ONE_PER_RESPONSE }
    const text = String((e as { text?: unknown }).text ?? '')
    const n = count(text)
    if (n === 0) return { deny: 'nothing to type: pass text.' }
    if (n > m.chunk) {
      return { deny: `one type() call takes at most ${plural(m.chunk, 'character')}; this one had ${n}. Nothing was typed; resend in smaller pieces.` }
    }
    await update($, draft, d => d + text)
    return { result: view(await read($, draft)) }
  })

  on('tool.call', { tool: fullName('backspace') }, async ($, e) => {
    if (e.agentId !== undefined) return { deny: 'keystroke tools serve the main conversation only; reply normally.' }
    const m = await read($, mode)
    if (!m.on) return { deny: 'keystroke mode is off; write your reply directly.' }
    if (notFirst(e.tool_use_id)) return { deny: ONE_PER_RESPONSE }
    const raw = Number((e as { count?: unknown }).count ?? 1)
    const k = Number.isFinite(raw) && raw >= 1 ? Math.floor(raw) : 1
    await update($, draft, d => chars(d).slice(0, Math.max(0, count(d) - k)).join(''))
    return { result: view(await read($, draft)) }
  })

  on('tool.call', { tool: fullName('send') }, async ($, e) => {
    if (e.agentId !== undefined) return { deny: 'keystroke tools serve the main conversation only; reply normally.' }
    const m = await read($, mode)
    if (!m.on) return { deny: 'keystroke mode is off; write your reply directly.' }
    if (notFirst(e.tool_use_id)) return { deny: ONE_PER_RESPONSE }
    const d = await read($, draft)
    if (d.trim() === '') return { deny: 'the draft is empty; type the reply first.' }
    pendingSend = d
    return { result: `sent (${plural(count(d), 'character')}). Your turn ends here.` }
  })

  on('turn.start', async ($, e, next) => {
    pendingSend = null
    synthesizedThisTurn = false
    blocksThisTurn = 0
    await update($, draft, () => '')
    return next(e)
  })

  // The step after `send`: no model call; the draft becomes the step's text.
  on('turn.step', async function* ($, e, next) {
    if (e.agentId !== undefined) return yield* next(e)
    if (pendingSend === null) {
      stepKeystrokes = []
      const m = await read($, mode)
      if (!m.on) return yield* next(e)
      // Text in a response that also makes a keystroke call is narration ("still
      // typing..."), not a reply: it is held back and dropped. Text in a response with
      // no keystroke call is shown, and the stop hook decides what to do about it.
      const held: TurnStepChunk[] = []
      for await (const c of next(e)) {
        if (c.kind === 'tool' && isOurs(c.name)) stepKeystrokes.push(c.id)
        if (c.kind === 'text') {
          held.push(c)
          continue
        }
        if (c.kind === 'stop' && stepKeystrokes.length === 0) yield* held
        yield c
      }
      return
    }
    const text = pendingSend
    pendingSend = null
    synthesizedThisTurn = true
    await update($, draft, () => '')
    yield { kind: 'text', index: 0, text }
    yield { kind: 'stop', stopReason: 'end_turn', usage: null }
    return { turnId: e.turnId, index: e.index, answer: text, toolUses: [], stopReason: 'end_turn', usage: null }
  })

  on('classic.Stop', async ($, e, next) => {
    const m = await read($, mode)
    if (!m.on || synthesizedThisTurn || blocksThisTurn >= MAX_BLOCKS_PER_TURN) return next(e)
    const d = await read($, draft)
    const direct = (e.last_assistant_message ?? '').trim()
    if (d.trim() !== '') {
      blocksThisTurn += 1
      return { block: `keystroke mode: your draft (${plural(count(d), 'character')}) was not sent. Keep typing with ${fullName('type')} or deliver it with ${fullName('send')}.` }
    }
    if (direct !== '') {
      blocksThisTurn += 1
      return {
        block: `keystroke mode is on: the ${plural(count(direct), 'character')} you wrote directly are not a reply. Compose it with ${fullName('type')} (at most ${plural(m.chunk, 'character')} per call) and finish with ${fullName('send')}.`,
      }
    }
    return next(e)
  })

  // Keystroke rows are noise in the transcript: the draft is drawn in the band instead,
  // and the sent reply arrives as an assistant message.
  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    if (!isOurs(e.props.tool)) return next(e)
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
  on('ui.render', { component: 'ToolResult' }, async ($, e, next) => {
    if (!isOurs(e.props.tool)) return next(e)
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    if (!e.props.calls.every(c => isOurs(c.tool))) return next(e)
    const { Box } = $.ui.resolve(e)
    return <Box />
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const m = await read($, mode)
    const d = await read($, draft)
    if (!m.on || d === '' || e.props.hasSurvey) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="column" borderStyle="round" borderDimColor paddingX={1}>
        <Text dimColor>
          draft · {plural(count(d), 'character')} · {m.chunk}/call
        </Text>
        <Text wrap="wrap">{d}▍</Text>
      </Box>
    )
  })
}
