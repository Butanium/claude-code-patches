"""Plumbing shared by the behavior tests: a throwaway Claude Code config, the
binary under test, and two ways to observe what it did.

Every test starts a FRESH process of an explicit binary path (never the
`claude` on PATH), inside a sandbox that shares nothing with your real config
except a copy of your login:

    <tmp>/cli-patch-test-<name>-XXXX/
        config/   CLAUDE_CONFIG_DIR: credentials copy, seeded .claude.json
                  (onboarding done, cwd trusted, no MCP servers, no projects),
                  settings.json with no hooks and DISABLE_AUTOUPDATER=1
        cwd/      working directory of the session(s)
        bodies/   OTEL_LOG_RAW_API_BODIES=file:<here> -- every API request
                  body the CLI sends, as JSON. This is the ground truth for
                  "what does the model see": tool schemas, deferred tools,
                  system reminders, teammate messages as rendered to the lead.

Observables, cheapest first:
  - requests(): the dumped request bodies (tool list + schemas, messages).
  - rows():     the session transcripts (tool calls, tool results,
                attachments), teammates included.
  - panes():    for interactive tests, the tmux panes of a private tmux server
                (`tmux -L <socket>`), so nothing shows up in your `tmux ls`.

The child environment is scrubbed: every CLAUDE*/OTEL_* variable and every key
of your settings.json `env` block is dropped (a test launched from inside a
Claude session would otherwise inherit that session's settings), then
DISABLE_AUTOUPDATER=1 is set. The updater otherwise runs in any session that
does not read a settings.json carrying it, and can repoint your install.

The tmux-driven tests need tmux, or psmux >= 3.3.8 on Windows (see the README's
psmux gotchas). Credentials are read from
`<config>/.credentials.json` (Linux); where the login lives in the macOS
keychain instead, set ANTHROPIC_API_KEY and the tests use that.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

PATCHED, STOCK, INCONCLUSIVE = "patched", "stock", "inconclusive"
WINDOWS = os.name == "nt"  # tmux there is psmux (>= 3.3.8), which runs pane commands through pwsh

IDLE_MARK = "✳"  # Claude Code's pane title while idle at the prompt; braille spinner while working
WORKING_FOOTER = re.compile(r"esc to inter")  # footer hint shown only mid-turn (may be cut with …)


@dataclass
class Verdict:
    status: str  # PATCHED | STOCK | INCONCLUSIVE
    detail: str

    def to_json(self) -> dict:
        return {"status": self.status, "detail": self.detail}


# ----------------------------------------------------------------------------- user config


def real_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def real_global_json() -> Path:
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return real_config_dir() / ".claude.json"
    return Path.home() / ".claude.json"


def _user_settings_env_keys() -> set[str]:
    p = real_config_dir() / "settings.json"
    try:
        return set((json.loads(p.read_text(encoding="utf-8")).get("env") or {}).keys())
    except (OSError, ValueError):
        return set()


def login_problem(margin_s: float = 1800) -> str | None:
    """Why the copied OAuth login can't carry a suite run, or None. A sandbox
    holding an expired access token refreshes it, which rotates the refresh token
    under your real login (or, when the refresh fails, just logs the sandbox out)."""
    p = real_config_dir() / ".credentials.json"
    try:
        exp = (json.loads(p.read_text(encoding="utf-8")).get("claudeAiOauth") or {}).get("expiresAt")
    except (OSError, ValueError):
        return None
    if exp and exp / 1000 < time.time() + margin_s:
        return (f"the OAuth access token in {p} expires {time.ctime(exp / 1000)}; start `claude` "
                "once (or /login) to refresh it, then rerun")
    return None


def tmp_base() -> Path:
    """Where sandboxes (and the stock-control binary copy) go. Override with
    CLI_PATCH_TESTS_TMPDIR, e.g. to keep a 250 MB control copy off a RAM /tmp."""
    if os.environ.get("CLI_PATCH_TESTS_TMPDIR"):
        return Path(os.environ["CLI_PATCH_TESTS_TMPDIR"])
    if WINDOWS:  # %TEMP% is under the profile: sessions would load ~/.claude's CLAUDE.md and skills as a parent project's
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "cli-patch-tests"
    return Path(tempfile.gettempdir())


def binary_version(binary: Path) -> str:
    return re.sub(r"\.orig$", "", Path(binary).name)


def stock_control(binary: Path) -> Path:
    """An executable copy of `<binary>.orig` (the pristine backup the patches keep),
    for running a test against stock bytes. The backup itself is not executable on
    purpose: Claude Code's version cleanup deletes executable files in versions/."""
    binary = Path(binary)
    orig = binary.with_name(binary.name + ".orig")
    if not orig.is_file():
        raise FileNotFoundError(f"no pristine backup at {orig}")
    _sweep_dead_stock_controls()
    # Per-process name: suites for the same version overlap (after_patch.py starts one per
    # patched binary state), and each run deletes its copy when it ends.
    dest = tmp_base() / f"cli-patch-test-stock-{binary_version(binary)}-pid{os.getpid()}"
    shutil.copyfile(orig, dest)
    os.chmod(dest, 0o755)
    side = dest.with_name(dest.name + ".orig")  # tests that diff against the pristine bytes find them
    side.unlink(missing_ok=True)
    side.symlink_to(orig)
    return dest


