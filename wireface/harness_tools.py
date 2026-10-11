"""The panel's Tools tab: the MCP servers and skills of the agent behind the face, read from that agent's own
configuration, with each server's status, a switch for each, and a catalogue of recommended ones to install.

Every harness keeps them its own way, and each is changed only its own way (what its own menus and commands write):

Claude Code  MCP servers in ``~/.claude.json`` (user: top-level ``mcpServers``; local: ``projects[<project>].mcpServers``),
             the project's ``.mcp.json``, plugins and claude.ai connectors. A server is turned off for a project in
             ``projects[<project>].disabledMcpServers``: what ``/mcp`` writes, and what the live session's
             ``toggle_mcp_server`` writes (used while the session runs, so the change applies at once). <project> is the
             git root of the working folder, or the folder itself, with forward slashes. Skills: ``~/.claude/skills``,
             ``.claude/skills`` in the working folder and the folders above it, plugins, and claude.ai's (``skills/synced``).
             A skill is turned off with ``"skillOverrides": {"<name>": "off"}`` in the project's
             ``.claude/settings.local.json``: what ``/skills`` writes. ``CLAUDE_CONFIG_DIR`` moves all of it.
Codex        ``[mcp_servers.<name>]`` in ``~/.codex/config.toml`` (``CODEX_HOME``), off with ``enabled = false``.
OpenCode     ``"mcp": {"<name>": {...}}`` in ``opencode.json`` (global and project), off with ``"enabled": false``.

Nothing secret reaches the page or the activity log: environment values and headers are never sent (only their
names), and arguments, addresses and error messages are scrubbed of anything that looks like a key or token.
Every file is written atomically, keeping keys this module doesn't know and the file's own formatting, and a copy of
it is kept in ``data/backups`` before the first change Wireface makes to it.
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from . import config

log = logging.getLogger(__name__)

CATALOG = config.ROOT / "catalog"                # what Wireface recommends: recommended.json, and skills/ of its own
CHECK_STALE_S = 600                              # a status older than this is checked again when the tab opens
CHECK_TIMEOUT_S = 120                            # the harness's own health check (it connects to every server)
HIDDEN = "****"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- keeping secrets out ---------------------------------------------------------------------------------------------
SECRET_NAME = re.compile(r"key|token|secret|passw|pwd|auth|bearer|credential|cookie|session|signature|\bpat\b|_pat$",
                         re.I)
TOKEN_PREFIX = re.compile(r"^(sk-|sk_|rk_|pk_live|pk_test|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|xox[abprs]-|AIza|"
                          r"ya29\.|npm_|hf_|eyJ[A-Za-z0-9_-]{8,}\.)")
OPAQUE = re.compile(r"^[A-Za-z0-9_\-+=.]{24,}$")
# in free text (an error message, a command line): things that are keys whatever surrounds them
TOKEN_IN_TEXT = re.compile(r"\b(sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_\w{16,}|xox[abprs]-[\w-]{10,}|"
                           r"AIza[\w-]{20,}|glpat-[\w-]{10,}|npm_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}|"
                           r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*)")
BEARER_IN_TEXT = re.compile(r"(?i)\b(bearer|basic|token)(\s+)[A-Za-z0-9._~+/=-]{8,}")
NAMED_IN_TEXT = re.compile(r"(?i)\b([\w-]*(?:api[-_]?key|apikey|token|secret|password|passwd|access[-_]?key)[\w-]*"
                           r"[\"']?\s*[=:]\s*[\"']?)(?!\*{4})([^\s\"'&,;]{4,})")


def _secretish(value: str) -> bool:
    """A value that looks like a key or token on its own (not a path, a package or a word)."""
    v = value.strip().strip("\"'")
    if TOKEN_PREFIX.match(v):
        return True
    return bool(OPAQUE.match(v) and re.search(r"\d", v) and re.search(r"[A-Za-z]", v))


def redact_url(url: str) -> str:
    """An address without its password, query values or token-like path parts."""
    try:
        u = urlsplit(url)
    except ValueError:
        return HIDDEN
    if not u.scheme or not u.netloc:
        return url
    host = u.hostname or ""
    if u.port:
        host = f"{host}:{u.port}"
    if u.username or u.password:
        host = f"{HIDDEN}@{host}"
    path = "/".join(HIDDEN if _secretish(p) else p for p in u.path.split("/"))
    query = "&".join(f"{k}={HIDDEN}" if v else k for k, v in parse_qsl(u.query, keep_blank_values=True))
    return urlunsplit((u.scheme, host, path, query, ""))


def redact_args(args: list) -> list[str]:
    """Command line arguments with anything secret replaced: --token=x, --api-key x, KEY=x, "Authorization: Bearer x",
    bare keys and tokens, and the query part of addresses."""
    out: list[str] = []
    prev = ""
    for raw in args:
        arg = str(raw)
        m = re.match(r"^(--?[\w.-]+)=(.*)$", arg, re.S)
        h = re.match(r"^([\w-]+)\s*:\s*(.+)$", arg, re.S)
        kv = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.+)$", arg, re.S)
        if m and (SECRET_NAME.search(m.group(1)) or _secretish(m.group(2))):
            arg = f"{m.group(1)}={HIDDEN}"
        elif kv and (SECRET_NAME.search(kv.group(1)) or _secretish(kv.group(2))):
            arg = f"{kv.group(1)}={HIDDEN}"
        elif h and not arg.startswith(("http:", "https:")) and (SECRET_NAME.search(h.group(1)) or _secretish(h.group(2))):
            arg = f"{h.group(1)}: {HIDDEN}"
        elif prev.startswith("-") and SECRET_NAME.search(prev) and not arg.startswith("-"):
            arg = HIDDEN
        elif re.match(r"(?i)^(bearer|basic|token)\s+\S+", arg):
            arg = f"{arg.split()[0]} {HIDDEN}"
        elif "://" in arg:
            arg = redact_url(arg) if re.match(r"^[a-z][a-z0-9+.-]*://", arg, re.I) else scrub(arg)
        elif not arg.startswith("-") and _secretish(arg):
            arg = HIDDEN
        out.append(arg)
        prev = str(raw)
    return out


def scrub(text: str | None, secrets: set[str] | frozenset = frozenset()) -> str:
    """Free text (an error, a status line) with known secret values and anything that looks like a key taken out."""
    if not text:
        return ""
    for s in sorted((s for s in secrets if s and len(s) >= 6), key=len, reverse=True):
        text = text.replace(s, "[hidden]")
    text = TOKEN_IN_TEXT.sub("[hidden]", text)
    text = BEARER_IN_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}[hidden]", text)
    text = NAMED_IN_TEXT.sub(lambda m: f"{m.group(1)}[hidden]", text)
    return re.sub(r"https?://\S+", lambda m: redact_url(m.group(0)), text)


def secret_values(cfg: dict) -> set[str]:
    """The values in a server's config that are secret whatever they look like: its environment and headers."""
    found: set[str] = set()
    for key in ("env", "environment", "headers", "http_headers", "env_http_headers"):
        block = cfg.get(key)
        if isinstance(block, dict):
            found |= {str(v) for v in block.values() if isinstance(v, (str, int)) and len(str(v)) >= 6}
    for key in ("bearer_token", "token", "apiKey", "api_key"):
        if isinstance(cfg.get(key), str) and len(cfg[key]) >= 6:
            found.add(cfg[key])
    return found


def describe_server(cfg: dict) -> dict:
    """How a server runs, safe to show: its transport, the command or address (redacted) and the names (only) of the
    environment variables and headers it is given."""
    if not isinstance(cfg, dict):
        return {"transport": "unknown", "target": "", "env": [], "program": ""}
    kind = str(cfg.get("type") or cfg.get("transport") or "").lower()
    program = ""
    url = cfg.get("url") or cfg.get("serverUrl")
    command = cfg.get("command")
    if isinstance(command, list):                        # OpenCode: the command and its arguments in one list
        command, args = (command[0] if command else ""), command[1:]
    else:
        args = cfg.get("args") or []
    if kind in ("local",):
        kind = "stdio"
    elif kind in ("remote", "streamable-http", "streamable_http"):
        kind = "http"
    if not kind:
        kind = "http" if url else "stdio" if command else "unknown"
    if url and kind != "stdio":
        target = redact_url(str(url))
    elif command:
        parts = [str(command), *[str(a) for a in args]] if isinstance(args, list) else [str(command)]
        if len(parts) > 2 and Path(parts[0]).stem.lower() == "cmd" and parts[1].lower() == "/c":
            parts = parts[2:]                            # Windows' wrapper for npx: what it runs is what matters
        first = program = parts[0]
        if ("\\" in first or "/" in first) and len(first) > 24:
            first = Path(first.replace("\\", "/")).name  # a long path to an executable: its name says enough
        target = " ".join([first, *redact_args(parts[1:])])
    else:
        target = ""
    names: list[str] = []
    for key in ("env", "environment", "headers", "http_headers", "env_http_headers"):
        if isinstance(cfg.get(key), dict):
            names += [str(k) for k in cfg[key]]
    if isinstance(cfg.get("env_vars"), list):            # Codex: names, or {name, source}
        names += [str(k.get("name", "")) if isinstance(k, dict) else str(k) for k in cfg["env_vars"]]
    if cfg.get("bearer_token_env_var"):
        names.append(str(cfg["bearer_token_env_var"]))
    return {"transport": kind, "target": target[:400], "env": sorted(set(names)), "program": program}


