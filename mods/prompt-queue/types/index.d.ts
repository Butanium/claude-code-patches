export type Gate = 'idle' | 'quiet'
export type QueueItem = { id: number; text: string; gate: Gate }
export type Blocker = { type: string; description: string }

declare module 'claude-code' {
  interface PluginState {
    'prompt-queue': {
      items: QueueItem[]
      nextId: number
      isRunning: boolean
      isPaused: boolean
      blockers: Blocker[]
      teammates: Blocker[]
    }
  }
}
