#!/usr/bin/env python3
"""CLI patch: silence the auto-mode / bypass-permissions-mode system reminders.

Cycling permission modes (shift+tab) injects a system turn into the model's
context on every transition. The user never sees any of it, and one of the
blocks contradicts the base system prompt outright. Two renderers produce it,
both in the attachment map next to `plan_mode_exit` (which `plan-exit-nag.py`
already gates):

`case"auto_mode"` builds ONE of three strings and is the entry side:

    e.bypass    -> `While bypass permissions mode is active:\n\n${p}`
    e.steerOnly -> `While auto mode is active:\n\n${p}`
    otherwise   -> o + d + (e.bashFirst ? `\n\n${p}` : "")

where `p` is a shared paragraph telling Claude to do its file work with
`cat`/`sed -n`/`grep`/heredocs "rather than using the dedicated Read, Edit, or
Write tools" — the direct opposite of the base system prompt's "Prefer the
dedicated file/search tools over shell commands when one fits", with nothing
saying which wins. `o` is the general auto-mode guidance (bias toward not
stopping for clarifying questions; run `git status` before anything that could
discard uncommitted work) and `d` is the auto-mode-classifier consent flow
(batch held asks, bold the blocking item, "approve? (or 'all of them')").

`auto_mode_exit` is the exit side: "## Exited Auto Mode / You have exited auto
mode." plus, when `bashFirst` is set, " Resume using the dedicated tools for
file reads, searches, and edits."

Both renderers are gated to `return[]` here, which is the shape the sibling
`case"attention_budget":return[];` already uses for "render nothing". The
attachment itself is still produced and still lands in the transcript, so any
backward-scan/throttling logic over these attachments behaves exactly as stock
— same reasoning as `plan-exit-nag.py`: gate the renderer, not the producer.

SCOPE NOTE — this removes the whole `case"auto_mode"` output, so the third
branch (`o`+`d`: the git-safety paragraph and the consent-flow instructions)
goes too, not just the Bash-first nudge. That is deliberate per the request
("remove the auto mode and skip permission mode injections"), but it is the one
judgement call in this patch: to keep real auto mode fully briefed and drop only
the two standing "While ... is active:" nudges, anchor EDIT_ENTRY on
`` `:""); `` -> keep, and instead neutralize just the two ternary branches. Left
as-is because the noise, not the briefing, was what prompted this.

Not touched: `plan_mode`, `plan_mode_reentry`, `plan_mode_exit`. Plan-mode
phantom exits are already handled by `plan-exit-nag.py`.

Byte budget: each edit swaps the return expression `$l([He({content:X,isMeta:!0})])`
(31 B on 2.1.258) for `[]` plus a padded block comment carrying MARKER, so the
edit is same-length with no dead code left behind — nothing to mis-parse.

Idempotency: MARKER is the applied-signal, and each edit is checked and applied
independently, so a partially-patched binary (one edit landed, upstream moved
the other) heals on the next run instead of reporting a false "already applied".

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if an edit can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import apply_patch, candidate_binaries

MARKER = b"[mode-nag-off]"

# Both patterns anchor on the stable English prose of the reminders and on the
# stable `e.bashFirst` / `e.steerOnly` field names, never on minified locals.
# The captured group is the return expression: `$l([He({content:y,isMeta:!0})])`
# on 2.1.258, but every identifier in it is renamed each build, so it is matched
# loosely and thrown away rather than re-emitted.
RETURN_EXPR = rb"([$\w]+\(\[[$\w]+\(\{content:[$\w]+,isMeta:!0\}\)\]\))"

EDITS = (
    (
        "entry",
        re.compile(
            rb"While auto mode is active:\n\n\$\{[$\w]+\}`:[$\w]+\+[$\w]+\+"
            rb"\(e\.bashFirst\?`\n\n\$\{[$\w]+\}`:\"\"\);return " + RETURN_EXPR + rb"\}"
        ),
        "the 'While bypass permissions mode is active:' / 'While auto mode is active:' reminders",
    ),
    (
        "exit",
        re.compile(
            rb"You have exited auto mode\. The user may now want to interact more "
            rb"directly\. You should ask clarifying questions when the approach is "
            rb"ambiguous rather than making assumptions\.\$\{[$\w]+\}`;return "
            + RETURN_EXPR
            + rb"\}"
        ),
        "the '## Exited Auto Mode' reminder",
    ),
)


def build_replacement(m: re.Match[bytes]) -> bytes:
    """Swap the captured return expression for `[]`, padding the freed bytes with
    a block comment that carries MARKER. Spaces go INSIDE the comment so nothing
    lands in a template literal (which would change runtime output)."""
    expr = m.group(1)
    core = b"[]/*" + MARKER + b"*/"
    pad = len(expr) - len(core)
    if pad < 0:
        raise RuntimeError(
            f"replacement ({len(core)} B) longer than the return expression "
            f"({len(expr)} B) — recompute or shorten MARKER"
        )
    rep_expr = b"[]/*" + MARKER + b" " * pad + b"*/"
    assert len(rep_expr) == len(expr)
    out = m.group(0).replace(expr, rep_expr, 1)
    assert len(out) == len(m.group(0))
    return out


def main() -> int:
    cands = candidate_binaries()
    if not cands:
        print("no claude binary found (unknown install layout)", file=sys.stderr)
        return 1
    binp = cands[0]
    data = binp.read_bytes()

    applied, confirmed = [], []
    buf = data
    for name, pattern, what in EDITS:
        matches = list(pattern.finditer(buf))
        if not matches:
            # Already patched? The anchor prose survives the edit; only the
            # return expression changed, so look for MARKER in that slot.
            if re.search(pattern.pattern.replace(RETURN_EXPR, re.escape(b"[]/*") + rb"\[mode-nag-off\] *\*/"), buf):
                confirmed.append(name)
                continue
            print(
                f"mode-nag-off: the {name} pattern is gone from {binp} — upstream "
                f"changed {what}. Re-investigate around the string "
                f"'While auto mode is active:' (entry) / 'You have exited auto mode.' "
                f"(exit) in the attachment renderer map, next to plan_mode_exit.",
                file=sys.stderr,
            )
            return 1
        if len(matches) != 1:
            print(
                f"mode-nag-off: expected exactly 1 occurrence of the {name} pattern, "
                f"found {len(matches)} in {binp} — upstream code changed; refusing to patch",
                file=sys.stderr,
            )
            return 1
        m = matches[0]
        buf = buf[: m.start()] + build_replacement(m) + buf[m.end() :]
        applied.append(name)

    assert len(buf) == len(data)
    if not applied:
        print(
            f"mode-nag-off: confirmed already patched — {', '.join(confirmed)} "
            f"reminder(s) render to nothing ({binp})",
            file=sys.stderr,
        )
        return 0

    def _verify(written: bytes) -> None:
        if len(written) != len(data) or written.count(MARKER) != len(EDITS):
            raise RuntimeError("post-write verification failed — live binary untouched")
        for _name, pattern, _what in EDITS:
            if pattern.search(written) is not None:
                raise RuntimeError("post-write verification failed (stock pattern still present)")

    apply_patch(binp, data, buf, _verify)
    print(
        f"mode-nag-off: applied — {', '.join(applied)} reminder(s) now render to nothing"
        + (f", {', '.join(confirmed)} already off" if confirmed else "")
        + f" ({binp})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
