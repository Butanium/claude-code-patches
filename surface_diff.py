#!/usr/bin/env python3
"""Diff the surfaces a harness consumes between two `claude` binaries.

After an update, the changelog is not a complete account of what appeared: a
status-line payload field, a settings key or a hook input field can ship
without a line there, and then nothing reads it for releases. The binary is
the complete source, but a text diff of two unpacked bundles is all
minification churn. This extracts *key sets* per surface from each binary and
diffs those:

    env           environment variables the CLI reads (process.env, the typed
                  env registry, env.get("X"), CLAUDE_*/ANTHROPIC_* literals)
    settings      settings.json keys, from the schema (dotted paths, with the
                  schema's .describe() text as context)
    hook-events   hook event names
    hook-input    fields of each hook event's input payload
    hook-output   fields of the hook JSON output (common + hookSpecificOutput)
    statusline    fields of the status-line command's JSON payload
    tools         built-in tool names and their input-schema keys
    tool-deferral tools marked shouldDefer (loaded only through ToolSearch)
    commands      slash commands
    mod-events    function-hook (Claude Mods plugin) event names
    gates         tengu_* names read as feature gates / dynamic configs
    events        tengu_* names logged as telemetry events
    tengu-other   tengu_* literals in neither role

    ./surface_diff.py OLD_BIN NEW_BIN                 # report to stdout
    ./surface_diff.py OLD_BIN NEW_BIN -o report.md --json diff.json
    ./surface_diff.py --auto                          # what run_cli_patches.sh calls

OLD/NEW may be binaries or directories already unpacked by clisrc.py.
Pristine `.orig` copies are the better input: patched binaries carry edited
text. `--auto` diffs the live binary (its `.orig` when present) against the
newest older version in the same directory, once per pair, writes the report
next to the behavior-test results and prints a short summary for the session.

Extraction locates everything through stable string literals and key names,
never minified identifiers, which are renamed every build. It still depends on
code shapes that can change; each surface has a floor on how many keys it must
find, and a surface under its floor is reported as FAILED instead of as "no
changes". An empty diff must mean nothing changed, not that extraction broke.
"""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clisrc

# --------------------------------------------------------------------------
# JS scanning: just enough to match brackets and split object literals in
# minified code without being fooled by strings, templates and regex literals.

OPEN = {"{": "}", "(": ")", "[": "]"}
_SPECIAL = re.compile(r"[\"'`/{}()\[\]]")
_SPECIAL_EXPR = re.compile(r"[\"'`/{}()\[\]?:]")
_SCHEMA_HEAD = re.compile(r"[A-Za-z_$][\w$]*\(\s*[\[{]")  # u({…}) / union([…])
_SLOT_IDENT = re.compile(r"\s*([A-Za-z_$][\w$]*)\s*")
_SPECIAL_COMMA = re.compile(r"[\"'`/{}()\[\],;]")
_TEMPLATE_STOP = re.compile(r"[`\\$]")
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KW = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw", "yield", "await"}
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_ID_TAIL = re.compile(r"[\w$]")


def skip_string(src: str, i: int) -> int:
    q, n = src[i], len(src)
    i += 1
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
        elif c == q or c == "\n":
            return i + 1
        else:
            i += 1
    return n