def missing_program(program: str) -> str:
    """Why a local server can't start, if the program it runs isn't on this PC ("" if it is, or can't be told)."""
    if not program or program.startswith("$"):
        return ""
    p = Path(program)
    if p.is_absolute() or "\\" in program or "/" in program:
        return "" if p.exists() else f"Can't find {program} on this PC"
    return "" if shutil.which(program) else f"Can't find {program} on this PC (is it installed, and on the PATH?)"


CHROME_PATHS = [Path(os.environ.get(v, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
                for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if os.environ.get(v)]


def requirement_met(need: dict) -> bool | None:
    """Whether one of a catalogue item's requirements is there: a program on the PATH, an environment variable (only
    whether it is set: its value is never read out), or Chrome. None: can't be told."""
    if need.get("command"):
        return bool(shutil.which(need["command"]))
    if need.get("env"):
        from .credentials import env_value
        return bool(env_value(need["env"]))
    if need.get("chrome"):
        return any(p.is_file() for p in CHROME_PATHS) if sys.platform == "win32" else bool(
            shutil.which("google-chrome") or shutil.which("chrome"))
    return None


# --- reading and writing the harnesses' files ----------------------------------------------------------------------
class ConfigError(ValueError):
    """A harness's file can't be read or changed: the message says which and why, for the panel."""


def read_text(path: Path) -> str | None:
    """A file's text as it is, line endings included (None if there is no file)."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"Can't read {path}: {e}") from None


def read_json(path: Path, jsonc: bool = False) -> dict | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(strip_jsonc(text) if jsonc else text)
    except ValueError as e:
        raise ConfigError(f"{path} isn't valid JSON ({e}). Fix the file, then refresh.") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} doesn't hold a JSON object.")
    return data


def strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas (OpenCode's .jsonc) as plain JSON, for reading."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _newline(text: str | None) -> str:
    return "\r\n" if text and "\r\n" in text else "\n"


def json_text(data: dict, like: str | None) -> str:
    """``data`` formatted the way the file was (indent, line endings, final newline); new files: two spaces."""
    indent: int | str = 2
    if like:
        m = re.search(r"\n([ \t]+)\S", like)
        if m:
            indent = m.group(1) if "\t" in m.group(1) else len(m.group(1))
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    if like is None or like.endswith("\n"):
        text += "\n"
    return text.replace("\n", _newline(like)) if like and "\r\n" in like else text


class Writer:
    """Writes the harnesses' files: atomically (a temporary file in the same folder, then a replace), and only after
    a copy of the file as it was before Wireface first changed it is in ``backups`` (kept, never overwritten)."""

    def __init__(self, backups: Path):
        self.backups = backups

    def backup_path(self, path: Path) -> Path:
        tag = hashlib.sha1(str(path.resolve()).lower().encode()).hexdigest()[:8]
        return self.backups / f"{path.name}-{tag}.orig"

    def write(self, path: Path, text: str) -> Path | None:
        """Returns the backup made now (None: there was one already, or the file is new)."""
        made = None
        if path.exists():
            copy = self.backup_path(path)
            if not copy.exists():
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, copy)
                made = copy
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):                     # another program may have it open for a moment (Windows)
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.1 * (attempt + 1))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return made

    def update_json(self, path: Path, change, jsonc: bool = False) -> Path | None:
        """Read the file fresh, apply ``change(data)`` and write it back (only if something changed)."""
        before = read_text(path)
        if before is not None and jsonc and strip_jsonc(before) != before:
            raise ConfigError(f"{path} has comments, which would be lost: change it by hand, or ask the agent to.")
        data = read_json(path, jsonc) if before is not None else {}
        after = json.loads(json.dumps(data))
        change(after)
        if after == data and before is not None:
            return None
        return self.write(path, json_text(after, before))


# --- what the panel shows ------------------------------------------------------------------------------------------
@dataclass
class Server:
    name: str
    scope: str                          # user, local, project, plugin, claude.ai, managed, wireface, global
    transport: str = "unknown"
    target: str = ""
    env: list = field(default_factory=list)
    enabled: bool = True
    status: str = "unknown"             # connected, failed, needs-auth, pending, disabled, unknown, builtin
    detail: str = ""
    tools: int | None = None
    source: str = ""                    # the file it's defined in (or what provides it)
    can_toggle: bool = True
    why: str = ""                       # why it can't be switched here, or how it is
    setting: str | None = None          # a Wireface setting that switches it (backend.tools.<x>)
    description: str = ""               # what it does, when known (the face's own)
    tool_names: list = field(default_factory=list)    # its tools, when the agent says (a running Claude Code)
    program: str = ""                   # what a local server runs (the panel isn't sent it: see missing)
    missing: str = ""                   # why it can't start: its program isn't on this PC
    writes: str = ""                    # the file its switch changes
    how: str = ""                       # and what in it

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.name}"

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("program")
        return {**d, "key": self.key}


@dataclass
class Skill:
    name: str
    scope: str                          # user, project, plugin, claude.ai, global
    description: str = ""
    path: str = ""
    enabled: bool = True
    state: str = "on"                   # Claude Code: on, off, name-only, user-invocable-only
    can_toggle: bool = True
    why: str = ""
    writes: str = ""                    # the file its switch changes
    how: str = ""                       # and what in it

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.name}"

    def to_json(self) -> dict:
        return {**asdict(self), "key": self.key}


def frontmatter(path: Path) -> dict:
    """The name and description of a SKILL.md (its YAML front matter, read simply: ``key: value`` lines)."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            head = f.read(16_000)
    except OSError:
        return {}
    m = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*(\r?\n|$)", head, re.S)
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$", line)
        if kv:
            key, value = kv.group(1), kv.group(2).strip()
            out[key] = "" if value in ("|", ">", "|-", ">-") else value.strip("\"'")
        elif key and line.startswith((" ", "\t")):        # a folded or continued value
            out[key] = f"{out[key]} {line.strip()}".strip()
    return out


def find_skills(folder: Path, scope: str, prefix: str = "") -> list[Skill]:
    """Every ``<folder>/<name>/SKILL.md`` (a folder may be a link; a broken one is skipped)."""
    found = []
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for d in entries:
        md = d / "SKILL.md"
        try:
            if not md.is_file():
                continue
        except OSError:
            continue
        fm = frontmatter(md)
        found.append(Skill(name=prefix + (fm.get("name") or d.name), scope=scope, description=fm.get("description", "")[:600],
                           path=str(d)))
    return found


def skill_files(folder: Path, depth: int = 4, limit: int = 400) -> list[Path]:
    """Every SKILL.md under ``folder``, following links (a broken one is skipped), a few levels deep."""
    out: list[Path] = []
    if not folder.is_dir():
        return out
    base = len(folder.parts)
    for root, dirs, files in os.walk(folder, followlinks=True, onerror=lambda e: None):
        if len(Path(root).parts) - base >= depth:
            dirs[:] = []
        dirs.sort(key=str.lower)
        if "SKILL.md" in files:
            out.append(Path(root) / "SKILL.md")
            if len(out) >= limit:
                break
    return out


def up_to_home(folder: Path, home: Path) -> list[Path]:
    """``folder`` and the folders above it, stopping below the home folder (whose skills are the user's own)."""
    stops = set()
    for h in (home, Path.home()):
        try:
            stops.add(os.path.normcase(str(h.resolve())))
        except OSError:
            pass
    out = []
    for f in [folder, *folder.parents]:
        try:
            if os.path.normcase(str(f.resolve())) in stops:
                break
        except OSError:
            pass
        out.append(f)
    return out


def git_root(folder: Path) -> Path | None:
    for p in [folder, *folder.parents]:
        if (p / ".git").exists():
            return p
    return None


# --- Claude Code ---------------------------------------------------------------------------------------------------
class ClaudeCode:
    """Claude Code's MCP servers and skills, for one working folder."""

    type, label = "claude", "Claude Code"
    WIREFACE = {                         # the servers Wireface itself gives Claude Code (backends/claude.py)
        "face": ("The face's expressions, going away and coming back, and drawing itself a new face.", None),
        "computer": ("Screenshots, mouse and keyboard on this PC.", "computer"),
        "browser": ("A Chrome window of its own (Playwright).", "browser"),
        "claude-in-chrome": ("Your own Chrome (Claude in Chrome).", "chrome"),
        "phone": ("Texts and calls from the face's phone line.", None),
    }

    def __init__(self, cwd: str, home: Path | None = None, env: dict | None = None):
        self.cwd = Path(cwd)
        self.home = Path(home) if home else Path.home()
        self.env = os.environ if env is None else env
        given = self.env.get("CLAUDE_CONFIG_DIR")
        self.dir = Path(given) if given else self.home / ".claude"
        self.global_file = (Path(given) if given else self.home) / ".claude.json"
        root = git_root(self.cwd)
        self.project = root or self.cwd
        self.project_key = os.path.abspath(str(self.project)).replace("\\", "/")
        self.local_settings = self.project / ".claude" / "settings.local.json"
        self.errors: list[str] = []
        self.secrets: set[str] = set()
        self._global: dict | None = None

    # --- reading ---
    def _read(self, path: Path, jsonc: bool = False) -> dict:
        try:
            return read_json(path, jsonc) or {}
        except ConfigError as e:
            self.errors.append(str(e))
            return {}

    def global_config(self) -> dict:
        if self._global is None:
            self._global = self._read(self.global_file)
        return self._global

    def project_entry(self, data: dict | None = None) -> tuple[str, dict]:
        """The project's entry in ~/.claude.json (keys are compared without regard to case or slashes)."""
        projects = (data if data is not None else self.global_config()).get("projects") or {}
        want = os.path.normcase(self.project_key.replace("/", "\\"))
        for key, entry in projects.items():
            if os.path.normcase(str(key).replace("/", "\\")) == want and isinstance(entry, dict):
                return key, entry
        return self.project_key, {}

    def settings(self) -> list[tuple[str, Path, dict]]:
        """The settings files, lowest precedence first."""
        files = [("user", self.dir / "settings.json"), ("project", self.project / ".claude" / "settings.json"),
                 ("local", self.local_settings)]
        return [(scope, path, self._read(path)) for scope, path in files]

    def files(self) -> list[dict]:
        return [{"label": "MCP servers (user and local)", "path": str(self.global_file), "exists": self.global_file.is_file()},
                {"label": "MCP servers (project)", "path": str(self.project / ".mcp.json"),
                 "exists": (self.project / ".mcp.json").is_file()},
                {"label": "Skills (yours)", "path": str(self.dir / "skills"), "exists": (self.dir / "skills").is_dir()},
                {"label": "Skill switches (this folder)", "path": str(self.local_settings), "exists": self.local_settings.is_file()}]

    def plugins(self) -> list[tuple[str, Path]]:
        """Installed plugins that are on: (name@marketplace, folder)."""
        enabled: dict = {}
        for _, _, s in self.settings():
            if isinstance(s.get("enabledPlugins"), dict):
                enabled.update(s["enabledPlugins"])
        record = self._read(self.dir / "plugins" / "installed_plugins.json")
        installed = record.get("plugins") if isinstance(record.get("plugins"), dict) else {}
        out = []
        for pid, installs in installed.items():
            if not enabled.get(pid):
                continue
            for inst in installs if isinstance(installs, list) else [installs]:
                path = inst.get("installPath") if isinstance(inst, dict) else None
                if path and Path(path).is_dir():
                    out.append((pid, Path(path)))
                    break
        return out

    def servers(self) -> list[Server]:
        g = self.global_config()
        _, entry = self.project_entry()
        off = set(entry.get("disabledMcpServers") or [])
        out: list[Server] = []

        def add(name, cfg, scope, source, **kw):
            if not isinstance(cfg, dict):
                return
            self.secrets |= secret_values(cfg)
            d = describe_server(cfg)
            on = kw.pop("enabled", True) and name not in off
            out.append(Server(name=name, scope=scope, source=source, enabled=on, transport=d["transport"],
                              target=d["target"], env=d["env"], program=d["program"], writes=str(self.global_file),
                              how="disabledMcpServers (this project's entry)", **kw))

        for name, cfg in (entry.get("mcpServers") or {}).items():
            add(name, cfg, "local", str(self.global_file))
        mcp_json = self.project / ".mcp.json"
        project = self._read(mcp_json).get("mcpServers") or {}
        rejected = set(entry.get("disabledMcpjsonServers") or [])
        for _, _, s in self.settings():
            rejected |= set(s.get("disabledMcpjsonServers") or [])
        for name, cfg in project.items():
            add(name, cfg, "project", str(mcp_json), enabled=name not in rejected,
                **({"can_toggle": False, "why": "Rejected in Claude Code's settings (disabledMcpjsonServers)."}
                   if name in rejected else {}))
        for name, cfg in (g.get("mcpServers") or {}).items():
            add(name, cfg, "user", str(self.global_file))
        for pid, folder in self.plugins():
            spec = self._read(folder / ".mcp.json")
            servers = spec.get("mcpServers", spec) if isinstance(spec, dict) else {}
            meta = self._read(folder / ".claude-plugin" / "plugin.json")
            if isinstance(meta.get("mcpServers"), dict):
                servers = {**servers, **meta["mcpServers"]}
            for name, cfg in servers.items():
                add(f"plugin:{pid.split('@')[0]}:{name}", cfg, "plugin", pid)
        seen = set()
        unique = []
        for s in out:                                    # the same name in two scopes: the first (highest) wins
            if s.name not in seen:
                seen.add(s.name)
                unique.append(s)
        return unique

    def skills(self) -> list[Skill]:
        user_dir = self.dir / "skills"
        found = find_skills(user_dir, "user")
        try:
            user_real = user_dir.resolve()
        except OSError:
            user_real = user_dir
        for folder in up_to_home(self.cwd, self.home):   # the working folder's .claude/skills, and its parents'
            d = folder / ".claude" / "skills"
            try:
                if d.is_dir() and d.resolve() != user_real:
                    found += find_skills(d, "project")
            except OSError:
                pass
        for pid, folder in self.plugins():
            for s in find_skills(folder / "skills", "plugin", prefix=pid.split("@")[0] + ":"):
                s.can_toggle, s.why = False, f"Comes with the plugin {pid}: turn the plugin off with /plugin."
                found.append(s)
        for bucket in sorted((user_dir / "synced").glob("*/")) if (user_dir / "synced").is_dir() else []:
            for s in find_skills(bucket, "claude.ai"):
                s.can_toggle, s.why = False, "Synced from claude.ai: turn it off in claude.ai's settings."
                found.append(s)
        overrides: dict = {}
        for _, _, s in self.settings():
            if isinstance(s.get("skillOverrides"), dict):
                overrides.update(s["skillOverrides"])
        for s in found:
            state = overrides.get(s.name, "on")
            s.state = state if state in ("on", "off", "name-only", "user-invocable-only") else "on"
            s.enabled = s.state != "off"
            if s.can_toggle:
                s.writes, s.how = str(self.local_settings), "skillOverrides"
        seen, unique = set(), []
        for s in found:
            if s.key not in seen:
                seen.add(s.key)
                unique.append(s)
        return unique

    # --- changing ---
    def set_server(self, writer: Writer, server: Server, enabled: bool) -> Path | None:
        """On or off for this project, as /mcp does: the server's name in or out of disabledMcpServers."""
        def change(data):
            projects = data.setdefault("projects", {})
            key, _ = self.project_entry(data)
            entry = projects.setdefault(key, {})
            names = [n for n in entry.get("disabledMcpServers") or [] if n != server.name]
            if not enabled:
                names.append(server.name)
            entry["disabledMcpServers"] = names

        self._global = None
        return writer.update_json(self.global_file, change)

    def set_skill(self, writer: Writer, skill: Skill, enabled: bool) -> Path | None:
        """On or off in this folder, as /skills does: skillOverrides in the project's .claude/settings.local.json.
        Turning one on removes the "off"; if a wider settings file turns it off, "on" here overrides that."""
        others = {}
        for scope, _, s in self.settings():
            if scope != "local" and isinstance(s.get("skillOverrides"), dict):
                others.update(s["skillOverrides"])

        def change(data):
            o = data.get("skillOverrides") if isinstance(data.get("skillOverrides"), dict) else {}
            if enabled:
                o.pop(skill.name, None)
                if others.get(skill.name, "on") == "off":
                    o[skill.name] = "on"
            else:
                o[skill.name] = "off"
            if o:
                data["skillOverrides"] = o
            else:
                data.pop("skillOverrides", None)

        return writer.update_json(self.local_settings, change)

    def skills_home(self) -> Path:
        return self.dir / "skills"


CLAUDE_LIST_STATUS = (            # `claude mcp list`: the words after " - " -> a status
    ("needs auth", "needs-auth"), ("failed", "failed"), ("pending approval", "pending"), ("rejected", "disabled"),
    ("disabled", "disabled"), ("connected", "connected"), ("connects on first use", "connected"),
)


def parse_claude_list(text: str, names: list[str]) -> dict[str, tuple[str, str]]:
    """``claude mcp list`` output -> {name: (status, detail)}. Lines are "name: command or address - mark Status"."""
    out = {}
    known = sorted(names, key=len, reverse=True)
    for line in text.splitlines():
        line = line.strip()
        if " - " not in line or ": " not in line:
            continue
        name = next((n for n in known if line.startswith(n + ": ")), None) or line.split(": ", 1)[0]
        said = line.rsplit(" - ", 1)[1]
        words = re.sub(r"^[^\w(]+", "", said).strip()
        status = next((s for k, s in CLAUDE_LIST_STATUS if k in words.lower()), "unknown")
        detail = "" if status == "connected" and words.lower() == "connected" else words
        if status == "pending":
            detail = "Not approved in the claude command line yet (the face's own session loads it)"
            status = "unknown"
        out[name] = (status, scrub(detail)[:300])
    return out


# --- Codex -------------------------------------------------------------------------------------------------------------
TOML_TABLE = re.compile(r"^[ \t]*\[(?!\[)(.*?)\][ \t]*(#.*)?$")
TOML_ARRAY = re.compile(r"^[ \t]*\[\[(.*?)\]\][ \t]*(#.*)?$")


def toml_key(text: str) -> list[str] | None:
    """A dotted TOML key (``mcp_servers."my server"``) as its parts; None if it isn't one."""
    parts, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            break
        if text[i] == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                return None
            parts.append("".join(buf))
            i = j + 1
        elif text[i] == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            parts.append(text[i + 1:j])
            i = j + 1
        else:
            m = re.match(r"[A-Za-z0-9_-]+", text[i:])
            if not m:
                return None
            parts.append(m.group(0))
            i += m.end()
        while i < n and text[i] in " \t":
            i += 1
        if i < n:
            if text[i] != ".":
                return None
            i += 1
    return parts or None


def toml_headers(lines: list[str]) -> list[tuple[int, str, list[str]]]:
    """The table headers in a TOML file: (line index, "table" or "array", key parts). Lines inside multi-line strings
    are skipped."""
    out, in_string = [], None
    for i, line in enumerate(lines):
        if in_string:
            if line.count(in_string) % 2 == 1:
                in_string = None
            continue
        for quote in ('"""', "'''"):
            if line.count(quote) % 2 == 1:
                in_string = quote
        if in_string:
            continue
        for kind, rx in (("array", TOML_ARRAY), ("table", TOML_TABLE)):
            m = rx.match(line)
            if m:
                key = toml_key(m.group(1))
                if key:
                    out.append((i, kind, key))
                break
    return out


def _lines(text: str) -> tuple[list[str], str]:
    nl = _newline(text)
    return text.split(nl), nl


def toml_set_enabled(text: str, table: list[str], enabled: bool) -> str:
    """``enabled = true / false`` in the table ``[a.b]``, changed in place or added under its header: every other line
    stays as it was. Raises ConfigError if the table isn't written as one (an inline table, dotted keys)."""
    lines, nl = _lines(text)
    heads = toml_headers(lines)
    for n, (i, kind, key) in enumerate(heads):
        if kind == "table" and key == table:
            end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
            for j in range(i + 1, end):
                m = re.match(r"^([ \t]*)enabled[ \t]*=[ \t]*(true|false)([ \t]*(#.*)?)$", lines[j])
                if m:
                    lines[j] = f"{m.group(1)}enabled = {'true' if enabled else 'false'}{m.group(3)}"
                    return nl.join(lines)
            if enabled:
                return text                              # on by default: nothing to say
            lines.insert(i + 1, "enabled = false")
            return nl.join(lines)
    raise ConfigError(f"[{'.'.join(table)}] isn't written as a table of its own, so Wireface won't edit it: add "
                      f"\"enabled = {'true' if enabled else 'false'}\" to it by hand.")


def toml_verify(before: str, after: str, expect) -> None:
    """The edited TOML must parse, and differ from the original only as ``expect(data)`` changes it."""
    try:
        want = tomllib.loads(before)
        expect(want)
        got = tomllib.loads(after)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"The change didn't come out as valid TOML ({e}), so nothing was written.") from None
    if got != want:
        raise ConfigError("The change would have altered more than the one setting, so nothing was written.")


def _upto_project(cwd: Path, project: Path) -> list[Path]:
    """The working folder and the folders above it, up to the project root (the root first)."""
    folders = [cwd, *cwd.parents]
    folders = folders[:folders.index(project) + 1] if project in folders else [cwd]
    return list(reversed(folders))


class Codex:
    """Codex's MCP servers (``[mcp_servers.*]`` in its config files) and skills (SKILL.md folders)."""

    type, label = "codex", "Codex"

    def __init__(self, cwd: str, home: Path | None = None, env: dict | None = None):
        self.cwd = Path(cwd)
        self.home = Path(home) if home else Path.home()
        self.env = os.environ if env is None else env
        self.dir = Path(self.env.get("CODEX_HOME") or self.home / ".codex")
        self.config_file = self.dir / "config.toml"
        self.project = git_root(self.cwd) or self.cwd
        self.errors: list[str] = []
        self.secrets: set[str] = set()

    def _toml(self, path: Path) -> dict:
        try:
            text = read_text(path)
            return tomllib.loads(text) if text else {}
        except ConfigError as e:
            self.errors.append(str(e))
        except tomllib.TOMLDecodeError as e:
            self.errors.append(f"{path} isn't valid TOML ({e}). Fix the file, then refresh.")
        return {}

    def layers(self) -> list[tuple[str, Path]]:
        """Its config files, lowest precedence first: the user's, then .codex/config.toml from the project root down
        to the working folder (Codex reads those only in folders it trusts)."""
        return [("user", self.config_file)] + [("project", f / ".codex" / "config.toml")
                                               for f in _upto_project(self.cwd, self.project)
                                               if (f / ".codex" / "config.toml").is_file()]

    def files(self) -> list[dict]:
        skills = self.home / ".agents" / "skills"
        return [{"label": "MCP servers and skill switches", "path": str(self.config_file), "exists": self.config_file.is_file()},
                {"label": "Skills (yours)", "path": str(skills), "exists": skills.is_dir()}]

    def servers(self) -> list[Server]:
        merged: dict[str, dict] = {}
        source: dict[str, tuple[str, Path]] = {}
        for scope, path in self.layers():
            for name, cfg in (self._toml(path).get("mcp_servers") or {}).items():
                if isinstance(cfg, dict):
                    merged[name] = {**merged.get(name, {}), **cfg}
                    source[name] = (scope, path)
        out = []
        for name, cfg in merged.items():
            self.secrets |= secret_values(cfg)
            d = describe_server(cfg)
            scope, path = source[name]
            out.append(Server(name=name, scope=scope, source=str(path), enabled=cfg.get("enabled", True) is not False,
                              transport=d["transport"], target=d["target"], env=d["env"], program=d["program"],
                              writes=str(path), how=f"enabled, in [mcp_servers.{name}]"))
        return out

    def skill_dirs(self) -> list[tuple[str, Path]]:
        dirs = [("user", self.home / ".agents" / "skills"), ("user", self.dir / "skills"),
                ("system", self.dir / "skills" / ".system"), ("project", self.project / ".codex" / "skills")]
        return dirs + [("project", f / ".agents" / "skills") for f in _upto_project(self.cwd, self.project)]

    def skills(self) -> list[Skill]:
        found, seen = [], set()
        for scope, d in self.skill_dirs():
            for s in find_skills(d, scope):
                real = os.path.normcase(os.path.realpath(s.path))
                if real not in seen:
                    seen.add(real)
                    found.append(s)
        rules = [r for r in ((self._toml(self.config_file).get("skills") or {}).get("config") or []) if isinstance(r, dict)]
        for s in found:
            if any(self._matches(r, s) and r.get("enabled") is False for r in rules):
                s.enabled, s.state = False, "off"
            s.writes, s.how = str(self.config_file), "a [[skills.config]] entry"
        return found

    @staticmethod
    def _matches(rule: dict, skill: Skill) -> bool:
        """A [[skills.config]] entry is for this skill: by name, or by the path of its SKILL.md (or its folder)."""
        if rule.get("name") is not None and rule.get("path") is None:
            return rule["name"] == skill.name
        if rule.get("path") is None:
            return False
        p = Path(str(rule["path"]))
        p = p.parent if p.name.upper() == "SKILL.MD" else p
        return os.path.normcase(os.path.realpath(p)) == os.path.normcase(os.path.realpath(skill.path))

    def set_server(self, writer: Writer, server: Server, enabled: bool) -> Path | None:
        """``enabled`` in the server's table, in the file that defines it (Codex's documented switch)."""
        path = Path(server.source)
        before = read_text(path) or ""
        after = toml_set_enabled(before, ["mcp_servers", server.name], enabled)
        if after == before:
            return None

        def expect(data):
            data["mcp_servers"][server.name]["enabled"] = enabled
        toml_verify(before, after, expect)
        return writer.write(path, after)

    def set_skill(self, writer: Writer, skill: Skill, enabled: bool) -> Path | None:
        """Off: a ``[[skills.config]]`` entry with the SKILL.md's path and ``enabled = false`` in the user config
        (what Codex's /skills writes); on: that entry taken out again (as /skills does)."""
        path = self.config_file
        before = read_text(path) or ""
        lines, nl = _lines(before)
        heads = toml_headers(lines)
        blocks = []                                      # this skill's [[skills.config]] entries: (start, end)
        for n, (i, kind, key) in enumerate(heads):
            if kind == "array" and key == ["skills", "config"]:
                end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
                try:
                    rule = tomllib.loads("\n".join(lines[i + 1:end]))
                except tomllib.TOMLDecodeError:
                    continue
                if self._matches(rule, skill):
                    blocks.append((i, end))
        rules = lambda data: (data.get("skills") or {}).get("config") or []
        if enabled:
            if not blocks:
                return None
            for start, end in reversed(blocks):
                while end > start + 1 and not lines[end - 1].strip():
                    end -= 1                             # keep the blank line before whatever follows
                del lines[start:end]
                if start > 0 and start < len(lines) and not lines[start - 1].strip() and not lines[start].strip():
                    del lines[start]                     # and don't leave two
            after = nl.join(lines)

            def expect(data):
                kept = [r for r in rules(data) if not self._matches(r, skill)]
                if kept:
                    data["skills"]["config"] = kept
                elif "skills" in data:
                    data["skills"].pop("config", None)
                    if not data["skills"]:
                        data.pop("skills")
        else:
            if any(r.get("enabled") is False for r in rules(self._toml(path)) if self._matches(r, skill)):
                return None
            md = str(Path(skill.path) / "SKILL.md")
            literal = f"'{md}'" if "'" not in md else json.dumps(md)
            block = ["[[skills.config]]", f"path = {literal}", "enabled = false", ""]
            body = before.rstrip("\r\n")
            after = nl.join(([body, ""] if body else []) + block)

            def expect(data):
                data.setdefault("skills", {}).setdefault("config", []).append({"path": md, "enabled": False})
        toml_verify(before, after, expect)
        return writer.write(path, after)

    def skills_home(self) -> Path:
        return self.home / ".agents" / "skills"


def parse_codex_list(text: str) -> dict[str, dict]:
    """``codex mcp list --json`` -> {name: {status, detail, enabled, describe}}. It prints environment and header
    values in full: they are only used here to hide them."""
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=0)
    data = json.loads(text[start:])
    rows = data.get("servers", data) if isinstance(data, dict) else data
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        t = r.get("transport") if isinstance(r.get("transport"), dict) else {}
        auth = str(r.get("auth_status") or "")
        if r.get("enabled") is False:
            status, detail = "disabled", str(r.get("disabled_reason") or "")
        elif auth == "not_logged_in":
            status, detail = "needs-auth", f"Sign in with: codex mcp login {r['name']}"
        else:
            status, detail = "unknown", "Codex connects to it when it works on a request"
        out[r["name"]] = {"status": status, "detail": scrub(detail, secret_values(t)), "describe": describe_server(t)}
    return out