MOD_ARM_PREFIX = "cli-patch-test-mod-"


def mod_arm(control: Path, mod_dir: Path, version: str) -> Path:
    """A launcher that runs the stock_control() copy with the plugin at `mod_dir` loaded.
    It sets CLAUDE_CODE_PLUGIN_DIRS itself because the sandbox scrubs CLAUDE* variables."""
    if WINDOWS:
        raise NotImplementedError("the mod arm's launcher is a bash script; not available on Windows")
    dest = tmp_base() / f"{MOD_ARM_PREFIX}{version}-pid{os.getpid()}"
    dest.write_text(f'#!/bin/bash\nexport CLAUDE_CODE_PLUGIN_DIRS={shlex.quote(str(mod_dir))}\n'
                    f'exec {shlex.quote(str(control))} "$@"\n')
    os.chmod(dest, 0o755)
    return dest


def is_mod_arm(binary: Path) -> bool:
    """For the few tests whose observable differs between a byte patch and its mod."""
    return Path(binary).name.startswith(MOD_ARM_PREFIX)


def drop_test_binary(dest: Path) -> None:
    """Delete a stock_control() copy or a mod_arm() launcher: a full binary copy is large,
    and /tmp may be RAM-backed."""
    for f in (dest, dest.with_name(dest.name + ".orig")):
        f.unlink(missing_ok=True)


def _sweep_dead_stock_controls() -> None:
    """Remove copies left by runs that were killed before their cleanup ran."""
    if WINDOWS:  # os.kill(pid, 0) is TerminateProcess there, not a liveness probe
        return
    for f in tmp_base().glob("cli-patch-test-*-pid*"):
        m = re.search(r"-pid(\d+)(?:\.orig)?$", f.name)
        if not m:
            continue
        try:
            os.kill(int(m.group(1)), 0)
        except ProcessLookupError:
            f.unlink(missing_ok=True)
        except OSError:  # alive, owned by another user
            pass


# ----------------------------------------------------------------------------- sandbox