def skip_template(src: str, i: int) -> int:
    n = len(src)
    i += 1
    while i < n:
        m = _TEMPLATE_STOP.search(src, i)
        if not m:
            return n
        i = m.start()
        c = src[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return i + 1
        elif src.startswith("${", i):
            i = match(src, i + 1) + 1
        else:
            i += 1
    return n


def skip_regex(src: str, i: int) -> int:
    n, in_class = len(src), False
    i += 1
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "\n":
            return i
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "/":
            i += 1
            while i < n and src[i].isalpha():
                i += 1
            return i
        i += 1
    return n


def prev_nonspace(src: str, i: int) -> int:
    j = i - 1
    while j >= 0 and src[j] in " \t\r\n":
        j -= 1
    return j


def word_before(src: str, j: int) -> str:
    """The identifier ending at index j (inclusive), or ''."""
    k = j
    while k >= 0 and _ID_TAIL.match(src[k]):
        k -= 1
    return src[k + 1 : j + 1]


def regex_allowed(src: str, i: int) -> bool:
    """Whether the '/' at i starts a regex literal (vs. a division)."""
    j = prev_nonspace(src, i)
    if j < 0:
        return True
    c = src[j]
    if c in "+-" and j > 0 and src[j - 1] == c:
        return False  # a++ / b
    if c in _REGEX_PREV:
        return True
    if _ID_TAIL.match(c):
        return word_before(src, j) in _REGEX_KW
    return False


def skip_slash(src: str, i: int) -> int | None:
    """Skip a comment or regex starting at i; None if it is a division."""
    nxt = src[i + 1 : i + 2]
    if nxt == "/":
        j = src.find("\n", i)
        return len(src) if j < 0 else j
    if nxt == "*":
        j = src.find("*/", i + 2)
        return len(src) if j < 0 else j + 2
    if regex_allowed(src, i):
        return skip_regex(src, i)
    return None


def match(src: str, i: int) -> int:
    """Index of the bracket closing the one at src[i] (len(src) if unclosed)."""
    depth, n = 1, len(src)
    i += 1
    while i < n:
        m = _SPECIAL.search(src, i)
        if not m:
            return n
        i = m.start()
        c = src[i]
        if c in "\"'":
            i = skip_string(src, i)
        elif c == "`":
            i = skip_template(src, i)
        elif c == "/":
            j = skip_slash(src, i)
            i = i + 1 if j is None else j
        elif c in OPEN:
            depth += 1
            i += 1
        else:
            depth -= 1
            if depth == 0:
                return i
            i += 1
    return n


def split_top(src: str, s: int, e: int, seps: str = ",") -> list[tuple[int, int]]:
    """Split src[s:e] at depth-0 separators."""
    parts, start, i = [], s, s
    while i < e:
        m = _SPECIAL_COMMA.search(src, i, e)
        if not m:
            break
        i = m.start()
        c = src[i]
        if c in seps:
            parts.append((start, i))
            start = i = i + 1
        elif c in ",;":
            i += 1
        elif c in "\"'":
            i = skip_string(src, i)
        elif c == "`":
            i = skip_template(src, i)
        elif c == "/":
            j = skip_slash(src, i)
            i = i + 1 if j is None else j
        elif c in OPEN:
            i = match(src, i) + 1
        else:
            i += 1
    parts.append((start, e))
    return parts


def statement_end(src: str, s: int, limit: int) -> int:
    """End of the expression starting at s: the first depth-0 ';' or an
    unbalanced closing bracket."""
    i = s
    while i < limit:
        m = _SPECIAL_COMMA.search(src, i, limit)
        if not m:
            return limit
        i = m.start()
        c = src[i]
        if c == ";":
            return i
        if c == ",":
            i += 1
        elif c in "\"'":
            i = skip_string(src, i)
        elif c == "`":
            i = skip_template(src, i)
        elif c == "/":
            j = skip_slash(src, i)
            i = i + 1 if j is None else j
        elif c in OPEN:
            i = match(src, i) + 1
        else:
            return i
    return limit


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def js_string(src: str, i: int) -> tuple[str, int] | None:
    """Decode the string / template literal at src[i] -> (value, end)."""
    if i >= len(src) or src[i] not in "\"'`":
        return None
    end = skip_template(src, i) if src[i] == "`" else skip_string(src, i)
    raw = src[i + 1 : end - 1]
    out, k = [], 0
    while k < len(raw):
        c = raw[k]
        if c != "\\" or k + 1 >= len(raw):
            out.append(c)
            k += 1
            continue
        d = raw[k + 1]
        if d == "u" and raw[k + 2 : k + 3] == "{":
            close = raw.find("}", k)
            out.append(chr(int(raw[k + 3 : close], 16)))
            k = close + 1
        elif d == "u":
            out.append(chr(int(raw[k + 2 : k + 6], 16)))
            k += 6
        elif d == "x":
            out.append(chr(int(raw[k + 2 : k + 4], 16)))
            k += 4
        else:
            out.append(_ESCAPES.get(d, d))
            k += 2
    # `\ud83d\ude00` escapes decode to surrogate halves: pair them up, and
    # replace lone ones, which no UTF-8 writer accepts
    return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16", "replace"), end


def one_line(s: str, width: int = 220) -> str:
    """Collapse whitespace; cap at `width` chars (0 = no cap)."""
    s = re.sub(r"\s+", " ", s).strip()
    return s if width <= 0 or len(s) <= width else s[: width - 1] + "…"


_TEMPLATE_HOLE = re.compile(r"\$\{[^{}]*\}")


def doc_norm(s: str) -> str:
    """Documentation text with template holes blanked: `${ono}` in one build
    is `${jTo}` in the next. Innermost first, so nested holes collapse too."""
    while True:
        t = _TEMPLATE_HOLE.sub("\0", s)
        if t == s:
            return s.replace("\0", "${}")
        s = t


def change_excerpt(a: str, b: str, ctx: int = 70) -> tuple[str, str]:
    """The differing middle of two strings, with `ctx` chars around it."""
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    lo = max(0, i - ctx)

    def cut(s: str) -> str:
        hi = min(len(s), len(s) - j + ctx)
        return ("…" if lo > 0 else "") + s[lo:hi] + ("…" if hi < len(s) else "")

    return cut(a), cut(b)


def snippet(src: str, off: int, before: int = 70, after: int = 110) -> str:
    return "code: " + one_line(src[max(0, off - before) : off + after], before + after + 10)


# --------------------------------------------------------------------------
# Object literals


@dataclass
class Prop:
    kind: str  # prop | get | set | method | spread | shorthand
    key: str | None
    ks: int  # offset of the key (or of '...')
    vs: int  # value / body / spread-expression span
    ve: int


_PROP_HEAD = re.compile(
    r"(?:(?P<acc>get|set|async|static)\s*(?=[\w$\"'\[*]))?\s*(?:\*\s*)?"
    r"(?:(?P<id>[A-Za-z_$][\w$]*)|(?P<str>[\"'])|(?P<num>\d[\w.]*)|(?P<comp>\[))"
)


def parse_object(src: str, i: int) -> list[Prop]:
    """Properties of the object literal whose '{' is at src[i]."""
    end = match(src, i)
    props: list[Prop] = []
    for s, e in split_top(src, i + 1, end):
        t = s
        while t < e and src[t] in " \t\r\n":
            t += 1
        if t >= e:
            continue
        if src.startswith("...", t):
            props.append(Prop("spread", None, t, t + 3, e))
            continue
        m = _PROP_HEAD.match(src, t, e)
        if not m:
            continue
        acc = m.group("acc")
        k_end = m.end()
        if m.group("id"):
            key = m.group("id")
        elif m.group("str"):
            lit = js_string(src, m.start("str"))
            if lit is None:
                continue
            key, k_end = lit
        elif m.group("num"):
            key = m.group("num")
        else:
            k_end = match(src, m.start("comp")) + 1
            key = None
        j = k_end
        while j < e and src[j] in " \t\r\n":
            j += 1
        nxt = src[j : j + 1]
        if nxt == ":":
            props.append(Prop("prop", key, t, j + 1, e))
        elif nxt == "(":
            close = match(src, j)
            body = src.find("{", close)
            kind = acc if acc in ("get", "set") else "method"
            b_end = match(src, body) if body != -1 and body < e else e
            props.append(Prop(kind, key, t, body + 1 if body != -1 else close, b_end))
        elif not nxt and acc is None:
            props.append(Prop("shorthand", key, t, t, k_end))
        elif not nxt and acc:
            # `get` / `set` / `async` used as a plain shorthand key
            props.append(Prop("shorthand", acc, t, t, k_end))
    return props


class Groups:
    """Every {}/()/[] group of one file, from a single linear scan, so the
    innermost group around an offset is a bisect instead of a backward search.
    """

    def __init__(self, src: str):
        self.starts: list[int] = []
        self.ends: list[int] = []
        self.parent: list[int] = []
        self.kind: list[str] = []
        self._by_end: dict[int, int] | None = None
        stack: list[int] = []
        i, n = 0, len(src)
        self.ok = True
        while i < n:
            m = _SPECIAL.search(src, i)
            if not m:
                break
            i = m.start()
            c = src[i]
            if c in "\"'":
                i = skip_string(src, i)
            elif c == "`":
                i = skip_template(src, i)
            elif c == "/":
                j = skip_slash(src, i)
                i = i + 1 if j is None else j
            elif c in OPEN:
                self.starts.append(i)
                self.ends.append(n)
                self.parent.append(stack[-1] if stack else -1)
                self.kind.append(c)
                stack.append(len(self.starts) - 1)
                i += 1
            else:
                if not stack or OPEN[self.kind[stack[-1]]] != c:
                    self.ok = False  # scanner confused (regex heuristic); callers fall back
                    return
                self.ends[stack.pop()] = i
                i += 1
        self.ok = not stack

    def ancestors(self, off: int) -> list[int]:
        """Indices of every group containing off, innermost first."""
        out, k = [], self.innermost(off, "")
        while k is not None and k >= 0:
            out.append(k)
            k = self.parent[k]
        return out

    def params_of(self, src: str, k: int) -> str | None:
        """Parameter text if group k is a function / arrow / catch body."""
        if self._by_end is None:
            self._by_end = {e: st for st, e, kd in zip(self.starts, self.ends, self.kind) if kd == "("}
        j = prev_nonspace(src, self.starts[k])
        if j >= 1 and src[j - 1 : j + 1] == "=>":
            j = prev_nonspace(src, j - 1)
            if src[j : j + 1] != ")":
                return word_before(src, j)
        elif self.kind[k] != "{" or src[j : j + 1] != ")":
            return None
        start = self._by_end.get(j)
        if start is None:
            return None
        if word_before(src, prev_nonspace(src, start)) in ("if", "while", "switch", "with", "for"):
            return None
        return src[start + 1 : j]

    def innermost(self, off: int, kind: str = "{") -> int | None:
        """Index of the innermost `kind` group containing off ('' = any kind)."""
        # Groups nest, so the innermost one containing `off` is the last group
        # starting before `off` or one of its ancestors.
        k = bisect.bisect_left(self.starts, off) - 1
        while k >= 0 and self.ends[k] < off:
            k = self.parent[k]
        while k >= 0 and kind and self.kind[k] != kind:
            k = self.parent[k]
        return k if k >= 0 else None


def enclosing_object_slow(src: str, off: int, max_back: int = 400_000) -> int | None:
    j, floor = off, max(0, off - max_back)
    while True:
        j = src.rfind("{", floor, j)
        if j == -1:
            return None
        if match(src, j) > off:
            return j


def string_value(src: str, s: int, e: int) -> str | None:
    t = s
    while t < e and src[t] in " \t\r\n":
        t += 1
    lit = js_string(src, t)
    if lit is None:
        return None
    val, end = lit
    return val if src[end:e].strip() == "" else None


def describe_of(src: str, s: int, e: int) -> str | None:
    """Text of the last depth-0 `.describe("…")` in the value src[s:e]."""
    found, i = None, s
    while i < e:
        m = _SPECIAL.search(src, i, e)
        if not m:
            break
        i = m.start()
        c = src[i]
        if c in "\"'":
            i = skip_string(src, i)
        elif c == "`":
            i = skip_template(src, i)
        elif c == "/":
            j = skip_slash(src, i)
            i = i + 1 if j is None else j
        elif c in OPEN:
            close = match(src, i)
            if c == "(" and src.endswith(".describe", 0, i):
                lit = js_string(src, i + 1)
                if lit:
                    found = lit[0]
            i = close + 1
        else:
            i += 1
    return found


# --------------------------------------------------------------------------
# The unpacked bundle: files, imports/exports, and name resolution


_IMPORT = re.compile(r'import\s*(?:(?:([\w$]+)\s*,?\s*)?(?:\{([^}]*)\}|\*\s*as\s+[\w$]+)?\s*from\s*)?"([^"]+)";?\s*')
_EXPORT = re.compile(r"export\{([^}]*)\};?\s*$")
_VFS = re.compile(r"^(/\$bunfs/|B:[/\\]~BUN[/\\])")
_DEF_SCAN = re.compile(
    r"(?<![\w$.])function\s*\*?\s*(?P<fn>[A-Za-z_$][\w$]*)\s*\("
    r"|(?:(?<![\w$.])(?:var|let|const)\s+|(?<=[,;]))\s*(?P<var>[A-Za-z_$][\w$]*)\s*=(?![=>])"
)
_ARROW = re.compile(r"\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>\s*")
_LAZY = re.compile(r"[A-Za-z_$][\w$]*\(\s*(?:\(\s*\)|[A-Za-z_$][\w$]*)\s*=>")


@dataclass
class Def:
    file: str
    kind: str  # 'function' (s:e = body) or 'value' (s:e = right-hand side)
    off: int  # where the definition starts
    s: int
    e: int
    arrow: bool = False  # value is an arrow function's expression body


class Bundle:
    def __init__(self, root: Path, label: str):
        self.root, self.label = root, label
        self.files: dict[str, str] = {}
        for f in sorted(root.rglob("*.js")):
            self.files[f.relative_to(root).as_posix()] = f.read_text(errors="replace")
        self.imports: dict[str, dict[str, tuple[str, str]]] = {}
        self.exports: dict[str, dict[str, str]] = {}
        for name, src in self.files.items():
            imp: dict[str, tuple[str, str]] = {}
            pos = src.find("import", 0, 5000)
            while pos != -1:  # the run of import statements at the top of a chunk
                m = _IMPORT.match(src, pos)
                if not m:
                    break
                path = _VFS.sub("", m.group(3)).replace("\\", "/")
                for item in (m.group(2) or "").split(","):
                    a, _, b = item.strip().partition(" as ")
                    if a:
                        imp[(b or a).strip()] = (path, a.strip())
                pos = m.end()
            self.imports[name] = imp
            exp: dict[str, str] = {}
            m = _EXPORT.search(src[-20_000:])
            if m:
                for item in m.group(1).split(","):
                    a, _, b = item.strip().partition(" as ")
                    if a:
                        exp[(b or a).strip()] = a.strip()
            self.exports[name] = exp
        self._defs: dict[tuple[str, int], Def | None] = {}
        self._groups: dict[str, Groups] = {}
        self._def_idx: dict[str, dict[str, list[tuple[str, int, int]]]] = {}

    def groups(self, file: str) -> Groups | None:
        g = self._groups.get(file)
        if g is None:
            g = self._groups[file] = Groups(self.files[file])
        return g if g.ok else None

    def enclosing_object(self, file: str, off: int) -> int | None:
        """'{' offset of the innermost brace group around `off`."""
        g = self.groups(file)
        if g is not None:
            k = g.innermost(off, "{")
            return None if k is None else g.starts[k]
        return enclosing_object_slow(self.files[file], off)

    def resolve(self, file: str, name: str) -> tuple[str, str]:
        """Follow imports to the file that defines `name`."""
        for _ in range(8):
            src_file = self.imports.get(file, {}).get(name)
            if not src_file or src_file[0] not in self.files:
                return file, name
            f2, exported = src_file
            file, name = f2, self.exports.get(f2, {}).get(exported, exported)
        return file, name

    def _def_index(self, file: str) -> dict[str, list[tuple[str, int, int]]]:
        """name -> [(kind, match start, match end)] for one file, one pass."""
        idx = self._def_idx.get(file)
        if idx is None:
            idx = {}
            for m in _DEF_SCAN.finditer(self.files[file]):
                if m.group("fn"):
                    idx.setdefault(m.group("fn"), []).append(("function", m.start(), m.end()))
                else:
                    idx.setdefault(m.group("var"), []).append(("value", m.start(), m.end()))
            self._def_idx[file] = idx
        return idx

    def _def(self, file: str, kind: str, ms: int, me: int) -> Def | None:
        key = (file, ms)
        if key in self._defs:
            return self._defs[key]
        src = self.files[file]
        d: Def | None = None
        if kind == "function":
            close = match(src, me - 1)
            body = src.find("{", close)
            if body != -1:
                d = Def(file, "function", ms, body + 1, match(src, body))
        else:
            end = statement_end_binding(src, me)
            arrow = _ARROW.match(src, me, min(end, me + 300))
            if arrow and src[arrow.end() : arrow.end() + 1] == "{":
                d = Def(file, "function", ms, arrow.end() + 1, match(src, arrow.end()))
            elif arrow:
                d = Def(file, "value", ms, arrow.end(), end, arrow=True)
            else:
                d = Def(file, "value", ms, me, end)
        self._defs[key] = d
        return d

    def _wanted(self, d: Def, want: str) -> bool:
        src = self.files[d.file]
        head = src[d.s : d.s + 200].lstrip()
        if want == "call":
            return d.kind == "function" or d.arrow or bool(_LAZY.match(head))
        if d.kind != "value" or d.arrow:
            return False
        if want == "object":
            return head.startswith("{")
        if want == "value":
            return True
        if want == "schema":
            return bool(_SCHEMA_HEAD.match(head) or _LAZY.match(head))
        return string_value(src, d.s, d.e) is not None  # 'string'

    def lookup(self, file: str, name: str, use_off: int, want: str, limit: int = 3) -> list[Def]:
        """Up to `limit` definitions of `name` visible from `use_off`, nearest
        first, of the wanted shape: 'call' (a function, arrow or lazy
        `wrap(()=>…)` binding), 'object' (a `{…}` literal), 'schema' (a
        `shape({…})` / `union([…])` call or lazy wrapper), 'value' (any
        non-function binding) or 'string'."""
        f2, n2 = self.resolve(file, name)
        raw = self._def_index(f2).get(n2, [])
        if not raw:
            return []
        g = self.groups(f2)
        ug = self.groups(file)
        use_anc = ug.ancestors(use_off) if ug is not None else []
        name_re = re.compile(rf"(?<![\w$.]){re.escape(name)}(?![\w$])")

        def shadowed_below(scope: int | None) -> bool:
            """A parameter named `name` between the use and `scope`."""
            for k in use_anc:
                if k == scope:
                    return False
                ps = ug.params_of(self.files[file], k) if ug is not None else None
                if ps and name_re.search(ps):
                    return True
            return False

        def candidates() -> Iterator[tuple[str, int, int]]:
            if f2 != file or g is None:
                if shadowed_below(None):
                    return
                if g is None:
                    yield from raw
                    return
                top = [r for r in raw if g.innermost(r[1], "{") is None]
                yield from top
                yield from (r for r in raw if r not in top)
                return
            idx = bisect.bisect_left([r[1] for r in raw], use_off)
            for r in raw[:idx][::-1] + raw[idx:]:
                k = g.innermost(r[1], "{")
                if (k is None or g.starts[k] < use_off <= g.ends[k]) and not shadowed_below(k):
                    yield r

        out: list[Def] = []
        for kind, ms, me in candidates():
            if g is not None:  # `(a,b={…})` parameter defaults and call arguments are not bindings in scope
                k = g.innermost(ms, "")
                if k is not None and g.kind[k] != "{":
                    continue
            d = self._def(f2, kind, ms, me)
            if d is not None and self._wanted(d, want):
                out.append(d)
                if len(out) >= limit:
                    break
        return out

    def const_string(self, file: str, name: str, use_off: int) -> str | None:
        for d in self.lookup(file, name, use_off, "string"):
            return string_value(self.files[d.file], d.s, d.e)
        return None

    def iter_matches(self, pattern: re.Pattern[str], needle: str, prev: str | None = None) -> Iterator[tuple[str, re.Match[str]]]:
        """Matches in every file containing `needle` (a cheap prefilter: a
        regex without a literal prefix scans 50 MB slowly). `prev`: required
        character class just before the match, checked here instead of as a
        lookbehind, which would defeat the regex engine's literal search."""
        for name, src in self.files.items():
            if needle not in src:
                continue
            for m in pattern.finditer(src):
                if prev is None or (m.start() > 0 and src[m.start() - 1] in prev):
                    yield name, m


def statement_end_binding(src: str, s: int) -> int:
    """End of a `name=<rhs>` binding: depth-0 ',' or ';' or unbalanced closer."""
    i, n = s, len(src)
    while i < n:
        m = _SPECIAL_COMMA.search(src, i)
        if not m:
            return n
        i = m.start()
        c = src[i]
        if c in ",;":
            return i
        if c in "\"'":
            i = skip_string(src, i)
        elif c == "`":
            i = skip_template(src, i)
        elif c == "/":
            j = skip_slash(src, i)
            i = i + 1 if j is None else j
        elif c in OPEN:
            i = match(src, i) + 1
        else:
            return i
    return n


# --------------------------------------------------------------------------
# Key-set extraction from object literals, following calls and bindings

# `.x({...})` member calls whose argument is part of a schema's shape: zod
# composition methods, and the `z.object(…)` namespace style of older builds.
_SHAPE_METHODS = {"extend", "and", "merge", "or", "safeExtend", "object", "strictObject", "looseObject",
                  "union", "discriminatedUnion", "intersection", "array", "record", "lazy", "tuple"}
_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "new", "await", "import", "super", "with"}


