# keystroke

A Claude Code mod (function hooks plugin) for one experiment: does Claude write
differently when each reply is composed a few characters at a time?

With the mode on, the model does not write its reply as text. It calls three tools:

| tool | what it does |
|---|---|
| `mcp__keystroke__type` | appends text to the draft, at most N characters per call (N = 1 by default), one call per response |
| `mcp__keystroke__backspace` | deletes the last `count` characters |
| `mcp__keystroke__send` | delivers the draft as the reply and ends the turn |

To the person the session looks almost unchanged: the keystroke tool rows are
hidden, the draft is drawn live in the band above the prompt with a cursor, and the
sent reply arrives as an ordinary assistant message. Under the hood that message
was never produced by a model call: after `send`, the mod answers the next model
step itself, yielding the draft as the step's text with `end_turn`, so the transcript
holds a normal assistant text row and the next request carries a valid
`tool_use → tool_result → assistant text` sequence.

## Use

```
/keystroke on        # 1 character per type() call
/keystroke 5         # 5 characters per call
/keystroke off
/keystroke status
```

The setting is kept per session id in the plugin store, so `claude -r` keeps it.
The status line shows `keystroke N/call` while it is on.

## What the mod enforces

- **Chunk size.** A `type` call longer than N characters is refused and nothing is typed.
- **One keystroke per response.** Models batch: Sonnet 5.5 put a whole 56-character
  greeting into one response as 56 `type` calls, which is the message written in one go.
  Of several keystroke calls in one response only the first is typed; the rest are refused
  with a reason. After one refused batch the model goes one call per response.
- **No direct text.** A `classic.Stop` hook blocks a turn that ends with direct text or
  an unsent draft, at most twice per turn, so a session where the tools are unavailable
  still gets an answer on the third try.
- **No narration.** Text in a response that also makes a keystroke call ("still typing,
  two letters left") is held back and dropped from the record. Text in a response that
  makes no keystroke call is shown, and the stop hook decides what to do with it.
- **Main conversation only.** The tools refuse calls from subagent loops, and the
  system prompt section says so.

## How it is built

One hooks module, `hooks/register.tsx`:

- `session.start` registers the tools (`isDeferred` follows the mode) and the command,
  and loads the mode from `$.store`.
- `prompt.compose` appends a `keystroke:mode` section to the system prompt while on.
- `tool.call` serves the three tools from `$.state` (`keystroke.draft`); `tool.check`
  allows them without a prompt.
- `turn.step` (streaming) records the response's keystroke calls for the
  one-per-response rule, drops narration, and, on the step after `send`, answers without
  a model call.
- `ui.render` draws an empty `Box` for keystroke `ToolUse`/`ToolResult`/`ToolGroup` rows
  and the draft in `AbovePrompt`.

## Cost and speed

Every character is one model request against the session's cached prefix plus two
transcript rows (about 60 tokens). Measured with Sonnet 5.5 at medium effort on 2.1.293,
thinking between keystrokes: about 3 s per character (17 characters in 58 s, 41 in two
minutes), so a 50-character reply takes two to three minutes and adds roughly 3k tokens
to the context. That is the experiment, not a flaw, but it rules out chunk 1 for long
replies; `/keystroke 5` or more is the usable setting for anything beyond a sentence.

## Tests

`claude plugin test <this folder>` runs `hooks/register.test.ts` (parsing, the draft
view, type/backspace/send, the chunk rule, the stop hook, the synthesized step, the
narration rule). The one-per-response rule depends on tool_use ids the test kit
assigns itself, so it is covered by an interactive probe instead.
