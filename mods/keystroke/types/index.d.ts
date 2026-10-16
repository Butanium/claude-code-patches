export type KeystrokeMode = { on: boolean; chunk: number }

declare module 'claude-code' {
  interface PluginState {
    keystroke: { mode: KeystrokeMode; draft: string }
  }
}
