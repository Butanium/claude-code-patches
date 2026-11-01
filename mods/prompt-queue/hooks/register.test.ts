import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { asCommand, pickReleasable, stripRecordArgs, toBlockers } from './register'

// The engine beneath the plugin: records what the plugin submits and runs, and answers
// the agent roster from `world.agents`.
function mockWorld(on: On) {
  const world = {
    prompts: [] as { text: string; asUser?: true }[],
    commands: [] as { command: string; args: string }[],
    agents: [] as { id: string; teammateId?: string; description: string; type: string; status: string }[],
  }
  on('prompt.submit', (_$, e) => {
    world.prompts.push({ text: e.text, ...(e.origin.kind === 'plugin' && e.origin.asUser ? { asUser: true } : {}) })
    return { text: e.text }
  })
  on('command.run', (_$, e) => {
    world.commands.push({ command: e.command, args: e.args })
    return { text: '' }
  })
  on('agent.list', () => ({ value: world.agents }) as never)
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }) as never)
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  on('classic.Stop', () => ({}))
  on('ui.render', ($, e) => { const { Text } = $.ui.resolve(e); return h(Text, {}, 'engine') as never })
  return world
}

const START = { cwd: '/x', surface: 'terminal', isInteractive: true } as const
const TURN_START = { text: 'go', turnId: 't1' }
const TURN_END = { answer: 'ok', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' } as const
const ABORTED = { ...TURN_END, isAborted: true, reason: 'aborted' } as const
const cmd = (command: 'queue' | 'hqueue', args: string) => ({ command, args, origin: { kind: 'composer' } }) as never
const shell = (command: string) => ({ id: command, type: 'shell', status: 'running', description: command, command })

test('pure helpers', () => {
  expect(asCommand('/compact keep the plan')).toEqual({ command: 'compact', args: 'keep the plan' })
  expect(asCommand('/clear')).toEqual({ command: 'clear', args: '' })
  expect(asCommand('fix the /tmp path')).toBeUndefined()
  expect(toBlockers([shell('npm test'), shell('bgwatch b123'), shell('tail -f log'), { type: 'teammate', description: 'w' }]))
    .toEqual([{ type: 'shell', description: 'npm test' }])
  const items = [{ id: 1, text: 'a', gate: 'quiet' as const }, { id: 2, text: 'b', gate: 'idle' as const }]
  expect(pickReleasable(items, [{ type: 'shell', description: 'x' }])?.id).toBe(2)
  expect(pickReleasable(items, [])?.id).toBe(1)
  const record = '<command-name>/hqueue</command-name>\n <command-message>hqueue</command-message>\n <command-args>run the analysis</command-args>\n'
  const stripped = stripRecordArgs([{ type: 'text', text: record }])
  expect(stripped?.[0]?.text).toContain('<command-name>/hqueue</command-name>')
  expect(stripped?.[0]?.text).not.toContain('run the analysis')
  expect(stripRecordArgs([{ type: 'text', text: '<command-name>/compact</command-name><command-args>x</command-args>' }])).toBeUndefined()
})

test('/queue mid-turn waits for the turn to end, then sends one prompt per idle point', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  await $.session.start(START)
  await $.turn.start(TURN_START)
  const ack = await $.command.run(cmd('queue', 'write the summary'))
  expect(ack.text).toContain('Queued #1')
  expect(ack.text).not.toContain('write the summary')
  await $.command.run(cmd('queue', 'then commit'))
  await clock.advance(5_000)
  expect(world.prompts).toEqual([])

  await $.classic.Stop({ stop_hook_active: false, background_tasks: [] })
  await $.turn.complete(TURN_END)
  await clock.advance(1_000)
  expect(world.prompts).toEqual([{ text: 'write the summary', asUser: true }])

  await $.turn.start({ text: 'write the summary', turnId: 't2' })
  await clock.advance(5_000)
  expect(world.prompts).toHaveLength(1)
  await $.turn.complete({ ...TURN_END, turnId: 't2' })
  await clock.advance(1_000)
  expect(world.prompts.map(p => p.text)).toEqual(['write the summary', 'then commit'])
})

test('a subagent turn ending does not release', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  await $.session.start(START)
  await $.turn.start(TURN_START)
  await $.command.run(cmd('queue', 'later'))
  await $.turn.complete({ ...TURN_END, agentId: 'a1' })
  await clock.advance(1_000)
  expect(world.prompts).toEqual([])
})

