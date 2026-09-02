#!/usr/bin/env python3
"""CLI patch: let teammates attach a `reason` when APPROVING a shutdown request,
and deliver it to the team lead.

Stock behavior (verified in 2.1.197):
- The SendMessage zod schema already accepts `reason:H.string().optional()` on
  `shutdown_response`, but validateInput hard-rejects it when approve is true:
      "reason is only delivered on rejections (approve: false) \\u2014 approvals
       are sent as a silent confirmation with no reason text; omit reason or
       reject instead"
  Teammates keep trying to use it to thank the lead on their way out, and get
  slapped with that error.
- Even without the check the reason would be dropped: the approve handler
  builds the `shutdown_approved` protocol frame via
      function RCo(e){return{type:"shutdown_approved",requestId:...,from:...,
                            timestamp:...,paneId:...,backendType:...}}
  with no reason field. (The reject path's `shutdown_rejected` frame does carry
  one.)
- The lead receives the frame's raw JSON verbatim, wrapped in a
  <teammate_message> tag (Vyt/qyt formatter), in both interactive (InboxPoller)
  and print (print.ts poll loop) modes — so any extra key we smuggle into the
  frame is visible to the lead model. The machinery that consumes the frame
  (pane kill, teamContext removal) parses it NON-strictly, so an extra key is
  harmless there.

Two same-length in-place edits (Bun single-file executable stores the JS blob
with length metadata; same technique as task-nag.sh / idle-notif.py):

A. validateInput: replace the approve+reason rejection statement with
       if(e.message.type==="shutdown_response"&&e.message.approve)
           Date.q=e.message.reason;
   padded with a block comment carrying the idempotency MARKER. `Date.q` is an
   otherwise-unused stash (grepped: zero occurrences in the stock binary) that
   bridges validateInput -> RCo within the same process/tool call. It is
   assigned on EVERY approve (undefined when no reason), so a stale reason from
   an earlier teammate can never leak into a later silent approve.

B. RCo: embed `reason:Date.q` into the frame, paying for the bytes by
   replacing the inline timestamp expression (`new Date().toISOString()`, 24
   bytes) with `Date.r` (6 bytes) — a second stash that edit A fills with the
   same ISO string one step earlier (`Date.r=new Date().toISOString()`). The
   builder has exactly one caller, handleShutdownApproval in SendMessageTool,
   which is only reached through validateInput, so `Date.r` is always fresh.
   (v1 of this patch used `Date()` there, a non-ISO string: the lead's display
   sanitizer checks frame timestamps against a strict ISO regex and rendered
   it as "[invalid timestamp]". The script migrates v1 binaries in place.)

C. Frame display/receive sanitizer (`mbt`): the per-frame-type spec table has
   no `reason` entry for `shutdown_approved` (stock never carries one), so the
   reason falls into the generic unknown-key path and is hard-cut at 256
   characters (`$2(x, Z)`, Z=256) — mid-word, no marker. Replace the
   `backendType:{kind:"backend-type"}` spec (a display-only whitelist of
   tmux/iterm2/in-process) with `reason:{kind:"body",bound:<XL>}`, the same
   bound the idle-notification `result` gets (4000 chars, " [truncated]"
   suffix beyond that). backendType then takes the generic path, which passes
   its short literal through unchanged. The bound identifier is minified, so
   it is captured from the idle_notification entry of the same table.

Resulting behavior: approve without reason is byte-identical to stock
(JSON.stringify drops the undefined key, the frame still strict-parses and
stays hidden from the transcript UI). Approve WITH reason delivers
    {"type":"shutdown_approved",...,"reason":"thanks for having me!"}
to the lead as a teammate_message. Side effect: a with-reason frame no longer
passes the lead UI's STRICT hide-parse, so it also shows up in the transcript
raw — the human sees the thank-you too. Considered a feature.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import apply_patch, candidate_binaries

MARKER = b"[kQ9dR shutdown-reason patch]"

# --- Edit A: validateInput rejection -> Date.q stash ------------------------
PATTERN_A = (
    b'if(e.message.type==="shutdown_response"&&e.message.approve&&'
    b"e.message.reason!==void 0)return{result:!1,"
    b'message:"reason is only delivered on rejections (approve: false) '
    b"\\u2014 approvals are sent as a silent confirmation with no reason "
    b'text; omit reason or reject instead",errorCode:9};'
)
CORE_A = (
    b'if(e.message.type==="shutdown_response"&&e.message.approve)'
    b"Date.q=e.message.reason,Date.r=new Date().toISOString();"
)
CORE_A_V1 = (
    b'if(e.message.type==="shutdown_response"&&e.message.approve)'
    b"Date.q=e.message.reason;"
)

# --- Edit B: shutdown_approved frame builder -> carry the reason ------------
# Anchored on the returned object literal ONLY, not the enclosing function name
# (`RCo` in 2.1.197, `LIo` in 2.1.201 — minified names are renamed every build,
# so anchoring on them breaks on every update; the object literal is stable).
PATTERN_B = (
    b'{type:"shutdown_approved",requestId:e.requestId,'
    b"from:e.from,timestamp:new Date().toISOString(),paneId:e.paneId,"
    b"backendType:e.backendType}"
)
CORE_B = (
    b'{type:"shutdown_approved",requestId:e.requestId,'
    b"from:e.from,reason:Date.q,timestamp:Date.r,paneId:e.paneId,"
    b"backendType:e.backendType"
)
CORE_B_V1 = (
    b'{type:"shutdown_approved",requestId:e.requestId,'
    b"from:e.from,reason:Date.q,timestamp:Date(),paneId:e.paneId,"
    b"backendType:e.backendType"
)
TAIL_B = b"}"

# --- Edit C: sanitizer spec table -> explicit reason bound for shutdown_approved
import re

PATTERN_C = (
    b'["shutdown_approved",{requestId:{kind:"request-id"},'
    b'from:{kind:"envelope-pinned-id"},timestamp:{kind:"timestamp"},'
    b'paneId:{kind:"pane-id"},backendType:{kind:"backend-type"}}]'
)
# the idle_notification entry names the 4000-char body bound (XL in 2.1.257)
BOUND_RE = re.compile(rb'\["idle_notification",\{[^\]]{0,400}?result:\{kind:"body",bound:(\w+)\}')


def build_replacement_c(bound: bytes) -> bytes:
    core = (
        b'["shutdown_approved",{requestId:{kind:"request-id"},'
        b'from:{kind:"envelope-pinned-id"},timestamp:{kind:"timestamp"},'
        b'paneId:{kind:"pane-id"},reason:{kind:"body",bound:' + bound + b"}"
    )
    tail = b"}]"
    pad = len(PATTERN_C) - len(core) - len(tail)
    if pad < 0:
        raise RuntimeError("edit C replacement longer than pattern — recompute")
    rep = core + b" " * pad + tail
    assert len(rep) == len(PATTERN_C)
    return rep


def build_replacement_a(core: bytes = CORE_A) -> bytes:
    pad = len(PATTERN_A) - len(core) - len(MARKER) - 4  # 4 = /* */
    if pad < 0:
        raise RuntimeError("edit A replacement longer than pattern — recompute")
    rep = core + b"/*" + MARKER + b" " * pad + b"*/"
    assert len(rep) == len(PATTERN_A)
    return rep


def build_replacement_b(core: bytes = CORE_B) -> bytes:
    pad = len(PATTERN_B) - len(core) - len(TAIL_B)
    if pad < 0:
        raise RuntimeError("edit B replacement longer than pattern — recompute")
    rep = core + b" " * pad + TAIL_B
    assert len(rep) == len(PATTERN_B)
    return rep


def main() -> int:
    cands = candidate_binaries()
    if not cands:
        print("no claude binary found (unknown install layout)", file=sys.stderr)
        return 1
    binp = cands[0]
    data = binp.read_bytes()

    m = BOUND_RE.search(data)
    if not m:
        print(
            "shutdown-reason: could not find the idle_notification sanitizer spec "
            "(anchor: `[\"idle_notification\",{` ... `result:{kind:\"body\",bound:`) — "
            f"upstream code changed; re-investigate the frame spec table in {binp}",
            file=sys.stderr,
        )
        return 1
    rep_c = build_replacement_c(m.group(1))

    # (stock pattern, previous-version replacements that must be upgraded, final replacement)
    edits = [
        ("A (validateInput)", PATTERN_A, [build_replacement_a(CORE_A_V1)], build_replacement_a()),
        ("B (frame builder)", PATTERN_B, [build_replacement_b(CORE_B_V1)], build_replacement_b()),
        ("C (sanitizer spec)", PATTERN_C, [], rep_c),
    ]
    patched = data
    applied = []
    for label, pattern, olds, final in edits:
        if final in patched:
            continue
        src = next((o for o in olds if o in patched), None)
        if src is None:
            src = pattern
        n = patched.count(src)
        if n != 1:
            print(
                f"shutdown-reason: expected exactly 1 occurrence of edit {label}'s anchor, "
                f"found {n} in {binp} — upstream code changed; re-investigate around "
                f"'reason is only delivered on rejections', the "
                f"'{{type:\"shutdown_approved\",requestId:...}}' frame object and the "
                f"'[\"shutdown_approved\",{{requestId:{{kind:...' sanitizer spec",
                file=sys.stderr,
            )
            return 1
        patched = patched.replace(src, final)
        applied.append(label)
    assert len(patched) == len(data)

    if not applied:
        print(f"shutdown-reason: confirmed already patched ({binp})", file=sys.stderr)
        return 0

    def _verify(written: bytes) -> None:
        if len(written) != len(data) or any(e[3] not in written for e in edits):
            raise RuntimeError("post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)
    print(
        f"shutdown-reason: applied edit(s) {', '.join(applied)} to {binp} "
        f"(pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
