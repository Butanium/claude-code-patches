import { expect, mock, test } from 'claude-code/testing'
import type { On, TurnStepChunk } from 'claude-code'

import { parseArgs, section, view } from './register'

const TYPE = 'mcp__keystroke__type'
const BACKSPACE = 'mcp__keystroke__backspace'
const SEND = 'mcp__keystroke__send'

// The engine beneath the plugin: a session with a store, no real model, and a model
// step that answers whatever `world.response` holds.
function mockWorld(on: On) {
  const world = { status: [] as (string | undefined)[], logs: [] as string[], response: [] as TurnStepChunk[] }
  mock.store(on)
  on('session.id', () => ({ value: 'sess-1' }))
  on('tool.register', (_$, e) => ({ value: { tool: `mcp__keystroke__${e.name}` } }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('ui.status', (_$, e) => {
    world.status.push(e.text)
    return { value: undefined }
  })
  on('ui.log', (_$, e) => {
    world.logs.push(e.text)
    return { value: undefined }
  })
  on('ui.invalidate', () => ({ value: undefined }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.step', async function* (_$, e) {
    for (const c of world.response) yield c
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  on('classic.Stop', () => ({}))
  return world
}

const START = { cwd: '/x', surface: 'terminal', isInteractive: true } as const
const STEP = { turnId: 't1', index: 0, model: 'm', messageCount: 1 } as const
const cmd = (args: string) => ({ command: 'keystroke', args, origin: { kind: 'composer' } }) as never
const call = (tool: string, input: Record<string, unknown> = {}) => ({ tool, ...input }) as never

async function drain(stream: AsyncGenerator<TurnStepChunk, unknown>) {
  const chunks: TurnStepChunk[] = []
  for (;;) {
    const r = await stream.next()
    if (r.done) return { chunks, result: r.value as { answer: string; stopReason: string | null } }
    chunks.push(r.value)
  }
}

test('parseArgs', () => {
  expect(parseArgs('')).toBe('status')
  expect(parseArgs('status')).toBe('status')
  expect(parseArgs('off')).toEqual({ on: false, chunk: 1 })
  expect(parseArgs('on')).toEqual({ on: true, chunk: 1 })
  expect(parseArgs('5')).toEqual({ on: true, chunk: 5 })
  expect(parseArgs('0')).toBeUndefined()
  expect(parseArgs('word')).toBeUndefined()
})

test('view: the current line tail with a cursor', () => {
  expect(view('')).toBe('▍')
  expect(view('hi')).toBe('hi▍')
  expect(view('line one\nline two')).toBe('line two▍')
  expect(view('x'.repeat(70))).toBe(`…${'x'.repeat(60)}▍`)
})

test('section names the tools and the chunk', () => {
  expect(section({ on: true, chunk: 3 })).toContain('at most 3 characters per call')
  expect(section({ on: true, chunk: 1 })).toContain(TYPE)
})

test('off by default: the tools refuse and the band stays empty', async ($, on) => {
  const world = mockWorld(on)
  await $.session.start(START)
  expect(world.status).toEqual([undefined])
  const r = await $.tool.call(call(TYPE, { text: 'h' }))
  expect(r.deny).toContain('off')
})

test('on: type appends within the chunk, backspace deletes, send ends the next step with the draft', async ($, on) => {
  const world = mockWorld(on)
  await $.session.start(START)
  expect((await $.command.run(cmd('on'))).text).toContain('1 character per call')
  await $.turn.start({ text: 'go', turnId: 't1' })

  expect((await $.tool.call(call(TYPE, { text: 'h' }))).result).toBe('h▍')
  expect((await $.tool.call(call(TYPE, { text: 'i' }))).result).toBe('hi▍')
  expect((await $.tool.call(call(TYPE, { text: 'ab' }))).deny).toContain('at most 1 character')
  expect((await $.tool.call(call(TYPE, { text: '' }))).deny).toContain('nothing to type')
  expect((await $.tool.call(call(BACKSPACE, {}))).result).toBe('h▍')
  expect((await $.tool.call(call(TYPE, { text: 'é' }))).result).toBe('hé▍')
  expect((await $.tool.call(call(SEND))).result).toContain('sent (2 characters)')

  const { chunks, result } = await drain($.turn.step(STEP))
  expect(chunks.map(c => c.kind)).toEqual(['text', 'stop'])
  expect(chunks[0]).toEqual({ kind: 'text', index: 0, text: 'hé' })
  expect(result.answer).toBe('hé')
  expect(result.stopReason).toBe('end_turn')

  // The draft is gone and the stop hook lets the turn end.
  expect((await $.tool.call(call(SEND))).deny).toContain('empty')
  expect(await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'hé' })).toEqual({})
})

test('chunk of 4: longer pieces pass, the deny names the size', async ($, on) => {
  mockWorld(on)
  await $.session.start(START)
  await $.command.run(cmd('4'))
  await $.turn.start({ text: 'go', turnId: 't1' })
  expect((await $.tool.call(call(TYPE, { text: 'abcd' }))).result).toBe('abcd▍')
  expect((await $.tool.call(call(TYPE, { text: 'efghi' }))).deny).toContain('at most 4 characters')
})

test('stop hook: direct text and unsent drafts are blocked, twice at most per turn', async ($, on) => {
  mockWorld(on)
  await $.session.start(START)
  await $.command.run(cmd('on'))
  await $.turn.start({ text: 'go', turnId: 't1' })

  const direct = await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'Four' })
  expect(direct.block).toContain('4 characters you wrote directly')

  await $.tool.call(call(TYPE, { text: 'F' }))
  const unsent = await $.classic.Stop({ stop_hook_active: true, last_assistant_message: '' })
  expect(unsent.block).toContain('was not sent')

  expect(await $.classic.Stop({ stop_hook_active: true, last_assistant_message: 'Four' })).toEqual({})

  // A new turn resets the count and clears the draft.
  await $.turn.start({ text: 'again', turnId: 't2' })
  expect((await $.tool.call(call(SEND))).deny).toContain('empty')
  expect((await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'x' })).block).toBeDefined()
})

