"""A face's own configuration and files, so several faces can run at once with different options.

    python -m wireface                       the default face: the files in ``data\\`` (as before)
    python -m wireface --face work           ``data\\faces\\work\\profile.json`` and friends
    python -m wireface --config D:\\x\\a.json  that file is the config; its runtime files go to ``data\\faces\\a\\``

The config (``profile.json``) is what the user chooses: the name, which agent backend runs behind the face and
how, and the voice. Runtime state that is not a choice (the conversation to resume) is ``state.json``. Both are
validated by ``validate``, which is what the settings API uses too.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

import psutil

from . import config

log = logging.getLogger(__name__)

DEFAULT_FACE = "default"
VOICE_ENGINES = ("auto", "local", "elevenlabs")
LOCAL_ENGINES = ("pocket", "kokoro")
VOICE_MODES = ("realtime", "classic")
REALTIME_PROVIDERS = ("gemini", "openai")
NARRATION = ("quiet", "normal", "chatty")
PERSONAS = ("self", "relay")


def defaults(default_face: bool = True) -> dict:
    """A new face's config. The environment variables of the single-face days still seed the default face."""
    return {
        "name": config.AGENT_NAME,
        "port": config.PORT if default_face else 0,          # 0: any free port
        "backend": {
            "type": "claude",
            "cwd": str(config.DEFAULT_CWD),                  # the folder the agent works in
            "model": None,                                   # None: the agent's own default
            "skip_permissions": False,                       # never ask before acting (the agent's bypass flag)
            "permission_mode": "default",                    # ignored while skip_permissions is on
            "auto_start": True,                              # start the agent session when the face starts
            # compact the conversation while idle once the context is this many percent full (0: leave it to the agent,
            # which compacts on its own nearer its limit, in the middle of a reply if need be)
            "compact_at": 80,
            # what the agent can use on this PC (Claude Code): the desktop (screenshots, mouse, keyboard), a browser of
            # its own (Playwright, driving Chrome), and Claude in Chrome (the user's own Chrome, once its extension is in)
            "tools": {"computer": True, "browser": True, "chrome": True},
            "options": {},                                   # anything else the backend understands
        },
        "voice": {
            "on": True,                                      # speak replies (off: captions only)
            "listening": False,                              # microphone
            # "realtime": a speech-to-speech model talks with you and relays the agent's work (low latency, cloud);
            # "classic": speech recognition and TTS on this PC read the agent's own replies
            "mode": "realtime",
            "realtime": {
                "provider": "gemini",                        # "gemini" (Gemini Live) or "openai" (OpenAI Realtime)
                "gemini": {"model": "gemini-3.8-live", "voice": "Puck", "api_key_env": "GEMINI_API_KEY"},
                "openai": {"model": "gpt-realtime-2.1", "voice": "marin", "api_key_env": "OPENAI_API_KEY"},
                "narration": "normal",                       # quiet | normal | chatty: how often it reports progress
                "persona": "self",                           # "self": the agent's work is its own; "relay": reports on it
                "barge_in": False,                           # interrupt it by talking (use headphones)
                "fallback": True,                            # the classic voice reads replies while it can't connect
            },
            "hearing": {
                "name_interrupt": True,                      # saying the face's name stops it talking, and it listens
                "denoise": True,                             # reduce steady background noise before recognition
                "focus": True,                               # ignore voices much quieter than yours (a TV, a colleague)
                # Advanced. "ignore constant background sound": speech that doesn't stand out from what the room
                # usually sounds like (surprise.py) is dropped before recognition
                "surprise": False,
                # "show voice detector": a live meter of how far what the microphone hears stands out from the room,
                # on the face and in the settings (whether or not the filter above is on)
                "show_detector": False,
                # "judge what it hears": a decision model on OpenRouter decides whether what was heard was said to the
                # face, and what it is (judge.py); "unsure": hold it back (the panel can send it) or send it anyway
                "judge": {"on": False, "model": "~typesafe/jev-latest", "api_key_env": "OPENROUTER_API_KEY",
                          "unsure": "hold"},
            },
            "engine": "auto",                                # "auto": ElevenLabs when a key is available
            "local": {"engine": config.TTS_ENGINE if config.TTS_ENGINE in LOCAL_ENGINES else "pocket",
                      "voice": "marius"},
            "elevenlabs": {"voice_id": None, "model_id": "eleven_flash_v2_5", "api_key_env": "ELEVENLABS_API_KEY"},
        },
        "room": {
            "name": None,                                    # the room this face joins (faces in one room take turns)
            "aliases": [],                                   # other ways of saying this agent's name
            "lead": "first",                                 # "first": whoever answers first leads; or an agent's name
        },
        # a Twilio number the face answers, and the agent can text and call from (see phone/line.py)
        "phone": {
            "enabled": False,
            "account_sid": None,                             # AC... (the auth token is a secret: secrets.json or env)
            "number": None,                                  # the Twilio number, +447700900123
            "auth_token_env": "TWILIO_AUTH_TOKEN",
            "owner": None,                                   # who the line agent works for ("Sam Jones")
            "default_prefix": "+44",                         # for numbers written without a country code
            "allow_countries": ["+44"],                      # where texts and calls may go
            "contacts": {},                                  # name -> number: who the agent may text by name
            "texts": "receptionist",                         # incoming texts: receptionist | notify | off
            "calls": "receptionist",                         # incoming calls: receptionist | off
            "public_url": None,                              # https address of the phone server (None: Cloudflare tunnel)
            "manage_number": True,                           # point the number's calls here, stop Twilio's demo reply
            "check_every_s": 15,                             # how often new texts are looked for
            "limits": {"texts_per_day": 20, "calls_per_day": 10, "replies_per_number_per_day": 20, "call_minutes": 10},
            # texts: a Gemini model (an OpenAI one is used when there's only an OpenAI key); live calls: Gemini Live
            # or OpenAI Realtime, with a model and voice of that service
            "models": {"text": "gemini-3.8-flash", "call_provider": "gemini", "call": "gemini-3.8-live", "call_voice": "Puck"},
            "receptionist": {
                "business": None,                            # its name, as callers know it
                "about": "",                                 # what callers may be told: services, prices, address
                "bookings": True,                            # take bookings (phone/bookings.json)
                "hours": {d: ("09:00-17:30" if d not in ("sat", "sun") else None)
                          for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
                "slot_minutes": 30,
                "max_days_ahead": 60,
                "per_caller_per_day": 2,
            },
        },
    }


# --- validation ------------------------------------------------------------------------------------------------
def _bool(v):
    if not isinstance(v, bool):
        raise ValueError("expected true/false")
    return v


def _int(lo: int, hi: int):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"expected a whole number from {lo} to {hi}")
        return v
    return check


