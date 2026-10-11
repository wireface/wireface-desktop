"""API keys: found in the environment or saved for this face, and never sent to the page.

A key saved from the settings page lives in the face's own ``secrets.json`` (next to its config, covered by
.gitignore, and never part of ``profile.json``, which is the file people share). Otherwise the key comes from an
environment variable. On Windows a variable set after the face started (in System Properties) is not in the
running process's environment, so the registry is read too: no restart needed.
"""
from __future__ import annotations

import os
import sys
import time

from .profile import Profile, read_json_lenient, write_json

ELEVENLABS_ENV = ("ELEVENLABS_API_KEY", "XI_API_KEY")      # the second is what ElevenLabs' own tools use
TWILIO_ENV = ("TWILIO_AUTH_TOKEN",)
OPENROUTER_ENV = ("OPENROUTER_API_KEY",)
REALTIME_ENV = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "openai": ("OPENAI_API_KEY",)}
_CACHE_S = 10.0


def registry_env(name: str) -> str | None:
    """A Windows environment variable straight from the registry (the user's, then the machine's)."""
    if sys.platform != "win32":
        return None
    import winreg

    for root, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, sub) as k:
                value, _kind = winreg.QueryValueEx(k, name)
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    return registry_env(name)


def check_key(key: str) -> str:
    key = key.strip()
    if not 8 <= len(key) <= 200 or any(c.isspace() for c in key):
        raise ValueError("that does not look like an API key")
    return key


class Credentials:
    """One face's keys: ElevenLabs (the classic cloud voice), the realtime voice services, the Twilio auth token of
    the phone line, and OpenRouter (the judge of what the microphone heard). ``elevenlabs()`` is cheap enough to call
    for every sentence spoken."""

    SERVICES = ("elevenlabs", "gemini", "openai", "twilio", "openrouter")

    def __init__(self, profile: Profile):
        self.profile = profile
        self._cached: dict[str, tuple[float, tuple[str | None, str | None]]] = {}

    def _env_names(self, service: str) -> tuple[str, ...]:
        voice = self.profile.config["voice"]
        if service == "elevenlabs":
            own, usual = voice["elevenlabs"]["api_key_env"], ELEVENLABS_ENV
        elif service == "twilio":
            own, usual = self.profile.config["phone"]["auth_token_env"], TWILIO_ENV
        elif service == "openrouter":
            own, usual = voice["hearing"]["judge"]["api_key_env"], OPENROUTER_ENV
        else:
            own, usual = voice["realtime"][service]["api_key_env"], REALTIME_ENV[service]
        return (own,) + tuple(n for n in usual if n != own)

    def key(self, service: str) -> tuple[str | None, str | None]:
        """(key, where it came from: "saved" or "env:NAME"), or (None, None)."""
        now = time.monotonic()
        cached = self._cached.get(service)
        if cached and now - cached[0] < _CACHE_S:
            return cached[1]
        found: tuple[str | None, str | None] = (None, None)
        saved = read_json_lenient(self.profile.secrets_file).get(f"{service}_api_key")
        if isinstance(saved, str) and saved.strip():
            found = (saved.strip(), "saved")
        else:
            for name in self._env_names(service):
                value = env_value(name)
                if value:
                    found = (value, f"env:{name}")
                    break
        self._cached[service] = (now, found)
        return found

    def elevenlabs(self) -> tuple[str | None, str | None]:
        return self.key("elevenlabs")

    def realtime_key(self, provider: str) -> tuple[str | None, str | None]:
        return self.key(provider)

    def set_key(self, service: str, key: str | None) -> None:
        """Save a key for this face (None: forget the saved one; an environment variable still counts)."""
        if service not in self.SERVICES:
            raise ValueError(f"unknown service {service!r}")
        secrets = read_json_lenient(self.profile.secrets_file)
        if key is None:
            secrets.pop(f"{service}_api_key", None)
        else:
            secrets[f"{service}_api_key"] = check_key(key)
        if secrets:
            write_json(self.profile.secrets_file, secrets)
        else:
            self.profile.secrets_file.unlink(missing_ok=True)
        self._cached.pop(service, None)

    def set_elevenlabs(self, key: str | None) -> None:
        self.set_key("elevenlabs", key)

    def forget_cache(self) -> None:
        self._cached.clear()

    def secret_values(self) -> set[str]:
        """Every key in use, so the activity log can keep them out of what it stores."""
        return {k for k, _ in (self.key(s) for s in self.SERVICES) if k}