class Keys:
    """Collects dotted key paths from JS expressions.

    Object literals contribute their keys; `...spread`, calls with no
    object-literal argument and bare identifiers are followed to their
    definitions (up to `hops` levels), so a payload assembled from helper
    functions and lazy schemas still reads as one key tree. Zod-style
    `u({...})` shapes nest under their property; `.describe("…")` text
    becomes the key's context.
    """

    def __init__(self, bundle: Bundle, max_depth: int = 4, hops: int = 2, schema: bool = False):
        # schema=True: the expressions are zod schemas, where an identifier
        # passed to a call (`union([bool(), shape])`, `array(shape)`) is a
        # sub-schema to follow. Off for runtime payload builders, where call
        # arguments are data the payload is computed from.
        self.b, self.max_depth, self.hops, self.schema = bundle, max_depth, hops, schema
        self.out: dict[str, str] = {}
        self.spread_keys: dict[str, str] = {}  # keys reached only through a followed spread / call
        self._active: set[tuple[str, int]] = set()
        self._followed: set[tuple[str, int, str]] = set()

    def add(self, path: str, ctx: str, via_follow: bool) -> None:
        target = self.spread_keys if via_follow else self.out
        old = target.get(path)
        if old is None or (old.startswith("code:") and not ctx.startswith("code:")):
            target[path] = ctx

    def object(self, file: str, i: int, prefix: str = "", depth: int = 0, hops: int | None = None, via: bool = False) -> None:
        hops = self.hops if hops is None else hops
        if (file, i) in self._active or depth > self.max_depth:
            return
        self._active.add((file, i))
        src = self.b.files[file]
        for p in parse_object(src, i):
            if p.kind == "spread":
                self.expr(file, p.vs, p.ve, prefix, depth, hops, via=via, spread=True)
                continue
            if p.key is None:
                continue
            path = prefix + p.key
            if p.kind == "prop":
                desc = describe_of(src, p.vs, p.ve)
                self.add(path, one_line(desc, 0) if desc else snippet(src, p.ks, 0, 160), via)
                if depth + 1 <= self.max_depth:
                    self.expr(file, p.vs, p.ve, path + ".", depth + 1, hops, via=via)
            elif p.kind == "get":
                self.add(path, snippet(src, p.ks, 0, 160), via)
                if depth + 1 <= self.max_depth:
                    for rs, re_ in returns(src, p.vs, p.ve):
                        self.expr(file, rs, re_, path + ".", depth + 1, hops, via=via)
            elif p.kind == "shorthand":
                self.add(path, snippet(src, p.ks, 0, 120), via)
                if depth + 1 <= self.max_depth:
                    self.expr(file, p.vs, p.ve, path + ".", depth + 1, hops, via=via)
            else:  # method / setter: a key, but not a data shape
                self.add(path, snippet(src, p.ks, 0, 160), via)
        self._active.discard((file, i))

    def follow_name(self, file: str, name: str, use_off: int, prefix: str, depth: int, hops: int, want: str) -> None:
        """Collect keys from the definition of `name` as seen from `use_off`:
        a called function's returned values, or a bound object literal."""
        if hops <= 0 or name in _KEYWORDS:
            return
        # Only calls spend a hop; a local binding (`let e=base.omit(…)`) is the
        # same function's data flow, guarded against cycles instead.
        nhops = hops - 1 if want == "call" else hops
        for d in self.b.lookup(file, name, use_off, want):
            key = (d.file, d.off, prefix)
            if key in self._followed:
                continue
            self._followed.add(key)
            before = len(self.out) + len(self.spread_keys)
            if d.kind == "function":
                for rs, re_ in returns(self.b.files[d.file], d.s, d.e):
                    self.expr(d.file, rs, re_, prefix, depth, nhops, via=True, ident_want="value")
            else:
                self.expr(d.file, d.s, d.e, prefix, depth, nhops, via=True, ident_want="value")
            if len(self.out) + len(self.spread_keys) > before:
                return  # nearest definition that yields keys wins

    def expr(self, file: str, s: int, e: int, prefix: str, depth: int, hops: int, via: bool = False,
             spread: bool = False, ctx: str = "", ident_want: str = "object") -> int:
        """Collect keys from the expression src[s:e]; returns objects found.

        A bare identifier is followed to its binding: only to an object
        literal where it is a property value or spread (`id:localVar` must
        not pull in whatever that local was computed from), to any value
        where it is what a function returns or a ternary branch."""
        src = self.b.files[file]
        t = s
        while t < e and src[t] in " \t\r\n":
            t += 1
        body = src[t:e].rstrip()
        if _IDENT.fullmatch(body):
            self.follow_name(file, body, t, prefix, depth, hops, ident_want)
            return 0
        found, i = 0, s
        if self.schema and ctx in ("(", "["):
            for a, z in split_top(src, s, e):
                item = src[a:z].strip()
                if _IDENT.fullmatch(item) and item not in _KEYWORDS:
                    self.follow_name(file, item, a, prefix, depth, hops, "schema")
        slots = ctx == ""  # call arguments and array items are not the value itself
        if slots:
            self._value_slot(file, t, e, prefix, depth, hops)
        while i < e:
            m = _SPECIAL_EXPR.search(src, i, e)
            if not m:
                break
            i = m.start()
            c = src[i]
            if c in "?:":
                if src.startswith("?.", i):
                    i += 2
                    continue
                i += 2 if src.startswith("??", i) else 1
                if slots:
                    self._value_slot(file, i, e, prefix, depth, hops)
                continue
            if c in "\"'":
                i = skip_string(src, i)
                continue
            if c == "`":
                i = skip_template(src, i)
                continue
            if c == "/":
                j = skip_slash(src, i)
                i = i + 1 if j is None else j
                continue
            if c not in OPEN:
                i += 1
                continue
            close = match(src, i)
            if c == "{":
                j = prev_nonspace(src, i)
                if j > 0 and src[j - 1 : j + 1] == "=>":  # arrow body, e.g. a lazy `wrap(()=>{…;return x})`
                    for rs, re_ in returns(src, i + 1, close):
                        found += self.expr(file, rs, re_, prefix, depth, hops, via=via, spread=spread, ident_want="value")
                elif self._is_object_literal(src, s, i, ctx) and src[close + 1 : close + 2] not in (".", "["):
                    self.object(file, i, prefix, depth, hops, via=via or spread)
                    found += 1
            elif c == "(":
                j = prev_nonspace(src, i)
                callee = word_before(src, j) if j >= s and _ID_TAIL.match(src[j]) else ""
                cs = j - len(callee) + 1  # callee start
                is_member = bool(callee) and cs > 0 and src[cs - 1] == "." and src[cs - 3 : cs] != "..."
                is_new = bool(callee) and src[max(0, cs - 4) : cs] == "new "
                if not (is_member and callee not in _SHAPE_METHODS) and not is_new:
                    inner = self.expr(file, i + 1, close, prefix, depth, hops, via=via, spread=spread, ctx="(")
                    found += inner
                    if (not inner and callee and not is_member and callee not in _KEYWORDS
                            and value_position(src, s, cs, close)):
                        self.follow_name(file, callee, cs, prefix, depth, hops, "call")
            else:  # '['
                found += self.expr(file, i + 1, close, prefix, depth, hops, via=via, spread=spread, ctx="[")
            i = close + 1
        return found

    def _value_slot(self, file: str, i: int, e: int, prefix: str, depth: int, hops: int) -> None:
        """Follow an identifier that fills a value slot on its own: the head of
        the expression or a ternary / `??` branch, ending there or continuing
        only with zod modifiers (`base.omit({…})`)."""
        src = self.b.files[file]
        m = _SLOT_IDENT.match(src, i, e)
        if not m or m.group(1) in _KEYWORDS or m.group(1) in ("void", "null", "undefined", "this", "true", "false"):
            return
        k = m.end()
        nxt = src[k : k + 1]
        if k >= e or nxt in ",;)]}:":
            self.follow_name(file, m.group(1), m.start(1), prefix, depth, hops, "value")
        elif nxt == "." and not src.startswith("...", k):
            mm = _IDENT.match(src, k + 1)
            if mm and mm.group(0) in _ZOD_CHAIN and src[mm.end() : mm.end() + 1] == "(":
                self.follow_name(file, m.group(1), m.start(1), prefix, depth, hops, "value")

    @staticmethod
    def _is_object_literal(src: str, s: int, i: int, ctx: str) -> bool:
        j = prev_nonspace(src, i)
        if j < s:
            return True
        c = src[j]
        if c == "(":
            return True
        if c in "[?:=&|":
            return not (c == "=" and src[j - 1 : j] == ">") if j > 0 else True
        if c == "," and ctx == "[":
            return True
        if c == "." and src.endswith("...", 0, j + 1):
            return True
        if _ID_TAIL.match(c) and word_before(src, j) == "return":
            return True
        return False