# --- OpenCode ----------------------------------------------------------------------------------------------------------
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class OpenCode:
    """OpenCode's MCP servers (``mcp`` in its opencode.json(c) files: v1 ``{name: {..., enabled}}``, v2
    ``{servers: {name: {..., disabled}}}``) and skills (SKILL.md folders: its own, Claude Code's and .agents')."""

    type, label = "opencode", "OpenCode"

    def __init__(self, cwd: str, home: Path | None = None, env: dict | None = None, version: tuple | None = None):
        self.cwd = Path(cwd)
        self.home = Path(home) if home else Path.home()
        self.env = os.environ if env is None else env
        if self.env.get("OPENCODE_CONFIG_DIR"):
            self.dir = Path(self.env["OPENCODE_CONFIG_DIR"])
        elif self.env.get("XDG_CONFIG_HOME"):
            self.dir = Path(self.env["XDG_CONFIG_HOME"]) / "opencode"
        else:
            self.dir = self.home / ".config" / "opencode"
        self.version = version                         # (major, minor, patch) when known: 2 and up gets v2 rules
        self.errors: list[str] = []
        self.secrets: set[str] = set()

    @property
    def v2(self) -> bool:
        return bool(self.version and self.version[0] >= 2)

    def global_file(self) -> Path:
        """The global config to change: opencode.jsonc if there is one (it wins over .json), else opencode.json."""
        jsonc = self.dir / "opencode.jsonc"
        return jsonc if jsonc.is_file() else self.dir / "opencode.json"

    def layers(self) -> list[tuple[str, Path]]:
        """Config files, lowest precedence first: global, then each folder's opencode.json(c) from the farthest up, then
        the .opencode/ ones the same way (each .opencode file beats every plain one)."""
        out = [("global", self.dir / n) for n in ("opencode.json", "opencode.jsonc")]
        folders = list(reversed([self.cwd, *self.cwd.parents]))
        out += [("project", f / n) for f in folders for n in ("opencode.json", "opencode.jsonc")]
        out += [("project", f / ".opencode" / n) for f in folders for n in ("opencode.json", "opencode.jsonc")]
        return [(s, p) for s, p in out if p.is_file()]

    def _read(self, path: Path) -> dict:
        try:
            return read_json(path, jsonc=True) or {}
        except ConfigError as e:
            self.errors.append(str(e))
            return {}

    @staticmethod
    def entries(data: dict) -> dict[str, tuple[dict, str]]:
        """The MCP servers in one config: {name: (its config, "v1" or "v2")}."""
        mcp = data.get("mcp") if isinstance(data.get("mcp"), dict) else {}
        out = {}
        if isinstance(mcp.get("servers"), dict):
            out.update({k: (v, "v2") for k, v in mcp["servers"].items() if isinstance(v, dict)})
        for k, v in mcp.items():
            if k not in ("servers", "timeout") and isinstance(v, dict) and ("type" in v or "command" in v or "url" in v):
                out.setdefault(k, (v, "v1"))
        return out

    def files(self) -> list[dict]:
        g = self.global_file()
        return [{"label": "MCP servers and skill switches", "path": str(g), "exists": g.is_file()},
                {"label": "Skills (yours)", "path": str(self.dir / "skills"), "exists": (self.dir / "skills").is_dir()}]

    def servers(self) -> list[Server]:
        found: dict[str, Server] = {}
        for scope, path in self.layers():
            for name, (cfg, shape) in self.entries(self._read(path)).items():
                self.secrets |= secret_values(cfg)
                d = describe_server(cfg)
                on = not cfg.get("disabled") if shape == "v2" else cfg.get("enabled", True) is not False
                found[name] = Server(name=name, scope=scope, source=str(path), enabled=on, transport=d["transport"],
                                     target=d["target"], env=d["env"], program=d["program"], writes=str(path),
                                     how='"disabled"' if shape == "v2" else '"enabled"')   # a later file replaces it whole
        return list(found.values())

    def skill_dirs(self) -> list[tuple[str, Path]]:
        dirs = [("global", self.dir / "skill"), ("global", self.dir / "skills"),
                ("global", self.home / ".claude" / "skills"), ("global", self.home / ".agents" / "skills")]
        for f in up_to_home(self.cwd, self.home):
            dirs += [("project", f / ".opencode" / "skill"), ("project", f / ".opencode" / "skills"),
                     ("project", f / ".claude" / "skills"), ("project", f / ".agents" / "skills")]
        return dirs

    def skills(self) -> list[Skill]:
        found, seen = [], set()
        for scope, d in self.skill_dirs():
            for md in skill_files(d):                       # any depth: its folder's name is the skill's id
                real = os.path.normcase(os.path.realpath(md.parent))
                if real in seen:
                    continue
                seen.add(real)
                fm = frontmatter(md)
                found.append(Skill(name=md.parent.name, scope=scope, description=fm.get("description", "")[:600],
                                   path=str(md.parent)))
        denied = self._denied()
        own = os.path.normcase(str(self.global_file()))
        for s in found:
            s.writes, s.how = str(self.global_file()), "a permission rule that denies it"
            rule = next((r for r in denied if fnmatch.fnmatchcase(s.name, r[0])), None)
            if rule:
                s.enabled, s.state = False, "off"
                if rule[0] != s.name or os.path.normcase(str(rule[1])) != own:
                    s.why = f"Turned off by the rule \"{rule[0]}\" in {rule[1]}."
        return found

    def _denied(self) -> list[tuple[str, Path]]:
        """Skills denied by permission rules: (name or pattern, file). v1: ``permission.skill``; v2: ``permissions``."""
        out = []
        for _, path in self.layers():
            data = self._read(path)
            perm = data.get("permission")
            if isinstance(perm, dict):
                skill = perm.get("skill")
                if skill == "deny":
                    out.append(("*", path))
                elif isinstance(skill, dict):
                    out += [(k, path) for k, v in skill.items() if v == "deny"]
            for rule in data.get("permissions") or []:
                if isinstance(rule, dict) and rule.get("action") == "skill" and rule.get("effect") == "deny":
                    out.append((str(rule.get("resource", "*")), path))
        return out

    def set_server(self, writer: Writer, server: Server, enabled: bool) -> Path | None:
        """The entry's own switch, in the file that defines it: v2 ``disabled``, v1 ``enabled``."""
        def change(data):
            cfg, shape = self.entries(data)[server.name]
            if shape == "v2":
                if enabled:
                    cfg.pop("disabled", None)
                else:
                    cfg["disabled"] = True
            else:
                cfg["enabled"] = enabled
        return writer.update_json(Path(server.source), change, jsonc=True)

    def set_skill(self, writer: Writer, skill: Skill, enabled: bool) -> Path | None:
        """Off: a rule denying the skill in the global config (v2: ``permissions``; v1: ``permission.skill``); on: that
        rule taken out. A rule in another file, or for a pattern, is left for the user to change."""
        if enabled and skill.why:
            raise ConfigError(skill.why + " Change it there.")

        def change(data):
            if enabled:
                perm = data.get("permission")
                if isinstance(perm, dict) and isinstance(perm.get("skill"), dict):
                    perm["skill"].pop(skill.name, None)
                    if not perm["skill"]:
                        perm.pop("skill")
                    if not perm:
                        data.pop("permission")
                if isinstance(data.get("permissions"), list):
                    data["permissions"] = [r for r in data["permissions"] if not (
                        isinstance(r, dict) and r.get("action") == "skill" and r.get("resource") == skill.name
                        and r.get("effect") == "deny")]
                    if not data["permissions"]:
                        data.pop("permissions")
            elif self.v2:
                data.setdefault("permissions", []).append({"action": "skill", "resource": skill.name, "effect": "deny"})
            else:
                perm = data.setdefault("permission", {})
                if not isinstance(perm.get("skill"), dict):
                    perm["skill"] = {} if perm.get("skill") in (None, "allow", "ask") else {"*": perm["skill"]}
                perm["skill"][skill.name] = "deny"
        return writer.update_json(self.global_file(), change, jsonc=True)

    def skills_home(self) -> Path:
        return self.dir / "skills"


