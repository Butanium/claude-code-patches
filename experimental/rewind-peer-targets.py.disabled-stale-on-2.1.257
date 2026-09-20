#!/usr/bin/env python3
"""CLI patch (EXPERIMENTAL): let /rewind target teammate messages, not just yours.

Scripts in experimental/ are NOT run by run_cli_patches.sh. They are meant to be
applied to a throwaway copy of the bundle built by ../make-expclaude.sh, driven
via $CLAUDE_CLI_PATCH_TARGET, so a half-baked patch can be lived with for a while
before it goes anywhere near the CLI you actually work in.

WHAT /rewind SHOWS TODAY

The checkpoint list is literally `messages.filter(a5e)`:

    function a5e(e){
        if(!eqe(e))return!1;
        if(e.origin&&e.origin.kind!=="human")return!1;             // <- gate B
        if(e.stackedExpansion)return!1;
        return!0
    }
    function eqe(e){
        ...
        if(e.isMeta)return!1;                                      // <- gate A
        ...
        let t=wP(e)?.trim()??"";
        if(<tag checks>
           ||t.startsWith(`<${J6} `)                               // <- gate C
           ||t.startsWith(jEt)&&t.startsWith(`<${J6} `,t.indexOf(`\\n`)+1))return!1;
        return!0
    }

with J6="teammate-message" and jEt="Another Claude session sent a message".

A message from another Claude session takes one of two shapes, and WHICH ONE
depends on the transport, which is the thing that makes this patch bigger than
it looks. Census over 600 recent transcripts on the box it was written for:

    origin.kind  isMeta  inner tag             origin.body   count  sessions
    None         False   <teammate-message>    absent          293       42
    peer         True    <agent-message>       present          15        4

  * In-process teammates (Agent tool with a name) are delivered directly: no
    origin, no isMeta, wrapper preamble + <teammate-message ...>. Only gate C
    rejects these — and they are the overwhelmingly common case.
  * Mailbox/router peers (background subagents, cross-session sends, tmux
    teammates) arrive with isMeta and origin.kind==="peer", wrapper preamble +
    <agent-message ...>. Gates A and B reject these; gate C does not.

So all three gates have to go or the patch only covers one transport. Reading
the predicate suggests A and B are the story; measuring says C is.

WHAT THIS PATCH DOES

1. Blanks gates A, B and C (same-length: a comment marker plus spaces; gate C
   is a trailing run of `||` disjuncts, so blanking it leaves a valid
   condition). Deliberately a superset of "allow teammate messages": every
   user-role message that isn't a tool result, compact summary or hook-tag blob
   becomes a checkpoint, so task notifications and harness-injected meta
   messages become rewind targets too. Allowing only the teammate shapes costs
   bytes there is no local slack to pay, and the extra entries are ones you'd
   plausibly want to rewind past anyway.

2. Makes the rows readable. Each row renders one truncated line of message
   text, and every wrapped message starts with the same preamble, so without
   this the list is N identical "Another Claude session sent a message:" rows.
   Both shapes carry their real text somewhere reachable — `origin.body` for
   router peers, the <teammate-message> element for in-process ones — so:

       let BoD=wP(lNl)?.trim()||"(no prompt)";let $Lt=NXr(BoD);
    -> let $Lt=NXr(lNl.origin?.body||uc(wP(lNl)||"","teammate-message")
                   ||wP(lNl)?.trim()||"(no prompt)");

   `uc` is the bundle's tag extractor; its regex is `<tag(?:\\s+[^>]*)?>` so it
   copes with the element's attributes. It is located by its own body rather
   than by name, since the name is minified.

   The ~42 bytes come from two places, neither of them cosmetic: folding the
   two statements into one (the intermediate variable has no other reader), and
   dropping the React-Compiler memo guard around the "((empty message))"
   placeholder — slots 16/17/18 simply go unused, and the placeholder gets
   rebuilt per render instead of being cached. That is a static text node in a
   branch that is almost never taken. Every label and style survives intact,
   which is why this is preferred over the obvious alternatives (shortening
   "(no prompt)", dropping `italic:!0,`, or dropping the `flexDirection:"row"`
   that Ink's Box already defaults to) — those are the fallback byte sources if
   a future rebuild makes this shape stop matching.

BLAST RADIUS (all call sites checked on 2.1.226)

  a5e -> SXs (userPromptCount): re-checks isMeta and origin itself, unaffected.
  a5e -> the SDK `rewind_conversation` control path, where it decides whether a
         rewind target is stale. Loosening makes remote rewind refuse in more
         cases (a teammate message after the target now counts as "stale").
  eqe -> a transcript filter that has its own `!isMeta` guard, unaffected.
  eqe -> a "does this session have any content" check in the fork-context
         builder, which gets slightly more permissive.

KNOWN WART, DELIBERATELY LEFT

Restoring prefills the prompt box with the rewound message's text ($Ta), so
rewinding to a teammate message dumps the whole wrapper blob into your input.
Clear it with ctrl-u. Fixing it means a third site with its own byte hunt.

Anchors are structural (this code has no stable strings to hang off), so the
patterns are regexes over the shapes with the minified identifiers wildcarded,
and each must match exactly once.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import apply_patch, candidate_binaries

MARKER = b"[Zq4x rewind-peer]"
ROW_SIGNAL = b'"teammate-message")||'

# The tag extractor, located by its body: `uc(text, tag)` -> inner text or null.
RE_UC = re.compile(
    rb"function ([\w$]{1,8})\(e,t\)\{if\(!e\.trim\(\)\|\|!t\.trim\(\)\)return null;"
)

# Gates A and B: one contiguous span across a5e and eqe. Requiring the same \2
# in the call and in the following definition is what makes this shape-match
# trustworthy despite every identifier being minified.
RE_GATES = re.compile(
    rb"function ([\w$]{1,8})\(e\)\{if\(!([\w$]{1,8})\(e\)\)return!1;"
    rb'(if\(e\.origin&&e\.origin\.kind!=="human"\)return!1;)'
    rb"if\(e\.stackedExpansion\)return!1;return!0\}"
    rb'function \2\(e\)\{if\(e\.type!=="user"\)return!1;'
    rb"(.{0,300}?)"
    rb"(if\(e\.isMeta\)return!1;)",
    re.S,
)

# Gate C: the two trailing `||` disjuncts that reject the teammate wrapper,
# either bare or behind the "Another Claude session sent a message" preamble.
# They sit at the end of the condition, so blanking them leaves it well-formed.
RE_GATEC = re.compile(
    rb"(\|\|t\.startsWith\(`<\$\{[\w$]{1,8}\} `\)"
    rb"\|\|t\.startsWith\([\w$]{1,8}\)"
    rb"&&t\.startsWith\(`<\$\{[\w$]{1,8}\} `,t\.indexOf\(`\n`\)\+1\))"
    rb"\)return!1;return!0\}"
)

# The checkpoint-row renderer, from the text-extraction statement through the
# memo epilogue of the empty-message branch (the span whose bytes pay for it).
RE_ROW = re.compile(
    rb'let ([\w$]{1,8})=([\w$]{1,8})\(([\w$]{1,8})\)\?\.trim\(\)\|\|"\(no prompt\)";'
    rb"let ([\w$]{1,8})=([\w$]{1,8})\(\1\);"
    rb"if\(([\w$]{1,8})\(\4\)\)\{let ([\w$]{1,8});"
    rb"if\([\w$]{1,8}\[\d+\]!==[\w$]{1,8}\|\|[\w$]{1,8}\[\d+\]!==[\w$]{1,8}\)"
    rb"(\7=.{20,400}?)"
    rb",[\w$]{1,8}\[\d+\]=[\w$]{1,8},[\w$]{1,8}\[\d+\]=[\w$]{1,8},"
    rb"[\w$]{1,8}\[\d+\]=\7;else \7=[\w$]{1,8}\[\d+\];",
    re.S,
)


def _one(rx: re.Pattern[bytes], data: bytes, what: str) -> re.Match[bytes]:
    hits = list(rx.finditer(data))
    if len(hits) != 1:
        raise RuntimeError(
            f"expected exactly 1 match for the {what} shape, found {len(hits)} — "
            f"upstream code changed. Re-investigate: the rewind checkpoint list is "
            f'`messages.filter(<pred>)`; find it by grepping for "(no prompt)" (row '
            f'renderer) or \'e.origin&&e.origin.kind!=="human"\' (the filter).'
        )
    return hits[0]


def _gates_replacement(m: re.Match[bytes]) -> tuple[int, int, bytes]:
    """Blank gates A and B in one span."""
    origin_gate, ismeta_gate = m.group(3), m.group(5)
    comment = b"/*" + MARKER + b"*/"
    pad = len(origin_gate) - len(comment)
    if pad < 0:
        raise RuntimeError(
            f"gate B ({len(origin_gate)} bytes) too short to hold the "
            f"{len(comment)}-byte marker — shorten MARKER"
        )
    span = m.group(0)
    new = span.replace(origin_gate, comment + b" " * pad, 1)
    assert new.endswith(ismeta_gate)
    new = new[: -len(ismeta_gate)] + b" " * len(ismeta_gate)
    assert len(new) == len(span)
    return m.start(), m.end(), new


def _gatec_replacement(m: re.Match[bytes]) -> tuple[int, int, bytes]:
    """Blank gate C — a trailing run of `||` disjuncts, so spaces keep it valid."""
    disjuncts = m.group(1)
    new = m.group(0).replace(disjuncts, b" " * len(disjuncts), 1)
    assert len(new) == len(m.group(0))
    return m.start(), m.end(), new


def _row_replacement(m: re.Match[bytes], uc: bytes) -> tuple[int, int, bytes]:
    """Show the teammate's own words, paid for by folding statements and dropping
    the memo guard on the empty-message placeholder."""
    _var, extract, msg, lt, strip, isempty, qve, jsx = m.groups()
    # .trim() wraps the whole chain, not just the fallback: `uc` returns the
    # element's inner text with the newline that follows the opening tag, so an
    # untrimmed teammate body renders as a blank row.
    new = (
        b"let " + lt + b"=" + strip + b"((" + msg + b".origin?.body||"
        b"" + uc + b"(" + extract + b"(" + msg + b')||"","teammate-message")||'
        b"" + extract + b"(" + msg + b'))?.trim()||"(no prompt)");'
        b"if(" + isempty + b"(" + lt + b")){let " + qve + b";" + jsx + b";"
    )
    pad = len(m.group(0)) - len(new)
    if pad < 0:
        raise RuntimeError(
            f"row edit is {-pad} bytes over budget. Fallback byte sources in the same "
            f'function, in order of least harm: `flexDirection:"row",` (20 bytes, Ink '
            f"Box already defaults to row), `italic:!0,` (10, only italicises the "
            f'empty-message placeholder), shortening "((empty message))" or '
            f'"(no prompt)"'
        )
    # Padding sits between statements, outside any string literal.
    new += b" " * pad
    assert len(new) == len(m.group(0))
    return m.start(), m.end(), new


def main() -> int:
    try:
        cands = candidate_binaries()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not cands:
        print(
            "no claude binary found (set CLAUDE_CLI_PATCH_TARGET to aim at an "
            "experimental copy)",
            file=sys.stderr,
        )
        return 1
    binp = cands[0]
    data = binp.read_bytes()

    if MARKER in data and ROW_SIGNAL in data:
        print(f"rewind-peer-targets: confirmed already patched ({binp})", file=sys.stderr)
        return 0
    if MARKER in data or ROW_SIGNAL in data:
        print(
            f"{binp} carries only half of this patch (marker={MARKER in data}, "
            f"row={ROW_SIGNAL in data}) — restore from {binp}.orig and re-run",
            file=sys.stderr,
        )
        return 1

    try:
        uc = _one(RE_UC, data, "tag extractor").group(1)
        edits = [
            _gates_replacement(_one(RE_GATES, data, "rewind checkpoint filter")),
            _gatec_replacement(_one(RE_GATEC, data, "teammate-wrapper gate")),
            _row_replacement(_one(RE_ROW, data, "checkpoint row renderer"), uc),
        ]
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    edits.sort(key=lambda t: t[0])
    if any(a[1] > b[0] for a, b in zip(edits, edits[1:])):
        print("edit spans overlap — refusing", file=sys.stderr)
        return 1

    patched, cursor = b"", 0
    for start, end, new in edits:
        patched += data[cursor:start] + new
        cursor = end
    patched += data[cursor:]
    assert len(patched) == len(data)

    def _verify(written: bytes) -> None:
        ok = (
            len(written) == len(data)
            and MARKER in written
            and written.count(ROW_SIGNAL) == 1
            and not RE_GATES.search(written)
            and not RE_GATEC.search(written)
            and not RE_ROW.search(written)
        )
        if not ok:
            raise RuntimeError(
                f"post-write verification failed (len={len(written)} want={len(data)}, "
                f"marker={MARKER in written}, row={written.count(ROW_SIGNAL)}) "
                f"— live binary untouched"
            )

    apply_patch(binp, data, patched, _verify)
    print(
        f"rewind-peer-targets: applied patch to {binp} "
        f"(3 gates blanked, rows unwrapped; pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