class Sandbox:
    def __init__(self, binary: Path | str, name: str, *, settings: dict | None = None,
                 keep: bool | None = None, credentials: bool = True):
        self.binary = Path(binary).expanduser().absolute()
        if not self.binary.is_file():
            raise FileNotFoundError(self.binary)
        self.name = name
        self.keep = bool(os.environ.get("CLI_PATCH_TESTS_KEEP")) if keep is None else keep
        base = tmp_base()
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=f"cli-patch-test-{name}-", dir=base))
        self.config = self.root / "config"
        self.cwd = self.root / "cwd"
        self.bodies = self.root / "bodies"
        for d in (self.config, self.cwd, self.bodies):
            d.mkdir()
        self.socket = f"cli-patch-test-{os.getpid()}-{self.root.name[-8:]}"
        self._session_panes: dict[str, str] = {}
        self._has_creds = False
        self._seed(settings or {}, credentials)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _seed(self, settings: dict, credentials: bool) -> None:
        creds = real_config_dir() / ".credentials.json"
        if credentials and creds.is_file():
            shutil.copyfile(creds, self.config / ".credentials.json")
            os.chmod(self.config / ".credentials.json", 0o600)
            self._has_creds = True

        g: dict = {}
        try:
            g = json.loads(real_global_json().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            g = {"hasCompletedOnboarding": True}
        g["projects"] = {}
        g["mcpServers"] = {}
        g["remoteControlAtStartup"] = False
        g["autoUpdates"] = False
        g["promptSuggestionEnabled"] = False  # ghost text in the input box confuses send()
        self.trust(self.cwd, g)
        (self.config / ".claude.json").write_text(json.dumps(g), encoding="utf-8")
        os.chmod(self.config / ".claude.json", 0o600)

        base = {
            "env": {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                    "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false"},
            "teammateMode": "tmux",
            "remoteControlAtStartup": False,
            "skipDangerousModePermissionPrompt": True,
            "tui": "default",  # any explicit value skips the "Try the new fullscreen renderer?" dialog
        }
        for k, v in settings.items():
            if k == "env":
                base["env"].update(v)
            else:
                base[k] = v
        (self.config / "settings.json").write_text(json.dumps(base, indent=1), encoding="utf-8")

    def trust(self, cwd: Path, g: dict | None = None) -> None:
        """Pre-accept the workspace-trust dialog for `cwd`."""
        path = self.config / ".claude.json"
        doc = g if g is not None else json.loads(path.read_text(encoding="utf-8"))
        for key in {str(cwd), Path(cwd).as_posix()}:  # Windows builds look up the forward-slash form
            doc.setdefault("projects", {})[key] = {
                "hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True,
            }
        if g is None:
            path.write_text(json.dumps(doc), encoding="utf-8")

    def env(self, **extra: str) -> dict[str, str]:
        drop = _user_settings_env_keys() | {"TMUX", "TMUX_PANE"}
        if self._has_creds:
            drop |= {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
        env = {k: v for k, v in os.environ.items()
               if k not in drop and not k.startswith(("CLAUDE", "OTEL_"))}
        env.update({
            "CLAUDE_CONFIG_DIR": str(self.config),
            "DISABLE_AUTOUPDATER": "1",
            "OTEL_LOG_RAW_API_BODIES": f"file:{self.bodies}",
        })
        env.update(extra)
        return env

    def close(self) -> None:
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True)
        if self.keep:
            print(f"[kept sandbox] {self.root}")
            return
        # A session killed with its tmux server flushes its transcript on the way
        # out, recreating the tree after a first rmtree; retry until it stays gone.
        for _ in range(10):
            shutil.rmtree(self.root, ignore_errors=True)
            time.sleep(1)
            if not self.root.exists():
                break

    # --------------------------------------------------------------- claude -p

    def run_print(self, prompt: str, *args: str, model: str = "haiku", timeout: float = 300,
                  env: dict[str, str] | None = None,
                  output_format: str = "stream-json") -> subprocess.CompletedProcess:
        """One `claude -p` turn. `--verbose` is added for stream-json, which the CLI
        requires it for in print mode, and for no other format: the thinking-display
        resolver treats `json --verbose` differently from `json`."""
        cmd = [str(self.binary), "-p", prompt, "--model", model, "--output-format", output_format,
               *(["--verbose"] if output_format == "stream-json" else []), *args]
        return subprocess.run(cmd, cwd=self.cwd, env=self.env(**(env or {})), capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    # --------------------------------------------------------------- observables

    def transcripts(self) -> list[Path]:
        return sorted((self.config / "projects").rglob("*.jsonl")) if (self.config / "projects").exists() else []

    def rows(self) -> list[dict]:
        out = []
        for p in self.transcripts():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                r["_file"] = str(p)
                out.append(r)
        return out

    def requests(self) -> list[dict]:
        """Dumped request bodies in send order, each with `_meta` from index.jsonl."""
        return self._dumps("request_file")

    def responses(self) -> list[dict]:
        """Dumped response bodies (the assembled assistant message) in send order.
        They hold tool calls a transcript can miss, e.g. the last call of a
        teammate that exits as soon as it returns."""
        return self._dumps("response_file")

    def _dumps(self, key: str) -> list[dict]:
        idx = self.bodies / "index.jsonl"
        if not idx.exists():
            return []
        out = []
        for line in idx.read_text(encoding="utf-8").splitlines():
            try:
                meta = json.loads(line)
                body = json.loads((self.bodies / meta[key]).read_text(encoding="utf-8"))
            except (ValueError, KeyError, TypeError, OSError):
                continue
            body["_meta"] = meta
            out.append(body)
        return out

    def inbox_messages(self) -> list[dict]:
        out = []
        for p in (self.config / "teams").glob("*/inboxes/*.json") if (self.config / "teams").exists() else []:
            try:
                for m in json.loads(p.read_text(encoding="utf-8")):
                    m["_inbox"] = p.stem
                    out.append(m)
            except (ValueError, OSError):
                continue
        return out

    # --------------------------------------------------------------- tmux (interactive)

    def tmux(self, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.socket, *a], capture_output=True, text=True,
                              env=self.env())

    def start_interactive(self, args: list[str], *, env: dict[str, str] | None = None,
                          session: str = "lead", timeout: float = 120) -> None:
        """Launch the binary in a detached tmux session of this sandbox's private
        server and wait for the prompt. Panes a lead spawns for teammates open in
        the same server and inherit the same scrubbed environment."""
        full_env = self.env(**(env or {}))
        if WINDOWS:
            cmd = "& " + " ".join(_psq(x) for x in [str(self.binary), *args]) + "; exit"
        else:
            cmd = " ".join(_shq(x) for x in [str(self.binary), *args])
        eflags: list[str] = []
        for k in ("CLAUDE_CONFIG_DIR", "DISABLE_AUTOUPDATER", "OTEL_LOG_RAW_API_BODIES", *(env or {})):
            eflags += ["-e", f"{k}={full_env[k]}"]
        r = subprocess.run(["tmux", "-L", self.socket, "new-session", "-d", "-s", session,
                            "-x", "220", "-y", "50", "-c", str(self.cwd), *eflags, cmd],
                           capture_output=True, text=True, env=full_env)
        if r.returncode:
            raise RuntimeError(f"tmux new-session failed: {r.stderr}")
        if WINDOWS:  # off by default in psmux: pane_title would never show the idle glyph
            self.tmux("set-option", "-g", "allow-set-title", "on")
        # Address the session's first pane by id: psmux makes a teammate's split the
        # active pane, which is what a bare session target resolves to.
        if pid := self.tmux("display-message", "-p", "-t", session, "#{pane_id}").stdout.strip():
            self._session_panes[session] = pid
        t0 = time.time()
        while time.time() - t0 < timeout:
            txt = re.sub(r"\s", "", self.capture(session))
            if "Itrustthisfolder" in txt:
                self.keys("Down", "Enter", target=session)
            elif "Yes,Iaccept" in txt:
                self.keys("Down", "Enter", target=session)
            # The idle title shows before the input box accepts keys; keystrokes
            # sent in that window vanish, so give the TUI a settling margin.
            elif time.time() - t0 > 12 and self.pane_state(session) == "idle":
                return
            time.sleep(0.5)
        raise RuntimeError(f"{session}: never reached the prompt:\n{self.capture(session)[-1500:]}")

    def _pane(self, target: str) -> str:
        return self._session_panes.get(target, target)

    def capture(self, target: str = "lead") -> str:
        return self.tmux("capture-pane", "-p", "-t", self._pane(target)).stdout

    def pane_state(self, target: str = "lead") -> str:
        """idle | working | gone | unknown. The footer's "esc to interrupt" is the
        working signal; the pane title alone is not (in a detached tmux server the
        title can keep its idle glyph through a whole turn)."""
        target = self._pane(target)
        # display-message on a closed pane id exits 0 with empty output, so check existence first
        if target.startswith("%") and target not in {p["id"] for p in self.panes()}:
            return "gone"
        r = self.tmux("display-message", "-p", "-t", target, "#{pane_title}")
        if r.returncode:
            return "gone"
        title = r.stdout.strip()
        cap = self.capture(target)
        if WORKING_FOOTER.search(cap[-1200:]):
            return "working"
        # psmux locks a title set with select-pane -T, as Claude Code names teammate
        # panes, so the program's idle glyph never shows there; read the screen instead
        if title.startswith(IDLE_MARK) or (WINDOWS and input_box_idle(cap)):
            return "idle"
        if title and 0x2800 <= ord(title[0]) <= 0x28FF:
            return "working"
        return "unknown"

    def panes(self) -> list[dict]:
        fmt = "#{pane_id}\t#{pane_title}\t#{pane_current_path}\t#{pane_pid}"
        r = self.tmux("list-panes", "-a", "-F", fmt)
        out = []
        for line in r.stdout.splitlines():
            pid, title, path, ppid = (line.split("\t") + ["", "", "", ""])[:4]
            out.append({"id": pid, "title": title, "path": path, "pid": ppid})
        return out

    def send(self, text: str, target: str = "lead", tries: int = 3) -> None:
        """Type `text` and submit it, then confirm the turn started (the pane
        leaves idle). Retries: keys typed while the TUI is busy redrawing can be
        dropped, or the Enter can land before the paste is taken in."""
        target = self._pane(target)
        text = " ".join(text.split())
        probe = re.sub(r"\s", "", text[:40])
        for _ in range(tries):
            if probe not in re.sub(r"\s", "", self.capture(target)):
                self.tmux("send-keys", "-t", target, "-l", text)
                time.sleep(1.0)
            self.tmux("send-keys", "-t", target, "Enter")
            if wait_for(lambda: self.pane_state(target) == "working", timeout=20, step=0.5):
                return
        raise RuntimeError(f"{target}: prompt was not taken:\n{self.capture(target)[-1500:]}")

    def keys(self, *keys: str, target: str = "lead") -> None:
        self.tmux("send-keys", "-t", self._pane(target), *keys)

    def wait_idle(self, target: str = "lead", timeout: float = 300, min_s: float = 6) -> str:
        """Block until `target` is idle at its prompt (ignoring the first `min_s`
        seconds, so the prompt showing before the turn starts doesn't count)."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.pane_state(target)
            if st == "gone" or (st == "idle" and time.time() - t0 >= min_s):
                return st
            time.sleep(1)
        return "timeout"


def input_box_idle(capture: str) -> bool:
    """The prompt line `❯` framed by the input box's two rules."""
    lines = [l.strip() for l in capture.splitlines() if l.strip()]
    return any(lines[i].startswith("❯") and lines[i - 1].startswith("─") and lines[i + 1].startswith("─")
               for i in range(1, len(lines) - 1))


def wait_for(pred: Callable[[], object], timeout: float, step: float = 2) -> object:
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _psq(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# ----------------------------------------------------------------------------- mock Messages API


def sse_message(blocks: list[dict], stop_reason: str = "end_turn") -> bytes:
    """A complete streamed Messages API response whose content is `blocks`
    (each {"type": "text", "text": ...} or {"type": "thinking", "thinking": ...})."""
    ev = [("message_start", {"type": "message_start", "message": {
        "id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-mock",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1}}})]
    for i, b in enumerate(blocks):
        if b["type"] == "thinking":
            ev += [("content_block_start", {"type": "content_block_start", "index": i,
                                            "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": i,
                                            "delta": {"type": "thinking_delta", "thinking": b["thinking"]}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": i,
                                            "delta": {"type": "signature_delta", "signature": "bW9jaw=="}})]
        else:
            ev += [("content_block_start", {"type": "content_block_start", "index": i,
                                            "content_block": {"type": "text", "text": ""}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": i,
                                            "delta": {"type": "text_delta", "text": b["text"]}})]
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    ev += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                              "usage": {"output_tokens": 5}}),
           ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {k}\ndata: {json.dumps(v)}\n\n" for k, v in ev).encode()