OPENCODE_MARKS = {"✓": "connected", "✔": "connected", "⚠": "needs-auth", "✗": "failed",
                  "✘": "failed", "○": "pending"}


def parse_opencode_list(text: str, names: list[str]) -> dict[str, tuple[str, str]]:
    """``opencode mcp list`` -> {name: (status, detail)}; lines are "<mark> <name>  <status>"."""
    out = {}
    known = sorted(names, key=len, reverse=True)
    for line in ANSI.sub("", text).splitlines():
        s = line.strip()
        if not s or s.lower().startswith(("no mcp", "mcp servers")):
            continue
        mark = s[0]
        rest = s if mark.isalnum() or mark in "_-" else s[1:].strip()
        if not rest:
            continue
        name = next((n for n in known if rest == n or rest.startswith(n + " ")), None) or rest.split()[0]
        words = rest[len(name):].strip()
        low = words.lower()
        status = OPENCODE_MARKS.get(mark)
        if "disabled" in low:
            status = "disabled"
        elif status is None:
            status = ("needs-auth" if "auth" in low else "failed" if "fail" in low else "connected" if "connected" in low
                      else "pending" if "pending" in low else "unknown")
        detail = words.split(":", 1)[1].strip() if status == "failed" and ":" in words else ""
        out[name] = (status, scrub(detail)[:300])
    return out