def _str(lo: int = 1, hi: int = 200, nullable: bool = False):
    def check(v):
        if v is None and nullable:
            return None
        if not isinstance(v, str) or not lo <= len(v.strip()) <= hi:
            raise ValueError(f"expected text of {lo} to {hi} characters" + (" or null" if nullable else ""))
        return v.strip()
    return check


def _choice(*options: str):
    def check(v):
        if v not in options:
            raise ValueError(f"expected one of {', '.join(options)}")
        return v
    return check


def _folder(v):
    path = Path(str(v)).expanduser()
    if not path.is_dir():
        raise ValueError(f"not a folder: {path}")
    return str(path)


def _env_name(v):
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", v):
        raise ValueError("expected an environment variable name")
    return v


def _options(v):
    if not isinstance(v, dict) or not all(isinstance(k, str) for k in v):
        raise ValueError("expected an object")
    if len(json.dumps(v, default=str)) > 8000:
        raise ValueError("too large")
    return copy.deepcopy(v)


def _room_name(v):
    if v is None:
        return None
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", v):
        raise ValueError("expected a short name (letters, digits, . _ -) or null")
    return v


def _aliases(v):
    if not isinstance(v, list) or len(v) > 10 or not all(isinstance(a, str) and 1 <= len(a.strip()) <= 30 for a in v):
        raise ValueError("expected up to 10 short names")
    return [a.strip() for a in v]


