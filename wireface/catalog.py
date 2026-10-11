"""The choices for the agent's Model setting, from models.dev (the open catalog of models that OpenCode itself uses).

Downloaded at most once a day into ``data/cache``; without a connection the last download stands in, and with none at
all only Claude Code's aliases are offered (anything else can still be typed in under "Other"). To download every
list again now: ``python -m wireface.catalog --refresh`` (RefreshModels.cmd); the panel shows them when reopened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from . import config

log = logging.getLogger(__name__)

URL = "https://models.dev/api.json"
MAX_AGE_S = 24 * 3600
TIMEOUT_S = 20
# Claude Code's aliases: each always means the newest model of that kind
CLAUDE_ALIASES = [("opus", "Opus (always the newest)"), ("sonnet", "Sonnet (always the newest)"),
                  ("haiku", "Haiku (always the newest)")]
# OpenCode can reach every provider in the catalog (as provider/model); these are the ones worth listing
OPENCODE_PROVIDERS = ["opencode", "anthropic", "openai", "google"]
# Listed by the catalog but not offered. Tested through OpenCode 2.0.24: "Model unavailable" (not served to this
# account), or "Endpoint is unavailable" / "no longer available" / needs a tool this app doesn't have
OPENCODE_HIDDEN = {"opencode/mistral-large-4", "opencode/exo-free", "opencode/ling-3.0-flash-fin-free",
                   "google/gemini-2.5-pro", "google/gemini-2.5-computer-use-preview-10-2025"}

_lock = threading.Lock()
_memo: tuple[float, dict] | None = None          # (file time, parsed catalog): the file is a few MB of JSON


def cache_file():
    return config.DATA / "cache" / "models.dev.json"


def _read() -> tuple[dict, float]:
    global _memo
    f = cache_file()
    mtime = f.stat().st_mtime
    if _memo and _memo[0] == mtime:
        return _memo[1], mtime
    data = json.loads(f.read_text(encoding="utf-8"))
    _memo = (mtime, data)
    return data, mtime


def catalog(refresh: bool = False) -> tuple[dict, float | None, str | None]:
    """(the catalog, when it was downloaded, why downloading it just now failed). Blocking: run it in a thread."""
    with _lock:
        f = cache_file()
        if not refresh and f.exists() and time.time() - f.stat().st_mtime < MAX_AGE_S:
            try:
                data, mtime = _read()
                return data, mtime, None
            except (OSError, ValueError):
                pass
        try:
            req = urllib.request.Request(URL, headers={"User-Agent": "wireface"})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                raw = r.read()
            if not isinstance(json.loads(raw), dict):
                raise ValueError("the model list is not in the expected shape")
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(f)
            data, mtime = _read()
            return data, mtime, None
        except (OSError, ValueError) as e:
            log.warning("could not download the model list: %s", e)
            try:
                data, mtime = _read()
                return data, mtime, str(e)
            except (OSError, ValueError):
                return {}, None, str(e)


def _models(provider: dict, prefix: str = "") -> list[dict]:
    """The provider's models an agent can work with (text out, tool calls), newest first."""
    out = []
    for m in (provider.get("models") or {}).values():
        if not m.get("tool_call") or (m.get("modalities") or {}).get("output") != ["text"]:
            continue
        if m.get("status") == "deprecated":
            continue
        out.append({"id": prefix + m["id"], "name": m.get("name") or m["id"], "released": m.get("release_date") or ""})
    out.sort(key=lambda m: (m["released"], m["name"]), reverse=True)
    return out


def opencode_served(refresh: bool = False) -> set[str] | None:
    """The provider/model ids OpenCode itself can serve on this machine (asked of the OpenCode CLI, kept a day).
    None when OpenCode can't be asked, in which case the catalog's own list stands."""
    f = config.DATA / "cache" / "opencode-served.json"
    if not refresh:
        try:
            kept = json.loads(f.read_text(encoding="utf-8"))
            if time.time() - kept.get("at", 0) < MAX_AGE_S:
                return set(kept["ids"])
        except (OSError, ValueError, KeyError):
            pass
    exe = shutil.which("opencode")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "api", "model.list"], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        out = json.loads(r.stdout[r.stdout.index("{"):])
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log.warning("could not ask OpenCode for its models: %s", e)
        return None
    ids = sorted({f"{m['providerID']}/{m['id']}" for m in out.get("data") or [] if m.get("providerID")})
    if not ids:
        return None
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"at": time.time(), "ids": ids}), encoding="utf-8")
    return set(ids)


def choices(backend: str, data: dict, served: set[str] | None = None) -> list[dict]:
    """The models on offer for an agent (``backend.type``), in groups: [{"label", "models": [{"id", "name"}]}].
    For OpenCode, ``served`` (see opencode_served) keeps only the models OpenCode can actually serve."""
    if backend == "claude":
        groups = [{"label": "Always the newest", "models": [{"id": a, "name": n} for a, n in CLAUDE_ALIASES]}]
        if "anthropic" in data:
            groups.append({"label": "Anthropic", "models": _models(data["anthropic"])})
    elif backend == "codex":
        groups = [{"label": "OpenAI", "models": _models(data["openai"])}] if "openai" in data else []
    elif backend == "opencode":
        groups = [{"label": data[p].get("name") or p,
                   "models": [m for m in _models(data[p], p + "/")
                              if m["id"] not in OPENCODE_HIDDEN and (served is None or m["id"] in served)]}
                  for p in OPENCODE_PROVIDERS if p in data]
    else:
        groups = []
    return [g for g in groups if g["models"]]