class MockMessagesAPI:
    """A local stand-in for the Messages API. `reply(body) -> blocks` picks each
    streamed response; every POST body is kept in `.bodies`. Point a sandbox at
    it with env ANTHROPIC_BASE_URL=mock.url and a dummy ANTHROPIC_API_KEY
    (and credentials=False), and the session is free and deterministic."""

    def __init__(self, reply: Callable[[dict], list[dict]]):
        import http.server

        mock = self
        self.reply, self.bodies = reply, []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self._send(200, "application/json", b"{}")

            def do_HEAD(self):
                self._send(200, "application/json", b"")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                mock.bodies.append({"path": self.path, "body": body})
                if not self.path.split("?")[0].endswith("/v1/messages"):
                    return self._send(200, "application/json", b"{}")
                blocks = mock.reply(body)
                if body.get("stream"):
                    return self._send(200, "text/event-stream", sse_message(blocks))
                msg = {"id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-mock",
                       "content": [({"signature": "bW9jaw==", **b} if b["type"] == "thinking" else b) for b in blocks],
                       "stop_reason": "end_turn", "stop_sequence": None,
                       "usage": {"input_tokens": 10, "output_tokens": 5}}
                self._send(200, "application/json", json.dumps(msg).encode())

            def _send(self, code, ctype, data):
                self.send_response(code)
                self.send_header("content-type", ctype)
                self.send_header("request-id", "req_mock")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def __enter__(self) -> "MockMessagesAPI":
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()