_ZOD_CHAIN = {"optional", "nullable", "nullish", "describe", "default", "catch", "array", "strict", "passthrough",
              "strip", "and", "or", "merge", "extend", "readonly", "min", "max", "int", "transform", "refine",
              "superRefine", "pipe", "brand", "meta", "partial", "required", "pick", "omit", "check"}


def value_position(src: str, s: int, cs: int, close: int) -> bool:
    """Whether the call spanning src[cs:close+1] *is* the value being built,
    as opposed to a condition (`f()?a:b`, `f()&&x`, `!f()`), an operand
    (`f()===x`) or the receiver of a property read (`f().name`). Zod modifier
    chains (`schema().optional()`) keep it a value."""
    j = prev_nonspace(src, cs)
    if j >= s:
        c = src[j]
        if c == ">" and src[j - 1 : j] != "=":
            return False  # comparison, not an arrow
        if c in "!<+-*%/~^" or (c == "=" and src[j - 1 : j] in ("=", "!", "<", ">")):
            return False
        if _ID_TAIL.match(c) and word_before(src, j) in ("typeof", "instanceof", "in", "void", "delete"):
            return False
    k = close + 1
    while True:
        while k < len(src) and src[k] in " \t\r\n":
            k += 1
        nxt = src[k : k + 2]
        if nxt[:1] == "." and nxt != "..":
            m = _IDENT.match(src, k + 1)
            if not m or src[m.end() : m.end() + 1] != "(" or m.group(0) not in _ZOD_CHAIN:
                return False
            k = match(src, m.end()) + 1
            continue
        if nxt in ("||", "??") or nxt[:1] in (",", ")", "]", "}", ";", ":", ""):
            return True
        return False


