"""Parse the Bun standalone-executable module graph embedded in the `claude` binary.

Why this exists: since Bun 1.4.1 (claude 2.1.250+) the embedded JS text is NOT
what runs. Each module ships with pre-compiled JSC bytecode, and the loader uses
it without checking that the source text still matches. A same-length text
patch therefore edits dead bytes — the patched string is visible in the binary
but the process executes the stock bytecode (verified 2026-09-01: a text-only
edit of a dialog label did not show; zeroing that module's bytecode length made
the edited label appear, so Bun compiles from source when bytecode is absent).

Layout (Bun 1.4.x, verified on claude 2.1.257 linux; same graph format on
macOS/Windows):

    [section] u64 byte_count | <graph bytes: strings, contents, bytecode, module table, ...>
              ... <Offsets> "\\n---- Bun! ----\\n" <padding to end of file>

`Offsets` sits right before the trailer; its `modules_ptr` (u32 offset, u32
length, at trailer-24) locates the module table. Every StringPointer offset is
relative to BASE = section start + 8. The section start is recovered without
parsing ELF/Mach-O/PE headers: it is file-alignment-aligned, its first u64 is
the byte count up to (about) the trailer, and the module table found through it
must resolve to '/$bunfs/…' names.

Module record (52 bytes, little-endian, all StringPointers = u32 off, u32 len):

    name | contents | sourcemap | bytecode | extra | name2 | flags(u32)

`contents` is the JS source text; `bytecode` the JSC blob. Zeroing
`bytecode.length` (keeping the offset) makes Bun compile that module from
`contents`. `extra` (~1 KB per module) is left alone — the fallback worked with
only the bytecode length cleared.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

TRAILER = b"\n---- Bun! ----\n"
REC_SIZE = 52
NAME_PREFIX = b"/$bunfs/"
_ALIGN = 512  # PE file alignment; ELF/Mach-O sections are 4096/16384-aligned (multiples of 512)


@dataclass
class Module:
    index: int
    rec_off: int  # file offset of this record
    name: bytes
    contents: tuple[int, int]  # (file offset, length) of the JS text
    bytecode: tuple[int, int]  # (file offset, length) of the JSC blob; length 0 = none

    @property
    def bytecode_len_field(self) -> int:
        """File offset of the u32 bytecode length inside the record."""
        return self.rec_off + 24 + 4

    def covers(self, off: int) -> bool:
        a, n = self.contents
        return a <= off < a + n


@dataclass
class Graph:
    base: int
    table_off: int
    modules: list[Module]

    def module_for(self, off: int) -> Module | None:
        for m in self.modules:
            if m.covers(off):
                return m
        return None


def _first_record_ok(data: bytes, base: int, mp_off: int, mp_len: int) -> bool:
    tbl = base + mp_off
    if tbl < 0 or tbl + mp_len > len(data):
        return False
    noff, nlen = struct.unpack_from("<II", data, tbl)
    if not 0 < nlen < 512:
        return False
    s = base + noff
    return 0 <= s and data[s : s + len(NAME_PREFIX)] == NAME_PREFIX


def parse(data: bytes) -> Graph:
    t = data.rfind(TRAILER)
    if t == -1:
        raise RuntimeError("Bun trailer not found — not a Bun standalone executable?")
    mp_off, mp_len = struct.unpack_from("<II", data, t - 24)
    if mp_len == 0 or mp_len % REC_SIZE:
        raise RuntimeError(
            f"module table length {mp_len} is not a multiple of {REC_SIZE} — Bun graph layout changed"
        )
    n = mp_len // REC_SIZE

    base = None
    p = (t // _ALIGN) * _ALIGN
    while p >= 0:
        (count,) = struct.unpack_from("<Q", data, p)
        # the byte count spans from p+8 through the end of the trailer (exact on
        # 2.1.257; a little slack in case a Bun release pads after it)
        if 0 <= (t + len(TRAILER)) - (p + 8) - count < 65536 and _first_record_ok(data, p + 8, mp_off, mp_len):
            base = p + 8
            break
        p -= _ALIGN
    if base is None:
        raise RuntimeError("could not recover the Bun graph base offset — layout changed")

    table = base + mp_off
    mods = []
    for i in range(n):
        r = table + i * REC_SIZE
        noff, nlen, coff, clen, _soff, _slen, boff, blen = struct.unpack_from("<8I", data, r)
        name = data[base + noff : base + noff + nlen]
        if not name.startswith(NAME_PREFIX):
            raise RuntimeError(f"record {i} name {name!r} does not look like a module name — layout changed")
        mods.append(Module(i, r, name, (base + coff, clen), (base + boff, blen)))
    return Graph(base, table, mods)


def disable_bytecode(data: bytearray, module: Module) -> bool:
    """Zero the module's bytecode length in place. Returns True if it changed."""
    p = module.bytecode_len_field
    if struct.unpack_from("<I", data, p)[0] == 0:
        return False
    struct.pack_into("<I", data, p, 0)
    return True
