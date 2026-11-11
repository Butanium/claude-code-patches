import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { DEFAULT_QUESTION, questionFor } from './register'

type Fork = { isAnswered: true; text: string } | { isAnswered: false; reason: string; status?: number; error?: string }

// The engine beneath the plugin: records each fork's prompt and answers it when the test
// says so, so a test can look at the band while the question is still out.
function mockWorld(on: On) {
  const world = {
    forks: [] as string[],
    settle: [] as ((r: Fork) => void)[],
    submitted: [] as string[],
  }
  on('model.fork', (_$, e) => {
    world.forks.push(e.prompt)
    return new Promise(resolve => world.settle.push(r => resolve({ value: r } as never))) as never
  })
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('command.run', () => ({ text: 'engine' }))
  on('prompt.submit', (_$, e) => {
    world.submitted.push(e.text)
    return { text: e.text }
  })
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('ui.render', ($, e) => { const { Text } = $.ui.resolve(e); return h(Text, {}, 'engine') as never })
  return world
}

const START = { cwd: '/x', surface: 'terminal', isInteractive: true } as const
const progress = (args = '') => ({ command: 'progress', args, origin: { kind: 'composer' } }) as never
const typed = (text: string) => ({ text, origin: { kind: 'composer' } }) as never
const BAND = {
  plugin: 'progress',
  surface: 'terminal',
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: true, maxRows: 20, bodyColumns: 100, scroll: { offset: 0, bodyRows: 19 } } as never,
} as const

test('the question defaults to the fixed one', () => {
  expect(questionFor('')).toBe(DEFAULT_QUESTION)
  expect(questionFor('   ')).toBe(DEFAULT_QUESTION)
  expect(questionFor(' is the eval done? ')).toBe('is the eval done?')
})

test('/progress forks the side question and shows the answer in the band, not as command output', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  await $.session.start(START)
  const ack = await $.command.run(progress())
  expect(ack.text).toBeUndefined()
  expect(world.forks).toHaveLength(1)
  expect(world.forks[0]).toContain('This is a side question from the user')
  expect(world.forks[0]!.endsWith(DEFAULT_QUESTION)).toBe(true)

  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ text: /asking/ })).toBeDefined()
  world.settle[0]!({ isAnswered: true, text: 'Tests pass; **nothing** to flag.' })
  await clock.advance(1)
  expect(await ui.find({ text: /asking/ })).toBeUndefined()
  expect(await ui.find({ type: 'Markdown' } as never)).toBeDefined()
  await ui.press({ key: 'close' })
  expect(await ui.find({ text: /\/progress/ })).toBeUndefined()
})

test('an answered band clears at the next prompt; one still being asked stays', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  await $.session.start(START)
  await $.command.run(progress('is the eval done?'))
  expect(world.forks[0]!.endsWith('is the eval done?')).toBe(true)
  const ui = await $.ui.mount(BAND)

  await $.prompt.submit(typed('carry on'))
  expect(world.submitted).toEqual(['carry on'])
  expect(await ui.find({ text: /asking/ })).toBeDefined()

  world.settle[0]!({ isAnswered: true, text: 'Not yet.' })
  await clock.advance(1)
  await $.prompt.submit(typed('ok'))
  expect(await ui.find({ text: /\/progress/ })).toBeUndefined()
})

test('a newer /progress owns the band; a late older answer is dropped', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  await $.session.start(START)
  await $.command.run(progress('first'))
  await $.command.run(progress('second'))
  const ui = await $.ui.mount(BAND)
  world.settle[0]!({ isAnswered: true, text: 'stale' })
  await clock.advance(1)
  expect(await ui.find({ text: /asking/ })).toBeDefined()
  expect(await ui.find({ text: 'second' })).toBeDefined()
})

test('a failed fork says why', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  await $.session.start(START)
  await $.command.run(progress())
  const ui = await $.ui.mount(BAND)
  world.settle[0]!({ isAnswered: false, reason: 'nothing-to-fork' })
  await clock.advance(1)
  expect(await ui.find({ text: /Nothing to ask about yet/ })).toBeDefined()
})