def returns(src: str, s: int, e: int) -> list[tuple[int, int]]:
    """Spans of the expressions returned in the function body src[s:e]."""
    out = []
    for m in re.finditer(r"(?<![\w$.])return(?![\w$])", src[s:e]):
        rs = s + m.end()
        out.append((rs, statement_end(src, rs, e)))
    return out


# --------------------------------------------------------------------------
# Surfaces


@dataclass
class Surface:
    name: str
    title: str
    items: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # read directly by a customized harness (status-line script, hooks,
    # settings.json, deferral patches): growth here is worth a ping
    consumed: bool = False
    group: str = ""  # surfaces diffed against their group's union (tengu roles)
    # key -> context computed only if the key ends up in the diff (costly lookups)
    lazy: dict[str, Callable[[], str]] = field(default_factory=dict)

    def context(self, key: str) -> str:
        fn = self.lazy.get(key)
        return fn() if fn is not None else self.items.get(key, "")


def need(s: Surface, count: int, *must: str) -> Surface:
    if len(s.items) < count:
        s.errors.append(f"found {len(s.items)} keys, expected at least {count}")
    missing = [k for k in must if k not in s.items]
    if missing:
        s.errors.append("missing anchor keys: " + ", ".join(missing))
    return s


ENV_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
ENV_LITERAL = re.compile(r'"((?:CLAUDE|ANTHROPIC|CLAUDECODE|DISABLE|ENABLE|OTEL|MCP)_[A-Z0-9_]+|CLAUDECODE)"')
ENV_PROCESS = re.compile(r'process\.env(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\])')
ENV_GET = re.compile(r'\.env\.get\(\s*"([A-Za-z_][A-Za-z0-9_]*)"')  # function-hook plugin API
ENV_REGISTRY = re.compile(r":\(\)=>([A-Za-z_$][\w$]*)")
_REGISTRY_BINDING = re.compile(r"(?<=[,;\s])([A-Za-z_$][\w$]*)=([A-Za-z_$][\w$]*)\.(\w+)\(([^()]{0,80})\)")


def env_surface(b: Bundle) -> Surface:
    s = Surface("env", "Environment variables")
    registry: dict[str, str] = {}
    # The typed env registry: namespace objects {NAME:()=>binding} whose
    # bindings are `x=M.bool()` / `M.str()` / `M.int({…})` / `M.enum([…])`.
    # Namespaces of other constants share the shape, so an entry counts only
    # when its binding is built by the file's majority parser object `M`.
    for src in b.files.values():
        if src.count(":()=>") < 20:
            continue
        pairs = []
        for m in ENV_REGISTRY.finditer(src):
            name = word_before(src, m.start() - 1)
            if ENV_NAME.match(name) and src[m.start() - len(name) - 1 : m.start() - len(name)] in "{,":
                pairs.append((name, m.group(1)))
        if len(pairs) < 20:
            continue
        built = {m.group(1): m for m in _REGISTRY_BINDING.finditer(src)}
        typed: dict[str, tuple[str, str]] = {}
        for name, binding in pairs:
            t = built.get(binding)
            if t:
                typed[name] = (t.group(2), f"{t.group(3)}({t.group(4)})" if t.group(4) else t.group(3))
        receivers = [r for r, _ in typed.values()]
        if not receivers:
            continue
        main = max(set(receivers), key=receivers.count)
        for name, (r, kind) in typed.items():
            if r == main:
                registry[name] = kind
    names: set[str] = set(registry)
    for _, m in b.iter_matches(ENV_PROCESS, "process.env"):
        names.add(m.group(1) or m.group(2))
    for _, m in b.iter_matches(ENV_GET, ".env.get("):
        names.add(m.group(1))
    for _, m in b.iter_matches(ENV_LITERAL, '"'):
        if not m.group(1).endswith("_"):  # a prefix (`startsWith("CLAUDE_CODE_USE_")`), not a name
            names.add(m.group(1))
    for n in sorted(names):
        s.items[n] = ""
        s.lazy[n] = lambda n=n: env_context(b, n, registry)
    return need(s, 300, "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY")


def env_context(b: Bundle, name: str, registry: dict[str, str]) -> str:
    """A usage site of the variable, preferring a read over a list entry."""
    q = re.escape(name)
    best = None
    pat = re.compile(rf'(?:process\.env\.|(?<![\w$])[A-Za-z_$][\w$]*\.){q}(?![\w$])|"{q}"')
    for src in b.files.values():
        if name not in src:
            continue
        for m in pat.finditer(src):
            if src[m.start() : m.start() + 1] == '"':
                prev = src[max(0, m.start() - 1) : m.start()]
                nxt = src[m.end() : m.end() + 1]
                if prev in ",[" and nxt in ",]":
                    best = best or snippet(src, m.start(), 60, 100)
                    continue
            best = snippet(src, m.start(), 90, 110)
            break
        else:
            continue
        break
    kind = registry.get(name)
    prefix = f"[registry {kind}] " if kind else ""
    return prefix + (best or "")


SETTINGS_ANCHOR = re.compile(r'\$schema:[^,]{0,60}\.describe\("JSON Schema reference for Claude Code settings"\)')


def settings_surface(b: Bundle) -> Surface:
    s = Surface("settings", "settings.json keys", consumed=True)
    for file, m in b.iter_matches(SETTINGS_ANCHOR, "JSON Schema reference for Claude Code settings"):
        obj = b.enclosing_object(file, m.start())
        if obj is None:
            continue
        k = Keys(b, max_depth=3, hops=1, schema=True)
        k.object(file, obj)
        for path, ctx in {**k.spread_keys, **k.out}.items():
            s.items.setdefault(path, ctx)
    return need(s, 80, "apiKeyHelper", "statusLine", "hooks", "env", "permissions")


HOOK_EVENT_ARRAY = re.compile(r'\["PreToolUse","PostToolUse"[^\]]*\]')


def hook_events_surface(b: Bundle) -> Surface:
    s = Surface("hook-events", "Hook event names", consumed=True)
    for _, m in b.iter_matches(HOOK_EVENT_ARRAY, '["PreToolUse"'):
        if '"SessionStart"' not in m.group(0) or '"Stop"' not in m.group(0):
            continue
        for ev in re.findall(r'"([A-Za-z]+)"', m.group(0)):
            s.items.setdefault(ev, "")
    for file, m in b.iter_matches(HOOK_INPUT_ANCHOR, "hook_event_name:"):
        if m.group(1) in s.items and not s.items[m.group(1)]:
            s.items[m.group(1)] = snippet(b.files[file], m.start(), 40, 160)
    return need(s, 20, "PreToolUse", "SessionStart", "Stop")


HOOK_INPUT_ANCHOR = re.compile(r'hook_event_name:(?:[A-Za-z_$][\w$.]*\()?"([A-Za-z]+)"')
HOOK_OUTPUT_ANCHOR = re.compile(r'hookEventName:(?:[A-Za-z_$][\w$.]*\()?"([A-Za-z]+)"')


def hook_payload_surface(b: Bundle, anchor: re.Pattern[str], needle: str, name: str, title: str) -> Surface:
    """Keys of every object literal carrying a literal event discriminator,
    grouped by event. Keys that reach three or more events only through a
    spread or a `base().and(…)` (the fields every event shares) are grouped
    under `(common)`."""
    s = Surface(name, title, consumed=True)
    seen: set[tuple[str, int]] = set()
    shared: dict[str, dict[str, str]] = {}  # key -> {event: ctx}
    for file, m in b.iter_matches(anchor, needle):
        src = b.files[file]
        obj = b.enclosing_object(file, m.start())
        if obj is None or (file, obj) in seen:
            continue
        seen.add((file, obj))
        ev = m.group(1)
        k = Keys(b, max_depth=2, hops=2)
        k.object(file, obj)
        # SDK schemas: `base().and(u({hook_event_name:…}))` -> base keys are shared
        base = re.search(r"([A-Za-z_$][\w$]*)\(\)\.and\([A-Za-z_$][\w$]*\($", src[max(0, obj - 40) : obj])
        if base:
            k.follow_name(file, base.group(1), obj, "", 0, 2, "call")
        top = {p.split(".")[0] for p in k.out}
        for path, ctx in {**k.spread_keys, **k.out}.items():
            if path.split(".")[0] in ("hook_event_name", "hookEventName"):
                continue
            if path in k.out or path.split(".")[0] in top:
                s.items.setdefault(f"{ev}.{path}", ctx)
            else:
                shared.setdefault(path, {}).setdefault(ev, ctx)
    for path, evs in shared.items():
        if len(evs) >= 3:
            s.items.setdefault(f"(common).{path}", next(iter(evs.values())))
        else:
            for ev, ctx in evs.items():
                s.items.setdefault(f"{ev}.{path}", ctx)
    return s


def hook_input_surface(b: Bundle) -> Surface:
    s = hook_payload_surface(b, HOOK_INPUT_ANCHOR, "hook_event_name:", "hook-input", "Hook input payload fields")
    return need(s, 60, "PreToolUse.tool_name", "PreToolUse.tool_input", "(common).session_id", "(common).transcript_path")