# --- the realtime voice's models: asked of the service itself (the catalog lags behind its live models) -------------
NOT_FOR_TALKING = ("translate", "transcribe", "whisper", "robotics")      # live models made for something else


def live_file(provider: str):
    return config.DATA / "cache" / f"live-models-{provider}.json"


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "wireface", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read())


def live_models(provider: str, key: str, refresh: bool = False) -> list[dict]:
    """The speech-to-speech models this key can use, from the service's own model list (kept a day, on disk so that
    a refresh from the command line reaches a running face). Blocking; raises OSError or ValueError when the service
    can't be asked."""
    f = live_file(provider)
    whose = hashlib.sha256(key.encode()).hexdigest()[:12]      # another key may see other models
    if not refresh:
        try:
            kept = json.loads(f.read_text(encoding="utf-8"))
            if kept.get("key") == whose and time.time() - kept.get("at", 0) < MAX_AGE_S:
                return kept["models"]
        except (OSError, ValueError, KeyError):
            pass
    if provider == "gemini":            # the key goes in a header, not the URL
        data = _get_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", {"x-goog-api-key": key})
        out = [{"id": m["name"].removeprefix("models/"), "name": m.get("displayName") or m["name"].removeprefix("models/")}
               for m in data.get("models") or []
               if "bidiGenerateContent" in (m.get("supportedGenerationMethods") or [])
               and not any(w in m["name"] for w in NOT_FOR_TALKING)]
        out.sort(key=lambda m: m["id"], reverse=True)
    elif provider == "openai":
        data = _get_json("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
        rows = sorted((m for m in data.get("data") or [] if "realtime" in m.get("id", "")
                       and not any(w in m["id"] for w in NOT_FOR_TALKING)), key=lambda m: m.get("created") or 0, reverse=True)
        out = [{"id": m["id"], "name": m["id"]} for m in rows]
    else:
        out = []
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"key": whose, "at": time.time(), "models": out}), encoding="utf-8")
    return out


def realtime_choices(provider: str, label: str, key: str | None, default: str, data: dict,
                     refresh: bool = False) -> tuple[list[dict], str | None]:
    """(groups, why the service's own list couldn't be had) for the realtime voice's Model setting."""
    error = None
    models: list[dict] = []
    if key:
        try:
            models = live_models(provider, key, refresh)
        except (OSError, ValueError, KeyError) as e:
            error = f"couldn't ask {label} for its models ({e})"
            log.warning("realtime model list: %s", error)
    else:
        error = f"there is no API key for {label} yet"
    if not models:                      # the catalog's idea of them
        src, word = {"gemini": ("google", "live"), "openai": ("openai", "realtime")}.get(provider, ("", "\0"))
        models = [{"id": m["id"], "name": m.get("name") or m["id"]} for m in (data.get(src, {}).get("models") or {}).values()
                  if word in m["id"] and not any(w in m["id"] for w in NOT_FOR_TALKING)]
    if default and not any(m["id"] == default for m in models):
        models.insert(0, {"id": default, "name": f"{default} (default)"})
    return ([{"label": label, "models": models}] if models else []), error


# --- python -m wireface.catalog --refresh --------------------------------------------------------------------------
def refresh_all(face: str | None = None) -> list[str]:
    """Download every list again: the catalog, and each realtime service's own list for the keys this face has.
    Returns what happened, a line each."""
    from .credentials import Credentials
    from .profile import resolve
    from .realtime import PROVIDERS

    lines = []
    data, fetched, error = catalog(refresh=True)
    if error:
        kept = time.strftime("%Y-%m-%d %H:%M", time.localtime(fetched)) if fetched else None
        lines.append(f"models.dev: download failed ({error})" + (f"; keeping the copy from {kept}" if kept else ""))
    else:
        lines.append(f"models.dev: {sum(len(p.get('models') or {}) for p in data.values())} models from {len(data)} providers")
    served = opencode_served(refresh=True)
    for kind, label in (("claude", "Claude Code"), ("codex", "Codex"), ("opencode", "OpenCode")):
        groups = choices(kind, data, served if kind == "opencode" else None)
        lines.append(f"  {label}: {sum(len(g['models']) for g in groups)} models"
                     + ("" if served or kind != "opencode" else " (OpenCode could not be asked; the full list)"))
    creds = Credentials(resolve(face))
    for pid, cls in PROVIDERS.items():
        groups, why = realtime_choices(pid, cls.label, creds.realtime_key(pid)[0], cls.default_model, data, refresh=True)
        lines.append(f"  {cls.label} (voice): {sum(len(g['models']) for g in groups)} models" + (f" ({why})" if why else ""))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wireface.catalog", description="The lists behind the Model settings.")
    ap.add_argument("--refresh", action="store_true", help="download every model list again now")
    ap.add_argument("--face", default=None, help="the face whose API keys list the realtime voice's models")
    args = ap.parse_args(argv)
    if not args.refresh:
        ap.print_help()
        return 0
    lines = refresh_all(args.face)
    print("\n".join(lines))
    return 1 if "failed" in lines[0] else 0


if __name__ == "__main__":
    sys.exit(main())