def _account_sid(v):
    if v is not None and (not isinstance(v, str) or not re.fullmatch(r"AC[0-9a-fA-F]{32}", v.strip())):
        raise ValueError("expected a Twilio account SID (AC followed by 32 hex digits) or null")
    return v.strip() if v else None


def _phone_number(v):
    if v is None:
        return None
    from .phone.numbers import normalize
    return normalize(str(v), "")                             # the line's own number: in full, with its country code


def _prefix(v):
    if not isinstance(v, str) or not re.fullmatch(r"\+[1-9]\d{0,4}", v.strip()):
        raise ValueError('expected a country code like "+44"')
    return v.strip()


def _prefixes(v):
    if not isinstance(v, list) or len(v) > 40:
        raise ValueError('expected a list of country codes like ["+44", "+1"]')
    return [_prefix(x) for x in v]


def _contacts(v):
    """name -> number; numbers are put in full (E.164) by ``validate``, which knows the default country code."""
    if not isinstance(v, dict) or len(v) > 300:
        raise ValueError("expected up to 300 contacts (name: number)")
    out = {}
    for name, number in v.items():
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or not isinstance(number, str):
            raise ValueError(f"contact {name!r}: expected a short name and a number")
        out[name.strip()] = number.strip()
    return out


def _public_url(v):
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?", v.strip()):
        raise ValueError("expected an https:// address (no query string) or null")
    return v.strip().rstrip("/")


def _hours(v):
    from .phone.bookings import DAYS, parse_hours
    if not isinstance(v, dict) or set(v) - set(DAYS):
        raise ValueError("expected opening hours for mon..sun")
    out = {}
    for day in DAYS:
        text = v.get(day)
        parse_hours(text)                                    # ValueError when malformed
        out[day] = text.strip() if text else None
    return out


def _backend_type(v):
    from .backends import registry
    return _choice(*registry.types())(v)


