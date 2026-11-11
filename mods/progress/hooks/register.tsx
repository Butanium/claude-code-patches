import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Answer } from '../types'

// `/progress` asks the running session a fixed side question, as typing
// `/btw how is it going? anything i should know?` would: one tool-less completion over
// the session's own transcript (`$.model.fork`, served from the main thread's prompt
// cache), while the turn keeps running. The answer is drawn in a band above the prompt
// and is not written to the transcript, so the model never reads it back.

export const DEFAULT_QUESTION = 'how is it going? anything i should know?'

// The framing /btw itself puts in front of the question (copied from 2.1.321), so the
// fork answers as a side question and does not try to continue the main turn's work.
const SIDE_QUESTION = `<system-reminder>This is a side question from the user. You must answer this question directly in a single response.

IMPORTANT CONTEXT:
- You are a separate, lightweight agent spawned to answer this one question
- The main agent is NOT interrupted - it continues working independently in the background
- You share the conversation context but are a completely separate instance
- Do NOT reference being interrupted or what you were "previously doing" - that framing is incorrect

CRITICAL CONSTRAINTS:
- You have NO tools available - you cannot read files, run commands, search, or take any actions
- Do NOT write tool calls or tool output as text (for example invoke or function_calls XML blocks) - nothing you write here is executed; if answering would need reading files, running commands, or searching, say that can't be checked from a side question and suggest asking in the main conversation
- This is a one-off response - there will be no follow-up turns
- You can ONLY provide information based on what you already know from the conversation context
- NEVER say things like "Let me try...", "I'll now...", "Let me check...", or promise to take any action
- If you don't know the answer, say so - do not offer to look it up or investigate

Simply answer the question with the information you have.</system-reminder>

`

const FAILURES: Record<string, string> = {
  'nothing-to-fork': 'Nothing to ask about yet: this session has not sent a request.',
  'empty-reply': 'The side question came back without text.',
  aborted: 'The side question was cut off because the turn was interrupted.',
}

const answer = atom({ plugin: 'progress', key: 'answer' } as const, null)
const nextId = atom({ plugin: 'progress', key: 'nextId' } as const, 1)

export const questionFor = (args: string): string => args.trim() || DEFAULT_QUESTION

// Not awaited by the command's hook: the band shows "asking" at once and the reply
// fills it in when the fork returns.
async function ask($: EngineInterface, id: number, question: string): Promise<void> {
  let settled: Pick<Answer, 'status' | 'text'>
  try {
    const r = await $.model.fork({ prompt: SIDE_QUESTION + question })
    if (r.isAnswered) settled = { status: 'answered', text: r.text.trim() }
    else if (r.reason === 'api-error') settled = { status: 'failed', text: `The side question failed: API error ${r.status ?? ''} ${r.error ?? ''}`.trim() }
    else settled = { status: 'failed', text: FAILURES[r.reason] ?? `The side question failed: ${r.reason}` }
  } catch (err) {
    settled = { status: 'failed', text: `The side question failed: ${err instanceof Error ? err.message : String(err)}` }
  }
  // a newer /progress, or a dismissal, owns the band by now
  await update($, answer, cur => (cur?.id === id ? { ...cur, ...settled } : cur))
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'progress',
      description: `Side question, like /btw: "${DEFAULT_QUESTION}"`,
      argumentHint: '[other question]',
      immediate: true,
    })
    return next(e)
  })

  on('command.run', { command: 'progress' }, async ($, e) => {
    const id = await read($, nextId)
    await update($, nextId, n => n + 1)
    const question = questionFor(e.args)
    await update($, answer, () => ({ id, question, status: 'asking', text: '' }))
    void ask($, id, question)
    return {}
  })

  // The answer stays until the person sends their next prompt, as /btw's overlay stays
  // until a key. One still being asked is kept.
  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind === 'composer') {
      // never the reason a prompt does not go through
      await update($, answer, cur => (cur !== null && cur.status !== 'asking' ? null : cur)).catch(() => undefined)
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const a = await read($, answer)
    if (e.props.hasSurvey || a === null) return next(e)
    const { Box, Button, Markdown, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={1}>
          <Text bold color="claude">
            ◆ /progress
          </Text>
          <Box flexGrow={1} flexShrink={1}>
            <Text dimColor wrap="truncate-end">
              {a.question}
            </Text>
          </Box>
          <Button key="close" label="×" hotkey="x" dimColor onPress={async () => void (await update($, answer, () => null))} />
        </Box>
        {a.status === 'asking' && <Text dimColor>asking…</Text>}
        {a.status === 'failed' && <Text color="warning">{a.text}</Text>}
        {a.status === 'answered' && <Markdown key="answer" text={a.text} />}
      </Box>
    )
  })
}