HOOK_OUTPUT_COMMON = re.compile(r"suppressOutput:")


def hook_output_surface(b: Bundle) -> Surface:
    s = hook_payload_surface(b, HOOK_OUTPUT_ANCHOR, "hookEventName:", "hook-output", "Hook JSON output fields")
    for file, m in b.iter_matches(HOOK_OUTPUT_COMMON, "suppressOutput:", prev="{,"):
        src = b.files[file]
        obj = b.enclosing_object(file, m.start())
        if obj is None:
            continue
        keys = {p.key for p in parse_object(src, obj)}
        if not {"continue", "stopReason"} <= keys:
            continue
        k = Keys(b, max_depth=1, hops=1)
        k.object(file, obj)
        for path, ctx in k.out.items():
            if not path.startswith("hookSpecificOutput."):
                s.items.setdefault(f"(common).{path}", ctx)
    return need(s, 25, "PreToolUse.permissionDecision", "(common).continue", "(common).suppressOutput")


STATUSLINE_ANCHOR = re.compile(r"workspace:\{current_dir:")


def statusline_surface(b: Bundle) -> Surface:
    s = Surface("statusline", "Status-line JSON payload fields", consumed=True)
    for file, m in b.iter_matches(STATUSLINE_ANCHOR, "workspace:{current_dir:", prev="{,"):
        src = b.files[file]
        obj = b.enclosing_object(file, m.start() - 1)
        if obj is None:
            continue
        keys = {p.key for p in parse_object(src, obj)}
        if not {"model", "cost"} <= keys:
            continue
        k = Keys(b, max_depth=4, hops=2)
        k.object(file, obj)
        for path, ctx in {**k.spread_keys, **k.out}.items():
            s.items.setdefault(path, ctx)
    return need(s, 25, "session_id", "transcript_path", "model.id", "workspace.current_dir", "cost.total_cost_usd", "context_window")


TOOL_ANCHOR = re.compile(r"get inputSchema\(\)\{|inputSchema:")


def _obj_props_with_spreads(b: Bundle, file: str, obj: int, hops: int = 2) -> list[tuple[str, Prop]]:
    """Props of an object plus those of objects it spreads by name (`...Base`)."""
    src = b.files[file]
    out: list[tuple[str, Prop]] = []
    for p in parse_object(src, obj):
        name = src[p.vs : p.ve].strip() if p.kind == "spread" else ""
        if p.kind == "spread" and hops > 0 and _IDENT.fullmatch(name):
            for d in b.lookup(file, name, p.vs, "object")[:1]:
                t = d.s
                while b.files[d.file][t] in " \t\r\n":
                    t += 1
                out.extend(_obj_props_with_spreads(b, d.file, t, hops - 1))
        else:
            out.append((file, p))
    return out


def _prop_string(b: Bundle, file: str, p: Prop) -> str | None:
    src = b.files[file]
    if p.kind == "prop":
        v = string_value(src, p.vs, p.ve)
        if v is not None:
            return v
        name = src[p.vs : p.ve].strip()
        if _IDENT.fullmatch(name):
            return b.const_string(file, name, p.vs)
        return None
    if p.kind == "get":
        for rs, re_ in returns(src, p.vs, p.ve):
            v = string_value(src, rs, re_)
            if v is not None:
                return v
            first = re.match(r'\s*[^"]{0,60}?(["`])', src[rs:re_])
            if first:
                lit = js_string(src, rs + first.start(1))
                if lit:
                    return lit[0]
    return None


def tools_surfaces(b: Bundle) -> tuple[Surface, Surface]:
    tools = Surface("tools", "Built-in tools and input-schema keys")
    deferred = Surface("tool-deferral", "Tools deferred behind ToolSearch (shouldDefer)", consumed=True)
    seen: set[tuple[str, int]] = set()
    for file, m in b.iter_matches(TOOL_ANCHOR, "inputSchema", prev="{,"):
        src = b.files[file]
        obj = b.enclosing_object(file, m.start())
        if obj is None or (file, obj) in seen:
            continue
        seen.add((file, obj))
        props = _obj_props_with_spreads(b, file, obj)
        by_key = {p.key: (f, p) for f, p in props if p.key}
        name = None
        if "name" in by_key:
            name = _prop_string(b, *by_key["name"])
        if not name and "userFacingName" in by_key:
            f, p = by_key["userFacingName"]
            name = _prop_string(b, f, Prop("get", p.key, p.ks, p.vs, p.ve))
        if not name or not re.fullmatch(r"[A-Za-z][\w-]*", name):
            continue
        hint = None
        for k in ("searchHint", "description", "userFacingName"):
            if k in by_key:
                hint = _prop_string(b, *by_key[k]) if by_key[k][1].kind in ("prop", "get") else None
                if hint:
                    break
        tools.items.setdefault(name, one_line(hint, 0) if hint else snippet(src, obj, 0, 160))
        if "shouldDefer" in by_key:
            f, p = by_key["shouldDefer"]
            if b.files[f][p.vs : p.ve].strip() == "!0":
                deferred.items.setdefault(name, tools.items[name])
        f, p = by_key["inputSchema"]
        k = Keys(b, max_depth=2, hops=3, schema=True)
        if p.kind == "get":
            for rs, re_ in returns(b.files[f], p.vs, p.ve):
                k.expr(f, rs, re_, "", 0, 3, ident_want="value")
        else:
            k.expr(f, p.vs, p.ve, "", 0, 3)
        keys = {**k.spread_keys, **k.out}
        if "properties" in keys and "type" in keys:  # JSON-schema tool (MCP-style)
            keys = {kk[len("properties."):]: v for kk, v in keys.items() if kk.startswith("properties.")}
            keys = {kk: v for kk, v in keys.items() if kk.count(".") == 0 or ".properties." in kk}
        for path, ctx in keys.items():
            tools.items.setdefault(f"{name}.{path}", ctx)
    need(tools, 40, "Bash", "Bash.command", "Read", "Edit", "Write", "Monitor")
    need(deferred, 3)
    return tools, deferred


COMMAND_ANCHOR = re.compile(r'type:"(?:local|local-jsx|prompt)"')
SKILL_ANCHOR = re.compile(r"(?:async )?getPromptForCommand\b")  # bundled skills: registerSkill({name, …})


def commands_surface(b: Bundle) -> Surface:
    s = Surface("commands", "Slash commands and bundled skills")
    seen: set[tuple[str, int]] = set()
    anchors = ((COMMAND_ANCHOR, 'type:"', ""), (SKILL_ANCHOR, "getPromptForCommand", "[bundled skill] "))
    for pattern, needle, tag in anchors:
        for file, m in b.iter_matches(pattern, needle, prev="{,"):
            src = b.files[file]
            obj = b.enclosing_object(file, m.start())
            if obj is None or (file, obj) in seen:
                continue
            seen.add((file, obj))
            by_key = {p.key: p for p in parse_object(src, obj) if p.key}
            desc_key = "description" if "description" in by_key else "menuDescription"
            if "name" not in by_key or desc_key not in by_key:
                continue
            name = _prop_string(b, file, by_key["name"])
            if not name or not re.fullmatch(r"[a-z][\w:-]*", name) or name in s.items:
                continue
            desc = _prop_string(b, file, by_key[desc_key]) or ""
            if "aliases" in by_key:
                desc += " (aliases " + one_line(src[by_key["aliases"].vs : by_key["aliases"].ve], 60) + ")"
            s.items[name] = tag + one_line(desc, 0)
    return need(s, 30, "compact", "clear", "resume")


MOD_REGISTRY_ANCHOR = re.compile(r'"tool\.call":')
MOD_EVENT_DECL = re.compile(r'(?<=[{,])event:"([a-z]+(?:\.[A-Za-z]+)+)"')
_MOD_EVENT_NAME = re.compile(r"[a-z]+(?:\.[A-Za-z]+)+")


def mod_events_surface(b: Bundle) -> Surface:
    """Function-hook (Claude Mods plugin) events: keys of the engine's event
    registries (objects keyed "tool.call", "prompt.submit", …) plus every
    `{event:"x.y",…}` site declaration."""
    s = Surface("mod-events", "Function-hook (Claude Mods) events", consumed=True)
    for file, m in b.iter_matches(MOD_REGISTRY_ANCHOR, '"tool.call":', prev="{,"):
        src = b.files[file]
        obj = b.enclosing_object(file, m.start())
        if obj is None:
            continue
        props = parse_object(src, obj)
        if not any(p.key == "prompt.submit" for p in props):
            continue
        for p in props:
            if p.key and _MOD_EVENT_NAME.fullmatch(p.key):
                s.items.setdefault(p.key, snippet(src, p.ks, 0, 160))
    for file, m in b.iter_matches(MOD_EVENT_DECL, 'event:"'):
        if m.group(1) in s.items and not s.items[m.group(1)].startswith("code: event:"):
            s.items[m.group(1)] = snippet(b.files[file], m.start(), 10, 200)
    return need(s, 20, "tool.call", "prompt.submit")