LEAVES = {
    ("name",): _str(1, 40),
    ("port",): _int(0, 65535),
    ("backend", "type"): _backend_type,
    ("backend", "cwd"): _folder,
    ("backend", "model"): _str(1, 100, nullable=True),
    ("backend", "skip_permissions"): _bool,
    ("backend", "permission_mode"): _str(1, 40),
    ("backend", "auto_start"): _bool,
    ("backend", "compact_at"): _int(0, 95),
    ("backend", "tools", "computer"): _bool,
    ("backend", "tools", "browser"): _bool,
    ("backend", "tools", "chrome"): _bool,
    ("backend", "options"): _options,
    ("voice", "on"): _bool,
    ("voice", "listening"): _bool,
    ("voice", "engine"): _choice(*VOICE_ENGINES),
    ("voice", "mode"): _choice(*VOICE_MODES),
    ("voice", "realtime", "provider"): _choice(*REALTIME_PROVIDERS),
    ("voice", "realtime", "gemini", "model"): _str(1, 100),
    ("voice", "realtime", "gemini", "voice"): _str(1, 40),
    ("voice", "realtime", "gemini", "api_key_env"): _env_name,
    ("voice", "realtime", "openai", "model"): _str(1, 100),
    ("voice", "realtime", "openai", "voice"): _str(1, 40),
    ("voice", "realtime", "openai", "api_key_env"): _env_name,
    ("voice", "realtime", "narration"): _choice(*NARRATION),
    ("voice", "realtime", "persona"): _choice(*PERSONAS),
    ("voice", "realtime", "barge_in"): _bool,
    ("voice", "realtime", "fallback"): _bool,
    ("voice", "hearing", "name_interrupt"): _bool,
    ("voice", "hearing", "denoise"): _bool,
    ("voice", "hearing", "focus"): _bool,
    ("voice", "hearing", "surprise"): _bool,
    ("voice", "hearing", "show_detector"): _bool,
    ("voice", "hearing", "judge", "on"): _bool,
    ("voice", "hearing", "judge", "model"): _str(1, 100),
    ("voice", "hearing", "judge", "api_key_env"): _env_name,
    ("voice", "hearing", "judge", "unsure"): _choice("hold", "send"),
    ("voice", "local", "engine"): _choice(*LOCAL_ENGINES),
    ("voice", "local", "voice"): _str(1, 80),
    ("voice", "elevenlabs", "voice_id"): _str(1, 64, nullable=True),
    ("voice", "elevenlabs", "model_id"): _str(1, 64),
    ("voice", "elevenlabs", "api_key_env"): _env_name,
    ("room", "name"): _room_name,
    ("room", "aliases"): _aliases,
    ("room", "lead"): _str(1, 40),
    ("phone", "enabled"): _bool,
    ("phone", "account_sid"): _account_sid,
    ("phone", "number"): _phone_number,
    ("phone", "auth_token_env"): _env_name,
    ("phone", "owner"): _str(1, 60, nullable=True),
    ("phone", "default_prefix"): _prefix,
    ("phone", "allow_countries"): _prefixes,
    ("phone", "contacts"): _contacts,
    ("phone", "texts"): _choice("receptionist", "notify", "off"),
    ("phone", "calls"): _choice("receptionist", "off"),
    ("phone", "public_url"): _public_url,
    ("phone", "manage_number"): _bool,
    ("phone", "check_every_s"): _int(5, 300),
    ("phone", "limits", "texts_per_day"): _int(0, 500),
    ("phone", "limits", "calls_per_day"): _int(0, 200),
    ("phone", "limits", "replies_per_number_per_day"): _int(0, 200),
    ("phone", "limits", "call_minutes"): _int(1, 60),
    ("phone", "models", "text"): _str(1, 100),
    ("phone", "models", "call_provider"): _choice("gemini", "openai"),
    ("phone", "models", "call"): _str(1, 100),
    ("phone", "models", "call_voice"): _str(1, 40),
    ("phone", "receptionist", "business"): _str(1, 80, nullable=True),
    ("phone", "receptionist", "about"): _str(0, 4000),
    ("phone", "receptionist", "bookings"): _bool,
    ("phone", "receptionist", "hours"): _hours,
    ("phone", "receptionist", "slot_minutes"): _int(5, 240),
    ("phone", "receptionist", "max_days_ahead"): _int(1, 365),
    ("phone", "receptionist", "per_caller_per_day"): _int(1, 20),
}


def _leaves(patch: dict, prefix: tuple = ()):
    """(path, value) for each setting in a nested patch; ``backend.options`` is one value, not a branch."""
    for key, value in patch.items():
        path = prefix + (key,)
        if isinstance(value, dict) and path != ("backend", "options") and path not in LEAVES:
            yield from _leaves(value, path)
        else:
            yield path, value


def validate(patch: dict, base: dict) -> dict:
    """``base`` with ``patch`` applied; raises ValueError (naming the setting) for anything invalid."""
    if not isinstance(patch, dict):
        raise ValueError("expected an object")
    out = copy.deepcopy(base)
    leaves = list(_leaves(patch))
    for path, value in leaves:
        check = LEAVES.get(path)
        if check is None:
            raise ValueError(f"unknown setting {'.'.join(path)!r}")
        try:
            value = check(value)
        except ValueError as e:
            raise ValueError(f"{'.'.join(path)}: {e}") from None
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    phone = out.get("phone")
    if phone and phone.get("contacts"):                      # contacts in full, now the default country code is known
        from .phone.numbers import NumberError, normalize
        try:
            phone["contacts"] = {n: normalize(num, phone["default_prefix"]) for n, num in phone["contacts"].items()}
        except NumberError as e:
            raise ValueError(f"phone.contacts: {e}") from None
    from .backends import registry
    try:
        agent = registry.get(out["backend"]["type"])
        if (out["backend"]["type"] != base["backend"]["type"] and ("backend", "permission_mode") not in {p for p, _ in leaves}
                and out["backend"]["permission_mode"] not in agent.permission_modes):
            out["backend"]["permission_mode"] = "default"       # each agent has its own modes: start the new one plainly
        agent.validate(out["backend"])
    except ValueError as e:
        raise ValueError(f"backend: {e}") from None
    return out


