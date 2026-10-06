#!/usr/bin/env python3
"""CLI patch: honor `showThinkingSummaries: true` in non-interactive sessions too.

The `showThinkingSummaries` setting ("Request API-side thinking summaries and
show them in the conversation and in the transcript view") picks the
`thinking.display` value every request carries. Stock, the resolver only
consults it for an interactive session:

    function sYr({explicitDisplay:e,isNonInteractive:n,outputFormat:r,verbose:s}){
      if(e)return e;                                   // --thinking-display <mode> wins
      if(!n)return qEn()?"summarized":void 0;          // interactive: the setting decides
      if(iYr({isNonInteractive:n,outputFormat:r,verbose:s}))return"omitted";
      return}                                          // -p: text / json -> "omitted", else unset
    function qEn(){return lt().showThinkingSummaries??!1}
    function iYr({isNonInteractive:e,outputFormat:n,verbose:r}){return e&&(n==="text"||n==="json"&&!r)}

So a `claude -p` run, or any `--input-format stream-json` driver such as the
Claude Desktop app's bundled CLI, never sends `display:"summarized"` from the
setting: text/json output sends `"omitted"`, stream-json sends no display at
all, and the transcript's thinking blocks come back empty. The `--thinking-display`
flag does make it work (`explicitDisplay` is returned first), but a launcher
you don't control can't be given a flag.

Patched, the setting wins before the interactive check:

    if(e)return e;if(qEn())return"summarized";if(!n)return;if(iYr(...))return"omitted"     }

Every path with the setting off or unset is unchanged: interactive still
returns `undefined`, non-interactive still returns `"omitted"` or `undefined`
from `iYr`. The bytes come from the trailing `;return}` (a function falls off
its end as `undefined` anyway) and are padded with spaces before the `}`,
outside any string literal.

Two callers: session start (sets `thinking.display` on the main loop's config
from the real `isNonInteractive`) and the interactive REPL's per-query config
builder, which passes `isNonInteractive:!1` and so behaves as before. Not
covered on purpose: subagents. In a non-interactive session the Agent tool's config
builder forces `display:"omitted"` on every subagent unless the session's
display was set explicitly by the flag, and it does so regardless of the main
loop's display, so subagent thinking is empty with or without this patch.

Anchored on the destructured parameter names (`explicitDisplay`,
`isNonInteractive`, `outputFormat`, `verbose`) and the `"summarized"` /
`"omitted"` literals, which survive rebuilds; the function and its two callees
are minified identifiers captured by regex. Same code in 2.1.288 (Desktop's
`remote/ccd-cli` copy) and 2.1.289.

Idempotency: the patched sequence `if(<id>())return"summarized";if(!<id>)return;`
in front of the `{isNonInteractive:` call is unique and doubles as the marker.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries

ANCHOR = b'{explicitDisplay:'
# Groups: 1-4 the destructured parameter names, 5 the settings getter, 6 the
# non-interactive-omit helper. Back-references pin the body to the same names.
STOCK_RX = re.compile(
    rb"\{explicitDisplay:(" + JSID + rb"),isNonInteractive:(" + JSID + rb"),"
    rb"outputFormat:(" + JSID + rb"),verbose:(" + JSID + rb")\}\)\{"
    rb"if\(\1\)return \1;"
    rb"if\(!\2\)return (" + JSID + rb")\(\)\?\"summarized\":void 0;"
    rb"if\((" + JSID + rb")\(\{isNonInteractive:\2,outputFormat:\3,verbose:\4\}\)\)return\"omitted\";"
    rb"return\}"
)
PATCHED_RX = re.compile(
    rb"if\(" + JSID + rb"\(\)\)return\"summarized\";if\(!" + JSID + rb"\)return;"
    rb"if\(" + JSID + rb"\(\{isNonInteractive:"
)


def build_replacement(m: re.Match) -> bytes:
    e, n, r, s, setting, omit = m.groups()
    core = (
        b"{explicitDisplay:" + e + b",isNonInteractive:" + n + b",outputFormat:" + r
        + b",verbose:" + s + b"}){"
        + b"if(" + e + b")return " + e + b";"
        + b"if(" + setting + b"())return\"summarized\";"
        + b"if(!" + n + b")return;"
        + b"if(" + omit + b"({isNonInteractive:" + n + b",outputFormat:" + r + b",verbose:" + s
        + b"}))return\"omitted\""
    )
    pad = len(m.group(0)) - len(core) - 1  # -1 for the closing brace
    if pad < 0:
        raise RuntimeError(f"replacement longer than the stock function by {-pad} bytes — recompute")
    replacement = core + b" " * pad + b"}"
    assert len(replacement) == len(m.group(0)), (len(replacement), len(m.group(0)))
    return replacement


def main() -> int:
    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if PATCHED_RX.search(data):
            print(f"thinking-summaries-print: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        if STOCK_RX.search(data):
            target = (binp, data)
            break

    if target is None:
        print(
            "thinking display resolver `({explicitDisplay,isNonInteractive,outputFormat,verbose})"
            "{if(e)return e;if(!n)return <setting>()?\"summarized\":void 0;...}` not found in any "
            f"candidate binary ({[str(p) for p in candidate_binaries()]}) — upstream code changed "
            f"or unknown install layout. Re-investigate by grepping for {ANCHOR!r} and for "
            "`showThinkingSummaries??!1` (the settings getter), then read the function that "
            "returns \"summarized\" / \"omitted\" next to it.",
            file=sys.stderr,
        )
        return 1

    binp, data = target
    matches = list(STOCK_RX.finditer(data))
    if len(matches) != 1:
        print(
            f"expected exactly 1 occurrence of the resolver, found {len(matches)} in {binp} "
            "— upstream code changed; refusing to patch",
            file=sys.stderr,
        )
        return 1

    m = matches[0]
    replacement = build_replacement(m)
    patched = data[: m.start()] + replacement + data[m.end():]
    if len(patched) != len(data):
        print("length changed after replace — refusing to patch", file=sys.stderr)
        return 1

    def _verify(written: bytes) -> None:
        if (
            len(written) != len(data)
            or len(PATCHED_RX.findall(written)) != 1
            or STOCK_RX.search(written)
        ):
            raise RuntimeError("post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)

    print(
        f"thinking-summaries-print: applied patch to {binp} (pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