TENGU_CALL = re.compile(r'\(\s*"(tengu_[a-z0-9_]+)"\s*(\)|,\s*(.))')
TENGU_ANY = re.compile(r"tengu_[a-z0-9_]+")
TENGU_CONST = re.compile(r'(?<![\w$.])([A-Za-z_$][\w$]*)="(tengu_[a-z0-9_]+)"')
_TINY_WRAPPER = re.compile(r"function ([A-Za-z_$][\w$]*)\(\)\{return[^{};]{0,60}$")


def gate_context(b: Bundle, file: str, off: int) -> str:
    """Where a gate is used. Most reads sit alone in a one-line wrapper
    (`function Q(){return x("tengu_…",!1)}`), which says nothing; the
    wrapper's first call site does."""
    src = b.files[file]
    w = _TINY_WRAPPER.search(src, max(0, off - 80), off)
    if w:
        call = re.compile(rf"(?<![\w$.]){re.escape(w.group(1))}\(\)")
        for m in call.finditer(src):
            if not src.startswith("function", max(0, m.start() - 9)):
                return snippet(src, m.start(), 90, 110)
        for f2, imp in b.imports.items():
            for local, (srcf, exported) in imp.items():
                if srcf == file and b.exports.get(file, {}).get(exported) == w.group(1):
                    m = re.search(rf"(?<![\w$.]){re.escape(local)}\(\)", b.files[f2])
                    if m:
                        return snippet(b.files[f2], m.start(), 90, 110)
    return snippet(src, off, 30, 140)


def tengu_surfaces(b: Bundle) -> tuple[Surface, Surface, Surface]:
    """Split tengu_* names into gates (read with a default value) and events
    (logged with a payload object). Callee names are minified, so each callee's
    role is voted from its call sites' second-argument shapes. The three sets
    are diffed as one (see `group`), so a name whose role reads differently in
    the next build does not show up as removed from one and added to another."""
    gates = Surface("gates", "Feature gates / dynamic configs (tengu_*)", group="tengu")
    events = Surface("events", "Telemetry events (tengu_*)", group="tengu")
    other = Surface("tengu-other", "Other tengu_* literals (neither role recognized)", group="tengu")
    votes: dict[str, list[int]] = {}
    sites: list[tuple[str, int, str, str]] = []

    def vote(file: str, callee: str, name: str, off: int, second: str, second_off: int) -> None:
        v = votes.setdefault(callee, [0, 0])
        if second == "{":
            v[1] += 1
        elif second in "!0123456789\"'[-" or b.files[file].startswith(("null", "void"), second_off):
            v[0] += 1
        sites.append((file, off, callee, name))

    for file, m in b.iter_matches(TENGU_CALL, '"tengu_'):
        callee = word_before(b.files[file], m.start() - 1)
        if callee:
            vote(file, callee, m.group(1), m.start(), m.group(3) or ")", m.start(3) if m.group(3) else 0)
    # `var Pn="tengu_x";…aa(Pn,!0)`: the name reaches its reader through a constant
    for file, m in b.iter_matches(TENGU_CONST, '="tengu_'):
        src = b.files[file]
        for c in re.finditer(rf"\(\s*{re.escape(m.group(1))}\s*(\)|,\s*(.))", src):
            callee = word_before(src, c.start() - 1)
            if callee:
                vote(file, callee, m.group(2), c.start(), c.group(2) or ")", c.start(2) if c.group(2) else 0)
    role: dict[str, str] = {}
    for callee, (g, e) in votes.items():
        if g + e >= 2 and g > 2 * e:
            role[callee] = "gate"
        elif g + e >= 2 and e > 2 * g:
            role[callee] = "event"
    for file, off, callee, name in sites:
        r = role.get(callee)
        if r == "gate" and name not in gates.items:
            gates.items[name] = ""
            gates.lazy[name] = lambda file=file, off=off: gate_context(b, file, off)
        elif r == "event" and name not in events.items:
            events.items[name] = snippet(b.files[file], off, 30, 140)
    for file, m in b.iter_matches(TENGU_ANY, "tengu_"):
        name = m.group(0)
        if name not in gates.items and name not in events.items and name not in other.items:
            other.items[name] = snippet(b.files[file], m.start(), 60, 120)
    for n in list(events.items):
        if n in gates.items:
            del events.items[n]
    return need(gates, 200), need(events, 500), other


def extract(b: Bundle) -> dict[str, Surface]:
    out: dict[str, Surface] = {}
    steps: list[tuple[str, Callable[[Bundle], object]]] = [
        ("settings", settings_surface),
        ("env", env_surface),
        ("hook-events", hook_events_surface),
        ("hook-input", hook_input_surface),
        ("hook-output", hook_output_surface),
        ("statusline", statusline_surface),
        ("tools", tools_surfaces),
        ("commands", commands_surface),
        ("mod-events", mod_events_surface),
        ("tengu", tengu_surfaces),
    ]
    for label, fn in steps:
        try:
            res = fn(b)
        except Exception as exc:  # an extractor crash is a FAILED surface, not a crash of the report
            res = Surface(label, label)
            res.errors.append(f"extractor raised {type(exc).__name__}: {exc}")
        for surf in res if isinstance(res, tuple) else (res,):
            out[surf.name] = surf
    return out


# --------------------------------------------------------------------------
# Diff + report


@dataclass
class SurfaceDiff:
    name: str
    title: str
    consumed: bool
    old_count: int
    new_count: int
    added: dict[str, str]
    removed: dict[str, str]
    errors: list[str]
    # parent -> nested keys, when a parent key exists in both versions but its
    # children are visible in only one: usually the sub-schema moved behind a
    # reference the extractor does not follow, not a real change
    lost_subtrees: dict[str, list[str]] = field(default_factory=dict)
    new_subtrees: dict[str, list[str]] = field(default_factory=dict)
    # key -> (old, new) for keys in both versions whose own documentation
    # (`.describe()` text, command description) changed; code snippets excluded
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)