def merge_loaded(saved: dict, base: dict) -> dict:
    """A config read from disk: settings that are no longer valid (or whose folder is gone) are dropped."""
    out = copy.deepcopy(base)
    for path, value in _leaves(saved if isinstance(saved, dict) else {}):
        try:
            out = validate(_nest(path, value), out)
        except ValueError as e:
            log.warning("profile: ignoring %s: %s", ".".join(path), e)
    return out


def _nest(path: tuple, value) -> dict:
    for part in reversed(path):
        value = {part: value}
    return value


# --- files -----------------------------------------------------------------------------------------------------
def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip(".-_")[:40]
    if not s:
        raise ValueError(f"not a usable face name: {text!r}")
    return s


def write_json(path: Path, data) -> None:
    """Atomic write (a reader never sees half a file); retries while an indexer or editor holds the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05)


def read_json(path: Path) -> dict:
    """The object in ``path``; {} if there is none. A file that exists but is not valid JSON raises ValueError."""
    try:
        text = path.read_text(encoding="utf-8-sig")      # Notepad and PowerShell 5 may start it with a BOM
    except OSError:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def read_json_lenient(path: Path) -> dict:
    try:
        return read_json(path)
    except ValueError:
        return {}


def ensure_sandbox() -> None:
    """The default working folder is ``data\\sandbox`` in this project: make it, with its AGENTS.md, if it is missing
    (before the config is read, which only accepts folders that exist). A folder chosen with WIREFACE_CWD is
    left to its owner."""
    folder = config.DEFAULT_CWD
    if folder != config.SANDBOX:
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        from .backends.base import ensure_instructions
        ensure_instructions(folder)
    except OSError as e:
        log.warning("could not create %s: %s", folder, e)


class AlreadyRunning(RuntimeError):
    pass


class Profile:
    """The files and settings of one face. ``config`` is the validated ``profile.json``; ``state`` is ``state.json``."""

    def __init__(self, name: str, directory: Path, file: Path, legacy: bool):
        self.name = name                                     # the folder name ("default" for the original data\\)
        self.dir = directory
        self.file = file                                     # the config file
        self.legacy = legacy                                 # the original data\\ layout (settings.json beside it)
        self.config = defaults(default_face=legacy)
        self.state: dict = {"session_id": None, "sessions": {}}    # sessions: each agent's own conversation
        self.load_error: str | None = None                   # the config file exists but could not be read
        self._lock_held = False

    # --- paths (everything a face writes lives in its own folder, except the shared models) ---
    @property
    def state_file(self) -> Path: return self.dir / "state.json"
    @property
    def face_file(self) -> Path: return self.dir / "face.json"
    @property
    def window_file(self) -> Path: return self.dir / "face_window.json"
    @property
    def photo_dir(self) -> Path: return self.dir / "face"
    @property
    def shell_profile(self) -> Path: return self.dir / "face-profile"
    @property
    def secrets_file(self) -> Path: return self.dir / "secrets.json"
    @property
    def logs(self) -> Path: return self.dir / "logs"
    @property
    def lock_file(self) -> Path: return self.dir / ".running"

    # --- load / save ---
    def load(self) -> "Profile":
        ensure_sandbox()
        try:
            saved = read_json(self.file)
        except ValueError as e:
            saved = {}
            self.load_error = f"{self.file.name} is not valid ({e}); using defaults until it is fixed"
            log.error("%s: %s", self.file, self.load_error)
        if not saved and self.legacy:
            saved = self._from_settings_json(read_json_lenient(self.dir / "settings.json"))
        self.config = merge_loaded(saved, self.config)
        state = read_json_lenient(self.state_file)
        if not state and self.legacy:
            state = {"session_id": read_json_lenient(self.dir / "settings.json").get("session_id")}
        sid, sessions = state.get("session_id"), state.get("sessions")
        self.state = {"session_id": sid if isinstance(sid, str) and sid else None,
                      "sessions": {k: v for k, v in sessions.items() if isinstance(k, str) and isinstance(v, str) and v}
                      if isinstance(sessions, dict) else {}}
        wanted = saved.get("backend", {}).get("cwd") if isinstance(saved.get("backend"), dict) else None
        if isinstance(wanted, str) and wanted and self.config["backend"]["cwd"] != str(Path(wanted).expanduser()):
            self.state["session_id"], self.state["sessions"] = None, {}     # its folder is gone: so is its conversation
        return self

    @staticmethod
    def _from_settings_json(old: dict) -> dict:
        """The single-face ``settings.json`` as a profile (first run after upgrading)."""
        out: dict = {"backend": {}, "voice": {"local": {}}}
        for src, dest in (("cwd", ("backend", "cwd")), ("permission_mode", ("backend", "permission_mode")),
                          ("model", ("backend", "model")), ("voice_on", ("voice", "on")),
                          ("listening", ("voice", "listening"))):
            if src in old:
                out[dest[0]][dest[1]] = old[src]
        if "voice" in old:
            out["voice"]["local"]["voice"] = old["voice"]
        return out

    def save_config(self) -> None:
        if self.load_error and self.file.exists():           # don't destroy a file the user is still editing
            backup = self.file.with_name(self.file.name + ".bad")
            shutil.copy2(self.file, backup)
            log.warning("kept the unreadable %s as %s", self.file.name, backup.name)
            self.load_error = None
        write_json(self.file, self.config)

    def save_state(self) -> None:
        write_json(self.state_file, self.state)

    def apply(self, patch: dict) -> dict:
        """Validate ``patch`` and make it the config (not yet saved); returns the new config."""
        self.config = validate(patch, self.config)
        return self.config

    # --- one instance per face ---
    def acquire_lock(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            other = int(self.lock_file.read_text().strip())
        except (OSError, ValueError):
            other = 0
        if other and other != os.getpid() and _is_face_process(other):
            raise AlreadyRunning(f"the face {self.name!r} is already running (pid {other}); "
                                 f"if it is not, delete {self.lock_file}")
        self.lock_file.write_text(str(os.getpid()))
        self._lock_held = True

    def release_lock(self) -> None:
        if self._lock_held:
            self.lock_file.unlink(missing_ok=True)
            self._lock_held = False


def _is_face_process(pid: int) -> bool:
    try:
        cmd = psutil.Process(pid).cmdline()
    except psutil.Error:
        return False
    # not just any process that reused the pid (the module, not the folder: the repo itself lives under wireface\)
    return any(part == "wireface" or part.startswith("wireface.") for part in cmd)


def resolve(face: str | None = None, config_path: str | None = None) -> Profile:
    """The profile for ``--face NAME`` / ``--config PATH`` (neither: the default face), loaded."""
    if config_path:
        file = Path(config_path).expanduser().resolve()
        name = slug(file.stem)
        return Profile(name, config.DATA / "faces" / name, file, legacy=False).load()
    if face and face != DEFAULT_FACE:
        name = slug(face)
        directory = config.DATA / "faces" / name
        return Profile(name, directory, directory / "profile.json", legacy=False).load()
    return Profile(DEFAULT_FACE, config.DATA, config.DATA / "profile.json", legacy=True).load()