# ----------------------------------------------------------------------------- data helpers


def walk_strings(obj) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_strings(v)


def request_text(req: dict) -> str:
    """All text the model sees in one request: system + messages (not tools)."""
    return "\n".join(walk_strings({"system": req.get("system"), "messages": req.get("messages")}))


def tool_schema(req: dict, name: str) -> dict | None:
    for t in req.get("tools") or []:
        if t.get("name") == name:
            return t
    return None


def json_objects_containing(text: str, needle: str) -> list[dict]:
    """Every JSON object embedded in `text` whose source contains `needle`."""
    dec = json.JSONDecoder()
    out, seen = [], set()
    for m in re.finditer(re.escape(needle), text):
        for start in range(m.start(), max(-1, m.start() - 20000), -1):
            if text[start] != "{":
                continue
            try:
                obj, end = dec.raw_decode(text, start)
            except ValueError:
                continue
            if end > m.start() and start not in seen:
                seen.add(start)
                out.append(obj)
            break
    return out


def tool_uses(rows: list[dict], name: str) -> list[tuple[dict, dict]]:
    """(row, tool_use block) for every call of tool `name`."""
    out = []
    for r in rows:
        content = (r.get("message") or {}).get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == name:
                    out.append((r, b))
    return out


def response_tool_uses(responses: list[dict], name: str) -> list[dict]:
    """tool_use blocks for tool `name` in dumped response bodies."""
    return [b for r in responses for b in r.get("content") or []
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == name]


