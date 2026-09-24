#!/usr/bin/env python3
"""CLI patch: let a named Agent call take `cwd`, and give it a real (pane) teammate there.

Stock behavior (2.1.280), all in the Agent tool module:

  1. The model-facing input schema is `po().omit({cwd:!0})`: the full schema
     defines `cwd` ("Absolute path to run the agent in...") but the model never
     sees it, and a `cwd` key would be stripped on validation.
  2. The teammate route is gated on
         if(teamContext&&name&&!fork&&!AW(..)&&!isolation&&!cwd&&!flag()){...spawnTeammate
     so any call carrying `isolation` or `cwd` silently becomes an in-process
     background subagent, even with `name` set.
  3. `spawnTeammate({...})` is never passed `cwd`, although the pane spawners
     (split-pane and separate-window) already destructure `cwd` and launch the
     pane as `cd ${cwd||<lead's current dir>} && env ... claude ...`.

The patch:
  - schema: `omit({cwd:!0})` -> `omit({cwd:!1})`. The bundled zod v4 omit skips
    falsy mask entries (`if(!t[o])continue`), so `cwd` stays in the schema.
  - gate: drop `&&!<cwd>`, so `name`+`cwd` takes the teammate route.
  - call: `use_splitpane:!0,` -> `use_splitpane:!0,cwd:<cwd>,`.
  - in-process spawner: it hardcodes `cwd:<lead's current dir>` and would run the
    teammate in the wrong place without a word, so it now throws when `cwd` is
    set. That covers teammateMode "in-process" and the auto fallback when no
    pane backend is available.

Bytes: the gate/call region pays for `cwd:<cwd>,` and the marker comment by
shortening the adjacent error "is not offered in this session." -> "... here.".
The in-process edit pays for `||n.cwd` by rewording its (unreachable from the
Agent tool: name and prompt are always present there) missing-params error.

Pairs with the PreToolUse hook `~/.claude/hooks/agent-worktree-teammate.py`,
which turns `name`+`isolation:"worktree"` into a fresh worktree + `cwd`.

Contract (cli-patches): stderr reports applied/confirmed, exit 0.
Exit 1 if the patch can't be applied (runner relays the message to Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _binpatch import JSID, apply_patch, candidate_binaries

MARKER = b"/*T7cwd"

SCHEMA_OLD = b".omit({cwd:!0})"
SCHEMA_NEW = b".omit({cwd:!1})"
SCHEMA_DESC = b'Mutually exclusive with isolation: "worktree".'

DESTRUCTURE = re.compile(
    rb"name:" + JSID + rb",isolation:(" + JSID + rb")\}=" + JSID
    + rb",\{prompt:" + JSID + rb",description:" + JSID + rb",cwd:(" + JSID + rb")\}="
)
SPAWN_REQUIRE = b"let{spawnTeammate:"
CALL_ANCHOR = b"use_splitpane:!0,"
OFFERED_OLD = b"is not offered in this session."
OFFERED_NEW = b"is not offered here."

INPROC = re.compile(
    rb"\{name:(" + JSID + rb"),prompt:(" + JSID + rb"),agent_type:" + JSID
    + rb",plan_mode_required:" + JSID + rb"\}=(" + JSID + rb"),"
)
INPROC_MSG_OLD = b'Error("name and prompt are required for spawn operation")'
INPROC_MSG_NEW_TEXT = b"cwd unsupported for in-process teammates."


def fail(msg: str) -> int:
    print(f"teammate-cwd: {msg}", file=sys.stderr)
    return 1


def build(data: bytes) -> bytes:
    """Return the patched bytes, or raise RuntimeError naming what moved."""
    out = bytearray(data)

    # --- 1. schema -------------------------------------------------------
    if data.count(SCHEMA_OLD) != 1:
        raise RuntimeError(f"expected 1 {SCHEMA_OLD!r}, found {data.count(SCHEMA_OLD)}")
    s = data.index(SCHEMA_OLD)
    if data.rfind(SCHEMA_DESC, s - 600, s) == -1:
        raise RuntimeError(f"{SCHEMA_OLD!r} is not right after the Agent cwd description")
    out[s : s + len(SCHEMA_OLD)] = SCHEMA_NEW

    # --- 2+3. gate and spawnTeammate call --------------------------------
    if data.count(SPAWN_REQUIRE) != 1:
        raise RuntimeError(f"expected 1 {SPAWN_REQUIRE!r}, found {data.count(SPAWN_REQUIRE)}")
    sp = data.index(SPAWN_REQUIRE)
    ds = [m for m in DESTRUCTURE.finditer(data, sp - 20000, sp)]
    if len(ds) != 1:
        raise RuntimeError(f"expected 1 Agent input destructure before spawnTeammate, found {len(ds)}")
    iso, raw_cwd = ds[0].group(1), ds[0].group(2)
    # the handler copies the destructured cwd into a working variable: `,oe=bt,`
    cm = re.search(rb",(" + JSID + rb")=" + re.escape(raw_cwd) + rb",", data[ds[0].end() : sp])
    if not cm:
        raise RuntimeError("could not find the working copy of the cwd variable")
    cwd = cm.group(1)

    gate_pat = re.compile(
        rb"&&!" + re.escape(iso) + rb"&&!" + re.escape(cwd) + rb"&&!" + JSID + rb"\(\)\)\{"
    )
    gates = list(gate_pat.finditer(data, ds[0].end(), sp))
    if len(gates) != 1:
        raise RuntimeError(f"expected 1 teammate gate (&&!{iso!r}&&!{cwd!r}&&!f()){{), found {len(gates)}")
    g = gates[0]
    call = data.find(CALL_ANCHOR, sp, sp + 400)
    if call == -1:
        raise RuntimeError(f"{CALL_ANCHOR!r} not found in the spawnTeammate call")
    start, end = g.start(), call + len(CALL_ANCHOR)
    region = data[start:end]
    if region.count(OFFERED_OLD) != 1:
        raise RuntimeError(f"expected 1 {OFFERED_OLD!r} between gate and call")

    drop = b"&&!" + cwd
    gate_old = g.group(0)
    gate_new_head = gate_old.replace(drop, b"", 1)  # ...&&!<iso>&&!f()){
    body = region[len(gate_old) : -len(CALL_ANCHOR)]
    body = body.replace(OFFERED_OLD, OFFERED_NEW, 1)
    tail = CALL_ANCHOR + b"cwd:" + cwd + b","
    fixed = len(gate_new_head) + len(MARKER) + len(b"*/") + len(body) + len(tail)
    pad = len(region) - fixed
    if pad < 0:
        raise RuntimeError(f"gate/call replacement is {-pad} bytes too long")
    new_region = gate_new_head + MARKER + b" " * pad + b"*/" + body + tail
    assert len(new_region) == len(region)
    out[start:end] = new_region

    # --- 4. in-process spawner refuses cwd -------------------------------
    ips = [m for m in INPROC.finditer(data) if data.find(INPROC_MSG_OLD, m.end(), m.end() + 300) != -1]
    if len(ips) != 1:
        raise RuntimeError(f"expected 1 in-process spawner (destructure without cwd), found {len(ips)}")
    ip = ips[0]
    name, prompt, arg = ip.groups()
    cond_old = b"if(!" + name + b"||!" + prompt + b")throw"
    cond_new = b"if(!" + name + b"||!" + prompt + b"||" + arg + b".cwd)throw"
    c = data.find(cond_old, ip.end(), ip.end() + 200)
    if c == -1:
        raise RuntimeError(f"{cond_old!r} not found after the in-process destructure")
    m_at = data.find(INPROC_MSG_OLD, c, c + 200)
    seg_old = data[c : m_at + len(INPROC_MSG_OLD)]
    grow = len(cond_new) - len(cond_old)
    text_len = len(INPROC_MSG_OLD) - len(b'Error("")') - grow
    msg_text = INPROC_MSG_NEW_TEXT
    if len(msg_text) > text_len:
        raise RuntimeError(f"in-process message needs {len(msg_text)} bytes, has {text_len}")
    msg_text = msg_text + b" " * (text_len - len(msg_text))
    seg_new = cond_new + seg_old[len(cond_old) : -len(INPROC_MSG_OLD)] + b'Error("' + msg_text + b'")'
    assert len(seg_new) == len(seg_old), (len(seg_new), len(seg_old))
    out[c : c + len(seg_old)] = seg_new

    assert len(out) == len(data)
    return bytes(out)


def main() -> int:
    cands = candidate_binaries()
    if not cands:
        return fail("no claude binary found")
    binp = cands[0]
    data = binp.read_bytes()
    if MARKER in data:
        if SCHEMA_NEW not in data or b".cwd)throw" not in data:
            return fail(f"marker present but the other edits are missing in {binp} — half-applied?")
        print(f"teammate-cwd: confirmed already patched ({binp})", file=sys.stderr)
        return 0
    try:
        patched = build(data)
    except RuntimeError as e:
        return fail(
            f"{e}. Re-investigate from the stable strings 'Mutually exclusive with "
            f"isolation', 'subagent_teammate_not_offered' and 'handleSpawnInProcess'."
        )

    def _verify(written: bytes) -> None:
        if len(written) != len(data) or written.count(MARKER) != 1 or SCHEMA_OLD in written:
            raise RuntimeError("post-write verification failed — live binary untouched")

    apply_patch(binp, data, patched, _verify)
    print(f"teammate-cwd: applied to {binp} (backup {binp}.orig)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
