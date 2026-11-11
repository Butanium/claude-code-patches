# progress

`/progress` asks the session "how is it going? anything i should know?" as a side question. It is a shortcut for typing `/btw how is it going? anything i should know?`.

## Use

```
/progress
/progress is the eval done?
```

- It runs at once, also while Claude is working, and does not interrupt the turn.
- The answer appears in a band above the prompt. It stays until you send your next prompt, press `×` in the band, or run `/progress` again.
- `/progress <question>` asks that question instead of the fixed one.

## How it compares to /btw

Like `/btw`, the question is answered by one completion over the session's own transcript, read from the prompt cache, with no tools. The mod puts the same instructions in front of the question that `/btw` does (copied from 2.1.321). Neither the question nor the answer is written to the transcript, so the model never reads them back.

Differences:

- The answer is drawn in the band above the prompt, not in `/btw`'s overlay. Bare `/btw` does not reopen it.
- Earlier `/btw` and `/progress` exchanges are not shown to the side question. `/btw` passes its own earlier exchanges along.
- If you interrupt the turn with Esc while the question is out, the question is cut off too.

A plugin command cannot call `/btw` itself mid-turn: `$.command.run` waits until the session is idle. Hence the separate implementation on `$.model.fork`.

## Install

`mods/` of this repository is a folder marketplace named `claude-code-patches`:

```sh
claude plugin marketplace add /path/to/claude-code-patches/mods
claude plugin install progress@claude-code-patches
```

Or load it for one session with `claude --plugin-dir /path/to/claude-code-patches/mods/progress`.
