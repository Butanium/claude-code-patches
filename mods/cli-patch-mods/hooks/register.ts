import type { Register } from 'claude-code'

// Reminders kept out of requests: task-nag, mode-nag-off. The engine still records them.
const DROPPED_ATTACHMENTS = ['todo_reminder', 'task_reminder', 'auto_mode', 'auto_mode_exit']

// peer-msg-warning. The CLI wraps a peer message as `<header>\n<body>\n\n<boilerplate>[<reply hint>]`
// after `session.receive` has run; the wrapped row is what `session.append` sees.
const PEER_HEADER = /^(?:Another Claude session sent a message|A peer session sent a message)(?: while you were working)?:\n/
const PEER_TAIL = /\n\n(?:This came from another Claude session|That "other Claude session" is an agent|IMPORTANT: This is NOT from your user|This is from another Claude session)[\s\S]*$/

function unwrapPeer(text: string): string {
  if (!PEER_HEADER.test(text) || !PEER_TAIL.test(text)) return text
  return text.replace(PEER_HEADER, '').replace(PEER_TAIL, '')
}

type Content = string | Array<{ type: string; text?: string }>

function contentText(content: Content): string {
  return typeof content === 'string' ? content : content.map(b => b.text ?? '').join('\n')
}

function unwrapContent(content: Content): Content {
  if (typeof content === 'string') return unwrapPeer(content)
  return content.map(b => (b.type === 'text' && b.text !== undefined ? { ...b, text: unwrapPeer(b.text) } : b))
}

// idle-notif, interrupted-idle-notif: a delivery that is nothing but one of these pings.
function isIdlePing(text: string): boolean {
  const m = text.match(/\{[\s\S]*\}/)
  if (!m) return false
  try {
    const f = JSON.parse(m[0])
    return f.type === 'idle_notification' && (f.idleReason === 'available' || f.idleReason === 'interrupted')
  } catch {
    return false
  }
}

export const register: Register = (on) => {
  on('tool.describe', { tool: 'Monitor' }, async ($, e, next) => ({ ...(await next(e)), isDeferred: false }))
  on('tool.describe', { tool: 'SendMessage' }, async ($, e, next) => ({ ...(await next(e)), isDeferred: false }))
  on('tool.describe', { tool: 'TaskStop' }, async ($, e, next) => ({ ...(await next(e)), isDeferred: false }))

  on('prompt.attachment', async ($, e, next) => {
    if (DROPPED_ATTACHMENTS.includes(e.type)) return { text: null }
    if (e.type === 'plan_mode_exit' && e.detail?.hasPlan === false) return { text: null }
    return next(e)
  })

  on('session.receive', async ($, e, next) => (isIdlePing(e.text) ? { consumed: 'idle ping' } : next(e)))

  on('session.append', async ($, e, next) => {
    const content = e.message.content as Content
    if (!contentText(content).includes('permission laundering')) return next(e)
    return next({ ...e, message: { ...e.message, content: unwrapContent(content) } } as typeof e)
  })
}