HARNESSES = {"claude": ClaudeCode, "codex": Codex, "opencode": OpenCode}


# --- the catalogue -----------------------------------------------------------------------------------------------------
def catalogue(root: Path | None = None) -> dict:
    """What Wireface recommends: MCP servers and skills from elsewhere (recommended.json), and its own skills
    (skills/<name>/SKILL.md, installed by copying the folder)."""
    root = root or CATALOG
    try:
        data = json.loads((root / "recommended.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("no catalogue: %s", e)
        data = {}
    shown = data.get("own") or {}                    # how the panel describes them (SKILL.md's text is for the agent)
    own = [{"id": Path(s.path).name, "kind": "skill", "title": s.name.replace("-", " ").capitalize(),
            "description": s.description, **(shown.get(Path(s.path).name) or {}), "by": "Made for Wireface", "licence": "",
            "bundled": s.path}
           for s in find_skills(root / "skills", "wireface")]
    return {"mcp": [dict(x, kind="mcp") for x in data.get("mcp", [])],
            "skills": own + [dict(x, kind="skill") for x in data.get("skills", [])]}


def _quote(arg: str) -> str:
    return arg if re.fullmatch(r"[\w@%+=:,./-]+", arg) else shlex.quote(arg)


def _pkg(spec: str) -> str:
    """A package without its version: @playwright/mcp@0.0.83 -> @playwright/mcp."""
    at = spec.rfind("@")
    return spec[:at] if at > 0 else spec


def _runs(item: dict, windows: bool, wrap: bool) -> list[str]:
    """The command line that starts a local server (``cmd /c`` before npx on Windows where the agent needs it)."""
    if item.get("npx"):
        run = ["npx", "-y", item["npx"], *item.get("args", [])]
        return ["cmd", "/c", *run] if windows and wrap else run
    return ["uvx", item["uvx"], *item.get("args", [])]


def install_plan(item: dict, h, skip_permissions: bool = False, windows: bool = sys.platform == "win32") -> dict:
    """How one catalogue item gets into harness ``h``: {"by": "wireface" | "agent" | "you", "command": what is shown
    (and can be copied), "argv": what Wireface runs, "prompt": what the agent is asked, "note": when it applies}.

    Wireface's own skills: Wireface copies the folder (exact, nothing to approve). Skills from elsewhere: the agent
    fetches them from their source (Codex only when unsandboxed). MCP servers: Claude Code and OpenCode are asked to add
    them, with the exact command or entry; Codex's sandbox keeps it out of its own config, so Wireface runs ``codex mcp
    add`` itself."""
    home = getattr(h, "home", Path.home())
    env = {k: str(v).replace("{home}", home.as_posix()) for k, v in (item.get("env") or {}).items()}
    if item["kind"] == "skill":
        dest = h.skills_home() / item["id"]
        if item.get("bundled"):
            return {"by": "wireface", "command": f"Copy the folder {item['id']} to {dest}", "dest": str(dest),
                    "note": "Wireface copies it."}
        steps = (f"clone {item['repo']} with git (--depth 1) into a temporary folder, copy its folder {item['path']} to "
                 f"{dest.as_posix()} (with its licence file), then delete the temporary clone")
        if h.type == "codex" and not skip_permissions:
            return {"by": "you", "command": steps[0].upper() + steps[1:] + ".",
                    "note": "Codex's sandbox keeps it from writing outside the working folder, so this one is for you."}
        return {"by": "agent", "command": steps[0].upper() + steps[1:] + ".",
                "prompt": f"Please install the {item['title']} skill for me ({item['licence']} licence, from {item['repo']}): "
                          f"{steps}. Don't change anything else. Then tell me in one short sentence whether it worked.",
                "note": "The agent fetches it from its source."}
    name = item["id"]
    if h.type == "claude":
        if item.get("claude_plugin"):
            cmd = ["claude", "plugin", "install", item["claude_plugin"], "--scope", "user"]
        elif item.get("remote"):
            cmd = ["claude", "mcp", "add", "--scope", "user", "--transport", "http", name, item["remote"]]
        else:                                        # current Claude Code starts npx itself on Windows (no cmd /c)
            cmd = ["claude", "mcp", "add", "--scope", "user", name,
                   *[x for k, v in env.items() for x in ("-e", f"{k}={v}")], "--", *_runs(item, windows, False)]
        line = " ".join(_quote(a) for a in cmd)
        return {"by": "agent", "command": line,
                "prompt": f"Please add the {item['title']} MCP server to Claude Code for me, for all my projects, by running "
                          f"exactly this command: {line}  Don't change anything else. Then tell me in one short sentence "
                          f"whether it worked.",
                "note": "Claude Code loads it when its session restarts."}
    if h.type == "codex":
        if item.get("remote"):
            argv = ["codex", "mcp", "add", name, "--url", item["remote"]]
            if item.get("bearer_env"):
                argv += ["--bearer-token-env-var", item["bearer_env"]]
        else:
            argv = ["codex", "mcp", "add", name, *[x for k, v in env.items() for x in ("--env", f"{k}={v}")], "--",
                    *_runs(item, windows, True)]
        return {"by": "wireface", "argv": argv, "command": " ".join(_quote(a) for a in argv),
                "note": "Wireface runs Codex's own command. Codex uses it from your next message."}
    # OpenCode: an entry in its global config (a shape both its v1 and v2 read)
    if item.get("remote"):
        entry = {"type": "remote", "url": item["remote"], "enabled": True}
        if item.get("bearer_env"):
            entry.update(oauth=False, headers={"Authorization": f"Bearer {{env:{item['bearer_env']}}}"})
    else:
        entry = {"type": "local", "command": _runs(item, windows, True), "enabled": True, **({"environment": env} if env else {})}
    snippet = json.dumps({name: entry})[1:-1]
    target = h.global_file()
    return {"by": "agent", "command": f"Add to \"mcp\" in {target}: {snippet}",
            "prompt": f"Please add the {item['title']} MCP server to OpenCode for me. In OpenCode's global config file "
                      f"{target} (create it if it is missing, and keep everything else in it, comments included), add this "
                      f"entry under \"mcp\" (inside \"mcp.servers\" if the file already uses that): {snippet}  "
                      f"Don't change anything else. Then tell me in one short sentence whether it worked.",
            "note": "OpenCode loads it when it restarts."}


def is_installed(item: dict, servers: list[Server], skills: list[Skill]) -> str | None:
    """The key of what a catalogue item is installed as (by name, or by the package or address a server runs), or
    None if it isn't there."""
    if item["kind"] == "skill":
        return next((s.key for s in skills if s.name == item["id"] or s.name.endswith(":" + item["id"])), None)
    marks = []
    if item.get("remote"):
        u = urlsplit(item["remote"])
        marks.append(f"{u.netloc}{u.path.rstrip('/')}")
    for key in ("npx", "uvx"):
        if item.get(key):
            marks.append(_pkg(item[key]))
    return next((s.key for s in servers if s.name == item["id"] or s.name.endswith(":" + item["id"])
                 or any(m in s.target for m in marks)), None)


# --- the service the server talks to ----------------------------------------------------------------------------------
class HarnessTools:
    """The Tools tab's data and actions, for the face's current agent and working folder. ``host`` is the App
    (``backend``, ``backend_cfg``, ``activity``, ``broadcast``, ``clients``, ``credentials``). Statuses are checked in
    the background, the agent's own way, and kept per agent and folder: the panel never waits for them."""

    INSTALL_WAIT_S = 900                          # how long an install handed to the agent shows as under way

    def __init__(self, host, backups: Path | None = None, home: Path | None = None, env: dict | None = None):
        self.host = host
        self.writer = Writer(backups or config.DATA / "backups")
        self.home, self.env = home, env
        self.checks: dict[tuple, dict] = {}       # (agent, folder) -> {at, source, servers, error, running}
        self.pending: str | None = None           # changes waiting for the agent to restart
        self.installing: dict[str, float] = {}    # catalogue id -> when the agent was asked
        self._task: asyncio.Task | None = None
        self._sending: asyncio.Task | None = None
        self._again = False
        self._session = None                       # the agent's session object last seen (a new one: it restarted)
        self._pending_session = None               # the session that was running when a change was made
        self._versions: dict[str, tuple | None] = {}

    # --- which agent, where ---
    @property
    def kind(self) -> str:
        return self.host.backend.id

    def _key(self) -> tuple:
        return (self.kind, str(self.host.backend_cfg["cwd"]))

    def harness(self):
        cls = HARNESSES.get(self.kind)
        if cls is None:
            return None
        h = cls(self.host.backend_cfg["cwd"], home=self.home, env=self.env)
        if isinstance(h, OpenCode):
            h.version = self._versions.get("opencode")
        return h

    def _cli(self) -> str | None:
        """The agent's executable, found the way its backend finds it (None: not installed)."""
        b = self.host.backend
        try:
            if self.kind == "claude":
                from .backends.claude import find_cli
                return find_cli((self.host.backend_cfg.get("options") or {}).get("cli_path"))
            if self.kind == "codex":
                return b._exe()
            if self.kind == "opencode":
                return b._command()[0]
        except Exception as e:
            log.info("no %s executable: %s", self.kind, e)
        return None

    def _secrets(self, h) -> set[str]:
        creds = getattr(self.host, "credentials", None)
        own = creds.secret_values() if creds is not None else set()
        return set(own) | set(getattr(h, "secrets", set()))

    # --- what the panel shows ---
    def _wireface_servers(self) -> list[Server]:
        """The servers the face itself gives the agent (backends/claude.py; the phone line's for the others)."""
        out = []
        phone = bool(((getattr(self.host, "cfg", None) or {}).get("phone") or {}).get("enabled"))
        profile = str(getattr(getattr(self.host, "profile", None), "file", "") or "")
        if self.kind == "claude":
            tools = {"computer": True, "browser": True, "chrome": True, **(self.host.backend_cfg.get("tools") or {})}
            for name, (what, setting) in ClaudeCode.WIREFACE.items():
                if (name == "phone" and not phone) or (name == "computer" and sys.platform != "win32"):
                    continue
                on = tools.get(setting, True) is not False if setting else True
                out.append(Server(name=name, scope="wireface", transport="sdk" if name in ("face", "computer", "phone") else "stdio",
                                  description=what, enabled=on, status="builtin" if on else "disabled", source="Wireface",
                                  setting=setting, can_toggle=bool(setting),
                                  writes=profile if setting else "", how=f"backend.tools.{setting}" if setting else "",
                                  why="A Wireface setting (Agent > Using this PC); changing it restarts the agent." if setting
                                  else "Part of the face." if name == "face" else "The phone line: Settings > Agent > Phone."))
        elif phone:
            out.append(Server(name="phone", scope="wireface", transport="stdio", description="Texts and calls from the face's phone line.",
                              status="builtin", source="Wireface", can_toggle=False, why="The phone line: Settings > Agent > Phone."))
        return out

    def view(self) -> dict:
        """Everything the Tools tab shows. It reads the agent's files: call it off the event loop."""
        b = self.host.backend
        h = self.harness()
        now = getattr(b, "client", None) if getattr(b, "connected", False) else None
        if self.pending and now is not None and now is not self._pending_session:
            self.pending = None                        # it has restarted since
        base = {"harness": {"type": self.kind, "label": b.label, "supported": h is not None},
                "cwd": str(self.host.backend_cfg["cwd"]), "pending": self.pending,
                "restart": {"can": h is not None and self.kind != "codex", "connected": bool(b.connected)}}
        if h is None:
            return {**base, "servers": [], "skills": [], "recommended": [], "errors": [], "files": [], "notes": {},
                    "check": {"state": "unsupported"}}
        wf = self._wireface_servers()
        own = {s.name for s in wf}
        servers = wf + [s for s in h.servers() if s.name not in own]
        skills = h.skills()
        check = self.checks.get(self._key()) or {}
        byname = {s.name: s for s in servers}
        for name, st in (check.get("servers") or {}).items():
            s = byname.get(name)
            if s is None:                          # only the agent knows it: claude.ai connectors, plugins, managed
                d = st.get("describe") or {}
                s = byname[name] = Server(name=name, scope=st.get("scope") or "other", transport=d.get("transport", "unknown"),
                                          target=d.get("target", ""), env=d.get("env", []), source=st.get("scope") or "",
                                          can_toggle=self.kind == "claude",
                                          why="" if self.kind == "claude" else "Comes from a plugin: change it there.")
                servers.append(s)
            if s.scope == "wireface" and not s.enabled:
                continue
            s.status, s.detail, s.tools = st.get("status") or "unknown", st.get("detail") or "", st.get("tools")
            s.tool_names = st.get("tool_names") or []
            if s.status == "disabled":
                s.enabled = False
        for s in servers:
            if not s.enabled and s.status != "failed":
                s.status = "disabled"
            elif s.enabled and s.status == "disabled":
                s.status, s.detail = "unknown", "Turned on: checked again with the next refresh"
            elif s.status == "unknown" and not s.detail:
                s.detail = "Not checked yet" if not check.get("at") else ""
            if s.enabled and s.transport == "stdio" and s.scope != "wireface" and s.status != "connected":
                s.missing = missing_program(s.program)
        secrets = self._secrets(h)
        for s in servers:
            s.detail, s.target, s.missing = scrub(s.detail, secrets)[:300], scrub(s.target, secrets), scrub(s.missing, secrets)
        skip = bool(self.host.backend_cfg.get("skip_permissions"))
        cat = catalogue()
        items = []
        for item in cat["mcp"] + cat["skills"]:
            builtin = self.kind in (item.get("builtin_for") or [])
            key = f"wireface:{item['builtin_as']}" if builtin and item.get("builtin_as") else is_installed(item, servers, skills)
            installed = builtin or bool(key)
            if installed:
                self.installing.pop(item["id"], None)
            asked = self.installing.get(item["id"])
            plan = install_plan(item, h, skip)
            needs = [{"label": n.get("label", ""), "url": n.get("url"), "hint": n.get("hint", ""), "ok": requirement_met(n)}
                     for n in item.get("needs") or []]
            items.append({**{k: v for k, v in item.items() if k not in ("bundled", "needs")}, "installed": installed,
                          "builtin": builtin, "installed_key": key, "needs": needs,
                          "installing": bool(asked and time.time() - asked < self.INSTALL_WAIT_S),
                          "plan": {k: v for k, v in plan.items() if k in ("by", "command", "note", "dest")}})
        b_running = bool(getattr(b, "connected", False))
        return {**base, "servers": [s.to_json() for s in servers], "skills": [s.to_json() for s in skills],
                "recommended": items, "errors": [scrub(e, secrets) for e in h.errors], "files": h.files(),
                "running": b_running, "home": str(self.home or Path.home()),
                "check": {"state": "running" if check.get("running") else "done" if check.get("at") else "never",
                          "at": check.get("at"), "source": check.get("source"), "error": check.get("error")},
                "notes": self._notes()}

    def _notes(self) -> dict:
        if self.kind == "claude":
            live = bool(self.host.backend.connected)
            return {"servers": "A switch applies at once, and Claude Code remembers it for this folder, as /mcp does." if live
                    else "Switches are kept for this folder, as /mcp does, and apply when the session starts.",
                    "skills": "Switches are kept for this folder (in .claude/settings.local.json, as /skills does) and apply "
                              "from the next session."}
        if self.kind == "codex":
            return {"servers": "Switches go in Codex's config.toml and apply from your next message.",
                    "skills": "Switches go in Codex's config.toml, as its /skills does, and apply from your next message."}
        return {"servers": "Switches go in the OpenCode config that defines the server, and apply when OpenCode restarts.",
                "skills": "Turning a skill off adds a rule denying it to OpenCode's global config; it applies when OpenCode "
                          "restarts."}

    # --- statuses, in the background ---
    def stale(self) -> bool:
        c = self.checks.get(self._key()) or {}
        return not c.get("running") and (not c.get("at") or time.time() - c["at"] > CHECK_STALE_S)

    def refresh(self, delay: float = 0.0) -> bool:
        """Check every server's status in the background; False if a check is under way already."""
        if self._task and not self._task.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                         # not from the event loop (a test): nothing to run it on
            return False
        self._task = loop.create_task(self._check(delay))
        return True

    async def _check(self, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        key = self._key()
        old = self.checks.get(key) or {}
        self.checks[key] = {**old, "running": True}
        self.changed()
        try:
            servers, source = await self._statuses()
            self.checks[key] = {"at": time.time(), "source": source, "servers": servers, "error": None, "running": False}
        except Exception as e:
            log.warning("checking the MCP servers of %s: %s", key[0], e)
            self.checks[key] = {**old, "running": False,
                                "error": scrub(str(e))[:300] or f"{type(e).__name__} (it took too long?)"}
        self.changed()

    async def _run(self, argv: list[str], timeout: float = CHECK_TIMEOUT_S) -> str:
        cwd = str(self.host.backend_cfg["cwd"])

        def go():
            r = subprocess.run(argv, cwd=cwd if os.path.isdir(cwd) else None, capture_output=True, timeout=timeout,
                               encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
            if r.returncode != 0:
                lines = (r.stderr or r.stdout or f"exit code {r.returncode}").strip().splitlines()
                raise RuntimeError(lines[-1][:300] if lines else f"exit code {r.returncode}")
            return r.stdout
        return await asyncio.get_running_loop().run_in_executor(None, go)

    async def _statuses(self) -> tuple[dict, str]:
        b = self.host.backend
        if self.kind == "claude" and getattr(b, "connected", False) and hasattr(b, "mcp_status"):
            rows = await asyncio.wait_for(b.mcp_status(), 30)
            if rows is not None:
                out = {}
                for r in rows:
                    cfg = r.get("config") if isinstance(r.get("config"), dict) else {}
                    out[r["name"]] = {"status": r.get("status") or "unknown",
                                      "detail": scrub(r.get("error") or "", secret_values(cfg)),
                                      "tools": len(r["tools"]) if isinstance(r.get("tools"), list) else None,
                                      "tool_names": [str(t.get("name", "")) for t in r.get("tools") or []
                                                     if isinstance(t, dict)][:200],
                                      "scope": {"claudeai": "claude.ai", "dynamic": "wireface", "sdk": "wireface"}.get(
                                          r.get("scope"), r.get("scope")),
                                      "describe": describe_server(cfg)}
                return out, "the running session"
        cli = self._cli()
        if not cli:
            raise RuntimeError(f"{b.label} isn't installed, or Wireface can't find it")
        h = self.harness()
        names = [s.name for s in h.servers()]
        if self.kind == "claude":
            rows = parse_claude_list(await self._run([cli, "mcp", "list"]), names)
            scope = lambda n: None if n in names else "claude.ai" if n.startswith("claude.ai ") else \
                "plugin" if n.startswith("plugin:") else "other"
            return {n: {"status": st, "detail": d, "scope": scope(n)} for n, (st, d) in rows.items()}, "claude mcp list"
        if self.kind == "codex":
            rows = parse_codex_list(await self._run([cli, "mcp", "list", "--json"], 60))
            return {n: {**r, "scope": None if n in names else "plugin"} for n, r in rows.items()}, "codex mcp list"
        if self.kind == "opencode":
            await self._opencode_version(cli)
            rows = parse_opencode_list(await self._run([cli, "mcp", "list"], 90), names)
            return {n: {"status": st, "detail": d} for n, (st, d) in rows.items()}, "opencode mcp list"
        return {}, ""

    async def _opencode_version(self, cli: str) -> None:
        if "opencode" in self._versions:
            return
        try:
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", await self._run([cli, "--version"], 30))
            self._versions["opencode"] = tuple(int(x) for x in m.groups()) if m else None
        except Exception:
            self._versions["opencode"] = None

    # --- telling the page ---
    def changed(self) -> None:
        """Send the page the new view (worked out off the event loop), if a page is open."""
        if not getattr(self.host, "clients", None):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._sending and not self._sending.done():
            self._again = True
            return

        async def send():
            try:
                while True:
                    self._again = False
                    view = await asyncio.get_running_loop().run_in_executor(None, self.view)
                    self.host.broadcast({"type": "tools", "view": view})
                    if not self._again:
                        break
            except Exception:
                log.exception("sending the tools view")
        self._sending = asyncio.ensure_future(send())

    def on_activity(self, ev) -> None:
        """Activity listener: a session that (re)started has applied the changes and is checked again; a request
        that ended while an install was with the agent may have added something."""
        b = self.host.backend
        session = getattr(b, "client", None) if getattr(b, "connected", False) else None
        if session is not None and session is not self._session:      # a new session (a restart closes the old one)
            if session is not self._pending_session:
                self.pending = None
            if self.kind == "claude":
                self.refresh(delay=4)                  # its servers take a moment to connect
            else:
                self.changed()
        self._session = session or self._session
        if ev.kind == "turn" and ev.status != "running" and self.installing:
            self.changed()

    def _note(self, summary: str, detail: str) -> None:
        self.host.activity.emit("notice", summary=summary, output=detail)

    # --- switching ---
    def _find(self, h, key: str, what: str):
        items = (self._wireface_servers() + h.servers()) if what == "server" else h.skills()
        found = next((x for x in items if x.key == key), None)
        if found is None and what == "server" and self.kind == "claude":
            scope, _, name = key.partition(":")
            if name in ((self.checks.get(self._key()) or {}).get("servers") or {}):
                found = Server(name=name, scope=scope)    # only the agent knows it (a claude.ai connector, say)
        if found is None:
            raise ValueError("It isn't there any more: refresh the list.")
        return found

    async def set_server(self, key: str, enabled: bool) -> str:
        """Turn an MCP server on or off, the agent's own way; returns what happened, in a sentence."""
        h = self.harness()
        if h is None:
            raise ValueError("This agent's MCP servers can't be changed from here.")
        loop = asyncio.get_running_loop()
        s = await loop.run_in_executor(None, self._find, h, key, "server")
        on = "on" if enabled else "off"
        if s.setting:
            await self.host.set_config({"backend": {"tools": {s.setting: bool(enabled)}}})
            return f"{s.name} turned {on}: the agent restarts with it (the conversation continues)."
        if not s.can_toggle:
            raise ValueError(s.why or "This one can't be switched from here.")
        b = self.host.backend
        if self.kind == "claude" and getattr(b, "connected", False) and hasattr(b, "toggle_mcp"):
            await asyncio.wait_for(b.toggle_mcp(s.name, bool(enabled)), 30)   # what /mcp does: applies, and is kept
            said = "Applied now"
            self.refresh(delay=1.5)
        else:
            try:
                await loop.run_in_executor(None, h.set_server, self.writer, s, bool(enabled))
            except ConfigError as e:
                raise ValueError(str(e)) from None
            said = self._applies()
        self._note(f"Turned the MCP server {s.name} {on}", f"{h.label}, working in {self.host.backend_cfg['cwd']}. {said}.")
        self.changed()
        return f"{s.name} turned {on}. {said}."

    async def set_skill(self, key: str, enabled: bool) -> str:
        if self.kind == "opencode" and "opencode" not in self._versions and (cli := self._cli()):
            await self._opencode_version(cli)          # v1 and v2 write their rules differently
        h = self.harness()
        if h is None:
            raise ValueError("This agent's skills can't be changed from here.")
        loop = asyncio.get_running_loop()
        s = await loop.run_in_executor(None, self._find, h, key, "skill")
        if not s.can_toggle:
            raise ValueError(s.why or "This skill can't be switched from here.")
        try:
            await loop.run_in_executor(None, h.set_skill, self.writer, s, bool(enabled))
        except ConfigError as e:
            raise ValueError(str(e)) from None
        said = self._applies()
        on = "on" if enabled else "off"
        where = f", working in {self.host.backend_cfg['cwd']}" if self.kind == "claude" else ""
        self._note(f"Turned the skill {s.name} {on}", f"{h.label}{where}. {said}.")
        self.changed()
        return f"{s.name} turned {on}. {said}."

    def _applies(self) -> str:
        """When a change to the agent's files takes effect (and, while it runs, that it wants a restart)."""
        if self.kind == "codex":
            return "Codex uses it from your next message"
        b = self.host.backend
        if getattr(b, "connected", False):
            self.pending = f"Restart {b.label} to use the changes (the conversation continues)."
            self._pending_session = getattr(b, "client", None)
            return f"{b.label} picks it up when it restarts"
        return f"{b.label} picks it up when it starts"

    async def restart(self) -> None:
        """Start the agent's session again (the conversation continues), so it loads what changed."""
        if self.kind != "codex":
            self.host.backend.reconnect()

    # --- installing ---
    async def install(self, item_id: str) -> str:
        """Install a catalogue item (see install_plan); returns what happened, in a sentence."""
        h = self.harness()
        if h is None:
            raise ValueError("Wireface can't install tools for this agent.")
        cat = catalogue()
        item = next((x for x in cat["mcp"] + cat["skills"] if x["id"] == item_id), None)
        if item is None:
            raise ValueError(f"{item_id!r} isn't in the catalogue.")
        plan = install_plan(item, h, bool(self.host.backend_cfg.get("skip_permissions")))
        if plan["by"] == "you":
            raise ValueError(plan["note"])
        if plan["by"] == "agent":
            self._ask_agent(plan["prompt"])
            self.installing[item_id] = time.time()
            self.changed()
            return f"Asked {self.host.backend.label} to install {item['title']}: you can follow it in Activity."
        loop = asyncio.get_running_loop()
        if item["kind"] == "skill":
            dest = Path(plan["dest"])

            def copy():
                if dest.exists():
                    raise ValueError(f"There is a skill folder at {dest} already.")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(item["bundled"], dest)
            await loop.run_in_executor(None, copy)
            said = self._applies()
            self._note(f"Installed the skill {item['id']}", f"Copied into {dest}. {said}.")
            self.changed()
            return f"Installed {item['title']}. {said}."
        cli = self._cli()
        if not cli:
            raise ValueError(f"Can't find {self.host.backend.label} to install it with.")
        try:
            await self._run([cli, *plan["argv"][1:]], 90)
        except subprocess.TimeoutExpired:
            raise ValueError(f"It took too long: {plan['command']}") from None
        except (RuntimeError, OSError) as e:
            raise ValueError(f"That didn't work ({scrub(str(e))}): {plan['command']}") from None
        self._note(f"Installed the MCP server {item['id']}", f"Ran {plan['command']}. {plan['note']}")
        self.refresh()
        return f"Installed {item['title']}. Codex uses it from your next message."

    def _ask_agent(self, prompt: str) -> None:
        """Hand the agent an install, as if typed to it (to this face's own agent, even in a room)."""
        host = self.host
        if getattr(host, "realtime_speaks", False):
            host.realtime.typed(prompt)
        host.backend.send(prompt)
        if hasattr(host, "changed"):
            host.changed()
