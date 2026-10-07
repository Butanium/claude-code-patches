import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { parse } from './register'

// The engine beneath the plugin: one session at `world.tokens` of a 200k window,
// recording compactions and status lines.
function mockWorld(on: On, tokens: number) {
  const world = { tokens, attempts: 0, compactions: 0, isFailing: false, status: [] as (string | undefined)[] }
  mock.store(on)
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { tokens: world.tokens, window: 200_000 }, rateLimits: [] } }))
  on('session.compact', () => {
    world.attempts++
    if (world.isFailing) throw new Error('api down')
    world.compactions++
    return { messages: [{ role: 'user', text: 'summary', toolUses: [] }] }
  })
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('ui.status', (_$, e) => {
    world.status.push(e.text)
    return { value: undefined }
  })
  on('ui.log', () => ({ value: undefined }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  return world
}

const START = { cwd: '/x', surface: 'terminal', isInteractive: true } as const
const TURN_START = { text: 'go', turnId: 't1' }
const TURN_END = { answer: 'ok', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' } as const
const cmd = (args: string) => ({ command: 'session-autocompact', args, origin: { kind: 'composer' } }) as never

test('parse', () => {
  expect(parse('')).toBe('status')
  expect(parse('off')).toBe('off')
  expect(parse('on')).toEqual({ percent: 80 })
  expect(parse('70')).toEqual({ percent: 70 })
  expect(parse('70%')).toEqual({ percent: 70 })
  expect(parse('400k')).toEqual({ tokens: 400_000 })
  expect(parse('1.5m')).toEqual({ tokens: 1_500_000 })
  expect(parse('250000')).toEqual({ tokens: 250_000 })
  expect(parse('100%')).toBeUndefined()
  expect(parse('banana')).toBeUndefined()
})

test('off by default: a turn far past any threshold does not compact', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on, 190_000)
  await $.session.start(START)
  await $.turn.start(TURN_START)
  await $.turn.complete(TURN_END)
  await clock.advance(10_000)
  expect(world.compactions).toBe(0)
})

test('on at 80%: compacts after a main turn ends past it, and only then', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on, 100_000)
  await $.session.start(START)
  expect((await $.command.run(cmd('on'))).text).toContain('compacts when a turn ends with context ≥ 80%')

  await $.turn.start(TURN_START)
  await $.turn.complete(TURN_END)
  await clock.advance(10_000)
  expect(world.compactions).toBe(0)

  world.tokens = 170_000
  await $.turn.complete({ ...TURN_END, agentId: 'a1' })
  await clock.advance(10_000)
  expect(world.compactions).toBe(0)

  await $.turn.start(TURN_START)
  await $.turn.complete({ ...TURN_END, isAborted: true, reason: 'aborted' })
  await clock.advance(10_000)
  expect(world.compactions).toBe(0)

  await $.turn.start(TURN_START)
  await $.turn.complete(TURN_END)
  await clock.advance(10_000)
  expect(world.compactions).toBe(1)

  expect((await $.command.run(cmd('off'))).text).toContain('off for this session')
  await $.turn.start(TURN_START)
  await $.turn.complete(TURN_END)
  await clock.advance(10_000)
  expect(world.compactions).toBe(1)
})

test('turning it on past the threshold compacts right away', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on, 150_000)
  await $.session.start(START)
  expect((await $.command.run(cmd('100k'))).text).toContain('Already past it')
  await clock.advance(1_000)
  expect(world.compactions).toBe(1)
})

test('status line follows the setting', async ($, on) => {
  mock.clock(on)
  const world = mockWorld(on, 50_000)
  await $.session.start(START)
  await $.command.run(cmd('400k'))
  expect(world.status.at(-1)).toBe('on (≥400k)')
  await $.command.run(cmd('off'))
  expect(world.status.at(-1)).toBeUndefined()
})

test('a failed compaction is tried once per turn end, not retried', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on, 170_000)
  world.isFailing = true
  await $.session.start(START)
  await $.command.run(cmd('on'))
  await clock.advance(10_000)
  expect(world.attempts).toBe(1)
  await $.turn.start(TURN_START)
  await $.turn.complete(TURN_END)
  await clock.advance(10_000)
  expect(world.attempts).toBe(2)
  expect(world.compactions).toBe(0)
})
