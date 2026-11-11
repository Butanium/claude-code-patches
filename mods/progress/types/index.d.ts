export type Answer = {
  id: number
  question: string
  status: 'asking' | 'answered' | 'failed'
  text: string
}

declare module 'claude-code' {
  interface PluginState {
    progress: {
      answer: Answer | null
      nextId: number
    }
  }
}