test('a step the model answers passes through; narration beside a keystroke is dropped', async ($, on) => {
  const world = mockWorld(on)
  await $.session.start(START)
  await $.command.run(cmd('on'))
  await $.turn.start({ text: 'go', turnId: 't1' })

  world.response = [
    { kind: 'text', index: 0, text: 'still typing' },
    { kind: 'tool', index: 1, id: 'toolu_1', name: TYPE },
    { kind: 'input', index: 1, json: '{"text":"h"}' },
    { kind: 'stop', stopReason: 'tool_use', usage: null },
  ]
  const typed = await drain($.turn.step(STEP))
  expect(typed.chunks.map(c => c.kind)).toEqual(['tool', 'input', 'stop'])

  world.response = [
    { kind: 'text', index: 0, text: 'a direct answer' },
    { kind: 'stop', stopReason: 'end_turn', usage: null },
  ]
  const direct = await drain($.turn.step({ ...STEP, index: 1 }))
  expect(direct.chunks.map(c => c.kind)).toEqual(['text', 'stop'])

  world.response = [
    { kind: 'text', index: 0, text: 'let me look' },
    { kind: 'tool', index: 1, id: 'toolu_2', name: 'Read' },
    { kind: 'input', index: 1, json: '{"file_path":"/x"}' },
    { kind: 'stop', stopReason: 'tool_use', usage: null },
  ]
  const other = await drain($.turn.step({ ...STEP, index: 2 }))
  expect(other.chunks.map(c => c.kind)).toEqual(['tool', 'input', 'text', 'stop'])
})

test('off again: the prompt section goes and the tools refuse', async ($, on) => {
  mockWorld(on)
  await $.session.start(START)
  await $.command.run(cmd('on'))
  expect((await $.command.run(cmd('off'))).text).toContain('written directly again')
  expect((await $.tool.call(call(TYPE, { text: 'h' }))).deny).toContain('off')
})