test('/queue while idle sends right away', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  await $.session.start(START)
  await $.command.run(cmd('queue', 'now please'))
  await clock.advance(1_000)
  expect(world.prompts.map(p => p.text)).toEqual(['now please'])
})

test('an interrupted turn pauses the queue until a turn ends on its own', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  await $.session.start(START)
  await $.turn.start(TURN_START)
  await $.command.run(cmd('queue', 'held'))
  await $.turn.complete(ABORTED)
  await clock.advance(10_000)
  expect(world.prompts).toEqual([])

  await $.turn.start({ text: 'typed after esc', turnId: 't2' })
  await $.turn.complete({ ...TURN_END, turnId: 't2' })
  await clock.advance(1_000)
  expect(world.prompts.map(p => p.text)).toEqual(['held'])
})

test('/hqueue waits for background shells but not watcher shells or idle teammates', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  world.agents = [{ id: 'w', teammateId: 'w@team', description: 'worker', type: 'teammate', status: 'idle' }]
  await $.session.start(START)
  await $.turn.start(TURN_START)
  await $.command.run(cmd('hqueue', 'report when the tests are done'))
  await $.classic.Stop({ stop_hook_active: false, background_tasks: [shell('npm test'), shell('bgwatch b1')] })
  await $.turn.complete(TURN_END)
  await clock.advance(10_000)
  expect(world.prompts).toEqual([])

  // The shell finishing notifies: a turn runs, its Stop lists only the watcher.
  await $.turn.start({ text: '', turnId: 't2' })
  await $.classic.Stop({ stop_hook_active: false, background_tasks: [shell('bgwatch b1')] })
  await $.turn.complete({ ...TURN_END, turnId: 't2' })
  await clock.advance(1_000)
  expect(world.prompts.map(p => p.text)).toEqual(['report when the tests are done'])
})

test('/hqueue waits for a running teammate, found by polling while idle', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  world.agents = [{ id: 'w', teammateId: 'w@team', description: 'worker', type: 'teammate', status: 'running' }]
  await $.session.start(START)
  await $.turn.start(TURN_START)
  await $.command.run(cmd('hqueue', 'after the worker'))
  await $.classic.Stop({ stop_hook_active: false, background_tasks: [] })
  await $.turn.complete(TURN_END)
  await clock.advance(10_000)
  expect(world.prompts).toEqual([])

  world.agents = [{ ...world.agents[0]!, status: 'idle' }]
  await clock.advance(6_000)
  expect(world.prompts.map(p => p.text)).toEqual(['after the worker'])
})

test('a queued slash command runs as a command', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  await $.session.start(START)
  await $.command.run(cmd('queue', '/compact keep the plan'))
  await clock.advance(1_000)
  expect(world.prompts).toEqual([])
  expect(world.commands).toContainEqual({ command: 'compact', args: 'keep the plan' })
})

test('the band lists items and its buttons cancel and send', async ($, on) => {
  const clock = mock.clock(on)
  const world = mockWorld(on)
  await $.session.start(START)
  await $.turn.start(TURN_START)
  await $.command.run(cmd('queue', 'first prompt'))
  await $.command.run(cmd('hqueue', 'second prompt'))
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({
      plugin: 'prompt-queue',
      surface,
      component: 'AbovePrompt',
      props: { hasSurvey: false, isWorking: true, maxRows: 20, bodyColumns: 100, scroll: { offset: 0, bodyRows: 19 } } as never,
    })
    expect(await ui.find({ text: /2 queued prompts/ })).toBeDefined()
    expect(await ui.find({ text: 'first prompt' })).toBeDefined()
    expect(await ui.find({ text: /waiting for this turn to end/ })).toBeDefined()
    await ui.unmount()
  }
  const ui = await $.ui.mount({
    plugin: 'prompt-queue',
    surface: 'terminal',
    component: 'AbovePrompt',
    props: { hasSurvey: false, isWorking: true, maxRows: 20, bodyColumns: 100, scroll: { offset: 0, bodyRows: 19 } } as never,
  })
  await ui.press({ key: 'drop-1' })
  expect(await ui.find({ text: 'first prompt' })).toBeUndefined()
  await ui.press({ key: 'send-2' })
  await clock.advance(1_000)
  expect(world.prompts.map(p => p.text)).toEqual(['second prompt'])
  expect(await ui.find({ text: /queued prompt/ })).toBeUndefined()
})
