#!/usr/bin/python3
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
   shortening the frame's cosmetic timestamp (`new Date().toISOString()` ->
   `Date()`). No consumer parses that field (the mailbox envelope has its own
   ISO timestamp; the zod schema only requires a string).

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

import os
import shutil
import sys
import tempfile
from pathlib import Path

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
    b"from:e.from,reason:Date.q,timestamp:Date(),paneId:e.paneId,"
    b"backendType:e.backendType"
)
TAIL_B = b"}"


def build_replacement_a() -> bytes:
    pad = len(PATTERN_A) - len(CORE_A) - len(MARKER) - 4  # 4 = /* */
    if pad < 0:
        raise RuntimeError("edit A replacement longer than pattern — recompute")
    rep = CORE_A + b"/*" + MARKER + b" " * pad + b"*/"
    assert len(rep) == len(PATTERN_A)
    return rep


def build_replacement_b() -> bytes:
    pad = len(PATTERN_B) - len(CORE_B) - len(TAIL_B)
    if pad < 0:
        raise RuntimeError("edit B replacement longer than pattern — recompute")
    rep = CORE_B + b" " * pad + TAIL_B
    assert len(rep) == len(PATTERN_B)
    return rep


def candidate_binaries() -> list[Path]:
    """The single ACTIVE binary (`which claude` resolved), else newest in versions/.

    Returning ONLY the active binary avoids the stale-old-version masking bug:
    an old patched binary lingering in versions/ (e.g. 2.1.197 after an update to
    2.1.201) must not let a patch report 'already patched' and skip the live one.
    """
    which = shutil.which("claude")
    if which:
        real = Path(which).resolve()
        if real.is_file():
            return [real]
    vdir = Path.home() / ".local/share/claude/versions"
    if vdir.is_dir():
        files = [
            p
            for p in vdir.iterdir()
            if p.is_file() and p.suffix not in (".orig",) and ".patch." not in p.name
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return [files[0]]
    return []


def main() -> int:
    rep_a = build_replacement_a()
    rep_b = build_replacement_b()

    target = None
    for binp in candidate_binaries():
        data = binp.read_bytes()
        if MARKER in data:
            if rep_b not in data:
                print(
                    f"shutdown-reason: MARKER present but edit B missing in {binp} "
                    f"— binary is half-patched (should be impossible: both edits "
                    f"land in one atomic write). Restore {binp}.orig and re-run.",
                    file=sys.stderr,
                )
                return 1
            print(f"shutdown-reason: confirmed already patched ({binp})", file=sys.stderr)
            return 0
        if PATTERN_A in data or PATTERN_B in data:
            target = (binp, data)
            break

    if target is None:
        print(
            f"neither pattern nor marker found in any candidate binary "
            f"({[str(p) for p in candidate_binaries()]}) — upstream code changed "
            f"or unknown install layout; re-investigate around the string "
            f"'reason is only delivered on rejections' and the "
            f"'{{type:\"shutdown_approved\",requestId:...}}' frame object in the binary",
            file=sys.stderr,
        )
        return 1

    binp, data = target
    for label, pat in (("A (validateInput)", PATTERN_A), ("B (RCo)", PATTERN_B)):
        n = data.count(pat)
        if n != 1:
            print(
                f"expected exactly 1 occurrence of pattern {label}, found {n} "
                f"in {binp} — upstream code changed; refusing to patch",
                file=sys.stderr,
            )
            return 1

    patched = data.replace(PATTERN_A, rep_a).replace(PATTERN_B, rep_b)
    assert len(patched) == len(data)

    # Write to a temp copy then atomically swap in (in-place write on a live
    # binary hits ETXTBSY if a claude process is running from it).
    fd, tmp = tempfile.mkstemp(prefix=binp.name + ".patch.", dir=str(binp.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(patched)
        written = Path(tmp).read_bytes()
        if (
            len(written) != len(data)
            or MARKER not in written
            or rep_b not in written
            or PATTERN_A in written
            or PATTERN_B in written
        ):
            raise RuntimeError(
                f"post-write verification failed on {tmp} — live binary untouched"
            )
        shutil.copymode(binp, tmp)
        orig = binp.with_name(binp.name + ".orig")
        if not orig.exists():
            shutil.copy2(binp, orig)
        os.replace(tmp, binp)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    print(
        f"shutdown-reason: applied both edits to {binp} "
        f"(pristine backup at {binp}.orig)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