def _split_subtrees(keys: dict[str, str], mine: set[str], other: set[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Move nested keys whose nearest shared ancestor has no children at all
    on the other side into per-ancestor groups."""
    parents_other = {k.rsplit(".", 1)[0] for k in other if "." in k}
    parents_other |= {".".join(k.split(".")[:i]) for k in other for i in range(1, k.count(".") + 1)}
    kept: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    for k, ctx in keys.items():
        parts = k.split(".")
        for i in range(1, len(parts)):
            anc = ".".join(parts[:i])
            if anc in mine and anc in other and anc not in parents_other:
                groups.setdefault(anc, []).append(k)
                break
        else:
            kept[k] = ctx
    return kept, groups


DROP_ALARM = 0.3  # share of a surface's keys that may vanish in one update before it counts as broken


def diff(old: dict[str, Surface], new: dict[str, Surface], ob: Bundle, nb: Bundle) -> list[SurfaceDiff]:
    res = []

    def union(side: dict[str, Surface], name: str, group: str) -> set[str]:
        if not group:
            return set(side[name].items) if name in side else set()
        return {k for x in side.values() if x.group == group for k in x.items}

    for name, ns in new.items():
        os_ = old.get(name) or Surface(name, ns.title)
        errors = [f"{ob.label}: {e}" for e in os_.errors] + [f"{nb.label}: {e}" for e in ns.errors]
        old_all, new_all = union(old, name, ns.group), union(new, name, ns.group)
        added = {k: ns.context(k) for k in sorted(set(ns.items) - old_all)}
        removed = {k: os_.context(k) for k in sorted(set(os_.items) - new_all)}
        old_keys, new_keys = set(os_.items), set(ns.items)
        removed, lost = _split_subtrees(removed, old_keys, new_keys)
        added, gained = _split_subtrees(added, new_keys, old_keys)
        gone = len(os_.items) and (len(set(os_.items) - new_all) / len(os_.items))
        if len(os_.items) >= 20 and gone > DROP_ALARM:
            errors.append(f"{gone:.0%} of the {ob.label} keys are gone in {nb.label}: "
                          "more likely a broken extraction than a real removal")
        changed = {k: (os_.items[k], ns.items[k]) for k in sorted(old_keys & new_keys)
                   if doc_norm(os_.items[k]) != doc_norm(ns.items[k]) and os_.items[k] and ns.items[k]
                   and not os_.items[k].startswith("code:") and not ns.items[k].startswith("code:")}
        res.append(SurfaceDiff(name, ns.title, ns.consumed, len(os_.items), len(ns.items), added, removed, errors,
                               lost, gained, changed))
    return res


def version_label(p: Path) -> str:
    m = re.search(r"\d+\.\d+\.\d+", p.name)
    return m.group(0) if m else p.name


def fence(s: str) -> str:
    return s.replace("`", "ˋ")


def render(diffs: list[SurfaceDiff], old_label: str, new_label: str, secs: float) -> str:
    lines = [f"# CLI surface diff: {old_label} → {new_label}", ""]
    lines.append(f"Generated by `surface_diff.py` in {secs:.1f}s. Key sets extracted from each binary and diffed; "
                 "context for an added key comes from the new binary, for a removed one from the old. "
                 "`code:` lines are minified source around the key.")
    lines += ["", "| surface | old | new | added | removed | doc changed | status |", "|---|---:|---:|---:|---:|---:|---|"]
    for d in diffs:
        status = "FAILED: " + "; ".join(d.errors) if d.errors else "ok"
        moved = sum(map(len, d.lost_subtrees.values())) + sum(map(len, d.new_subtrees.values()))
        if moved:
            status += f" ({moved} nested keys only resolved on one side, listed separately)"
        lines.append(f"| {d.name} | {d.old_count} | {d.new_count} | {len(d.added)} | {len(d.removed)} | {len(d.changed)} | {status} |")
    lines.append("")
    for d in diffs:
        lines.append(f"## {d.title} (`{d.name}`)")
        lines.append("")
        if d.errors:
            lines.append("**Extraction FAILED — an empty diff below does not mean nothing changed:** " + "; ".join(d.errors))
            lines.append("")
        if not (d.added or d.removed or d.lost_subtrees or d.new_subtrees or d.changed):
            lines += ["No changes.", ""]
            continue
        for label, items in (("Added", d.added), ("Removed", d.removed)):
            if not items:
                continue
            lines.append(f"### {label} ({len(items)})")
            lines.append("")
            for k, ctx in items.items():
                lines.append(f"- `{k}`" + (f" — {fence(one_line(ctx, 400))}" if ctx else ""))
            lines.append("")
        if d.changed:
            lines.append(f"### Description changed ({len(d.changed)})")
            lines.append("")
            for k, (a, z) in d.changed.items():
                was, now = change_excerpt(doc_norm(a), doc_norm(z))
                lines.append(f"- `{k}` — was: {fence(was)}")
                lines.append(f"  now: {fence(now)}")
            lines.append("")
        for label, groups, side in (("Nested keys no longer resolved", d.lost_subtrees, old_label),
                                    ("Nested keys newly resolved", d.new_subtrees, new_label)):
            if not groups:
                continue
            lines.append(f"### {label} ({sum(map(len, groups.values()))} keys under {len(groups)} parents)")
            lines.append("")
            lines.append(f"The parent key exists in both versions; its children were extracted only from {side}. "
                         "Usually the sub-schema moved behind a reference the extractor does not follow — check by hand before believing it.")
            lines.append("")
            for parent, keys in groups.items():
                tails = ", ".join(k[len(parent) + 1 :] for k in keys)
                lines.append(f"- `{parent}.*` ({len(keys)}): {one_line(tails, 300)}")
            lines.append("")
    return "\n".join(lines)


def summary(diffs: list[SurfaceDiff], old_label: str, new_label: str, report: Path | None) -> str:
    counts = ", ".join(f"{d.name} +{len(d.added)}/-{len(d.removed)}" for d in diffs if d.added or d.removed)
    lines = [f"CLI surface diff {old_label} → {new_label}: {counts or 'no changes'}"]
    failed = [d for d in diffs if d.errors]
    for d in failed:
        lines.append(f"  extraction FAILED for {d.name}: {'; '.join(d.errors)}")
    for d in diffs:
        keys = list(d.added) if d.consumed else [k for k in d.added if "." not in k] if d.name in ("tools", "commands") else []
        if keys:
            shown = keys[:10]
            more = f" (+{len(keys) - len(shown)} more)" if len(keys) > len(shown) else ""
            lines.append(f"  new {d.name}: " + ", ".join(shown) + more)
    if report:
        lines.append(f"  full report: {report}")
    return "\n".join(lines)


def load(p: Path) -> tuple[Bundle, str]:
    label = version_label(p)
    root = p if p.is_dir() else clisrc.unpack(p)
    return Bundle(root, label), label


def run(old: Path, new: Path) -> tuple[list[SurfaceDiff], str, str, float]:
    t0 = time.time()
    ob, ol = load(old)
    nb, nl = load(new)
    diffs = diff(extract(ob), extract(nb), ob, nb)
    return diffs, ol, nl, time.time() - t0


# --------------------------------------------------------------------------
# --auto: called from run_cli_patches.sh after the patches ran

_VER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(\.orig)?$")
LOCK_STALE_S = 15 * 60


def pristine(p: Path) -> Path:
    orig = p.with_name(p.name + ".orig")
    return orig if orig.is_file() and orig.stat().st_size == p.stat().st_size else p


def versioned_binaries(d: Path) -> dict[tuple[int, int, int], Path]:
    """version -> binary in `d`, preferring the pristine `.orig` copy."""
    out: dict[tuple[int, int, int], Path] = {}
    if not d.is_dir():
        return out
    for f in d.iterdir():
        m = _VER.match(f.name)
        if not m or not f.is_file() or f.stat().st_size < 50_000_000:
            continue
        v = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if v not in out or f.name.endswith(".orig"):
            out[v] = f
    return out


def version_pair(live: Path) -> tuple[Path, Path] | None:
    """(previous, current) binaries for the live one. The live binary is
    normally `versions/X.Y.Z`; where it is not (a copied `claude.exe`), the
    two newest entries of the native installer's versions dir stand in."""
    m = _VER.match(live.name)
    if m:
        cur = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        vers = versioned_binaries(live.parent)
        older = [v for v in vers if v < cur]
        return (vers[max(older)], pristine(live)) if older else None
    vers = versioned_binaries(Path.home() / ".local" / "share" / "claude" / "versions")
    if len(vers) < 2:
        return None
    newest = sorted(vers)[-2:]
    return vers[newest[0]], vers[newest[1]]


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "claude-cli-patch-tests"


def ntfy(topic: str, title: str, body: str) -> None:
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), headers={"Title": title})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as exc:
        print(f"surface diff: ntfy post failed: {exc}")


def auto() -> int:
    """Once per (previous, current) version pair: write the report and JSON
    next to the behavior-test results and print the summary, which lands in
    the SessionStart context. With CLAUDE_CLI_SURFACE_DIFF_NTFY_TOPIC set,
    also post the summary when a consumed surface grew or extraction failed."""
    from _binpatch import candidate_binaries

    cands = candidate_binaries()
    pair = version_pair(cands[0]) if cands else None
    if pair is None:
        return 0
    prev, new = pair
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    stem = f"surfaces-{version_label(prev)}-to-{version_label(new)}"
    report, js, lock = d / f"{stem}.md", d / f"{stem}.json", d / f"{stem}.lock"
    if report.exists():
        return 0
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < LOCK_STALE_S:
            return 0  # another session is producing this report
        lock.touch()
    try:
        diffs, ol, nl, secs = run(prev, new)
    except Exception as exc:  # a broken extractor must say so once, not fail silently
        msg = f"CLI surface diff {version_label(prev)} → {version_label(new)} FAILED: {type(exc).__name__}: {exc}"
        report.write_text(msg + "\n", encoding="utf-8")
        lock.unlink(missing_ok=True)
        print(msg)
        return 0
    report.write_text(render(diffs, ol, nl, secs), encoding="utf-8")
    js.write_text(json.dumps([asdict(x) for x in diffs], indent=1), encoding="utf-8")
    lock.unlink(missing_ok=True)
    text = summary(diffs, ol, nl, report)
    print(text)
    topic = os.environ.get("CLAUDE_CLI_SURFACE_DIFF_NTFY_TOPIC")
    if topic and any((x.consumed and x.added) or x.errors for x in diffs):
        ntfy(topic, f"claude {ol} -> {nl}: new surface", text)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", nargs="?", type=Path, help="older binary (or clisrc-unpacked dir)")
    ap.add_argument("new", nargs="?", type=Path, help="newer binary (or clisrc-unpacked dir)")
    ap.add_argument("-o", "--out", type=Path, help="write the Markdown report here (default: stdout)")
    ap.add_argument("--json", type=Path, help="also write the diff as JSON")
    ap.add_argument("--summary", action="store_true", help="print only the short summary")
    ap.add_argument("--auto", action="store_true",
                    help="diff the live binary against the previous version once, report into the patch-test cache dir")
    args = ap.parse_args()
    if args.auto:
        try:
            sys.exit(auto())
        except Exception as exc:  # stdout: the session should hear that the check itself broke
            print(f"CLI surface diff (--auto) crashed: {type(exc).__name__}: {exc}")
            sys.exit(0)
    if not (args.old and args.new):
        ap.error("OLD and NEW are required (or use --auto)")
    diffs, ol, nl, secs = run(args.old, args.new)
    text = render(diffs, ol, nl, secs)
    if args.json:
        args.json.write_text(json.dumps([asdict(d) for d in diffs], indent=1), encoding="utf-8")
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    if args.summary or args.out:
        print(summary(diffs, ol, nl, args.out))
    else:
        print(text)
    sys.exit(1 if any(d.errors for d in diffs) else 0)


if __name__ == "__main__":
    main()