def tool_result_text(rows: list[dict], tool_use_id: str) -> str | None:
    for r in rows:
        content = (r.get("message") or {}).get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == tool_use_id:
                    return "\n".join(walk_strings(b.get("content")))
    return None


# ----------------------------------------------------------------------------- shared scenarios

_memo: dict[tuple, object] = {}
_memo_locks: dict[tuple, threading.Lock] = {}
_memo_guard = threading.Lock()


def memoized(key: tuple, fn: Callable[[], object]) -> object:
    """Run `fn` once per key per process: several tests read one expensive
    scenario (e.g. one team session) when the suite runner imports them together."""
    with _memo_guard:
        lock = _memo_locks.setdefault(key, threading.Lock())
    with lock:
        if key not in _memo:
            _memo[key] = fn()
        return _memo[key]


def retry_inconclusive(fn: Callable[[], Verdict], attempts: int = 3) -> Verdict:
    """Re-run a model-driven check while it is inconclusive (the model didn't
    make the call the scenario asked for); patched/stock verdicts return at once."""
    v = fn()
    for _ in range(attempts - 1):
        if v.status != INCONCLUSIVE:
            break
        v = fn()
    return v


# ----------------------------------------------------------------------------- CLI entry for a test module


def main(run: Callable[[Path], Verdict], doc: str | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=(doc or "").strip().splitlines()[0] if doc else None)
    ap.add_argument("--binary", required=True, help="claude binary to test (explicit path)")
    ap.add_argument("--expect", choices=[PATCHED, STOCK], default=PATCHED,
                    help="exit 0 iff the observed behavior matches (use stock with a pristine copy)")
    ap.add_argument("--control", action="store_true",
                    help="also run against an executable copy of <binary>.orig and expect stock")
    ap.add_argument("--keep", action="store_true", help="keep the sandbox dirs for inspection")
    a = ap.parse_args()
    if a.keep:
        os.environ["CLI_PATCH_TESTS_KEEP"] = "1"
    arms = [(Path(a.binary), a.expect)]
    if a.control:
        arms.append((stock_control(Path(a.binary)), STOCK))
    ok = True
    try:
        for binary, expect in arms:
            v = run(binary)
            good = v.status == expect
            ok &= good
            print(f"{'PASS' if good else 'FAIL'}  {binary}  expected={expect} observed={v.status}  {v.detail}")
    finally:
        if a.control:
            drop_test_binary(arms[1][0])
    return 0 if ok else 1
