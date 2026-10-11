"""ElevenLabs text to speech: streams raw 16-bit PCM for each sentence into the face's audio player.

Standard library only (``urllib``): it runs in the voice worker thread, one request per sentence, and the audio
is handed over as it arrives so speech starts before the sentence has finished generating.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterator

import numpy as np

log = logging.getLogger(__name__)

API = "https://api.elevenlabs.io"
TIMEOUT_S = 25
CHUNK_BYTES = 4800                                  # 100 ms at 24 kHz, 16-bit mono
FORMATS = {16000: "pcm_16000", 22050: "pcm_22050", 24000: "pcm_24000", 44100: "pcm_44100"}
MODELS = {"eleven_flash_v2_5": "Flash v2.5 (fastest)", "eleven_turbo_v2_5": "Turbo v2.5",
          "eleven_multilingual_v2": "Multilingual v2 (best quality)"}


class ElevenLabsError(RuntimeError):
    """The request failed; ``fatal`` means retrying will not help (a bad key, no voice chosen)."""

    def __init__(self, message: str, status: int | None = None, fatal: bool = False):
        super().__init__(message)
        self.status, self.fatal = status, fatal


def decode_pcm(data: bytes) -> tuple[np.ndarray, bytes]:
    """16-bit little-endian PCM -> float32 samples, and the odd byte left over when a chunk split a sample."""
    usable = len(data) - (len(data) % 2)
    samples = np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0
    return samples, data[usable:]


def _detail(body: bytes) -> str:
    try:
        d = json.loads(body.decode("utf-8", "replace")).get("detail")
    except (ValueError, AttributeError):
        return ""
    if isinstance(d, dict):
        return str(d.get("message") or d.get("status") or "")
    if isinstance(d, list) and d:
        return str(d[0].get("msg", "")) if isinstance(d[0], dict) else str(d[0])
    return str(d or "")


def _error(e: urllib.error.HTTPError) -> ElevenLabsError:
    detail = _detail(e.read() if hasattr(e, "read") else b"")
    if e.code in (401, 403):
        return ElevenLabsError(f"ElevenLabs did not accept the API key{': ' + detail if detail else ''}", e.code, fatal=True)
    if e.code == 429:
        return ElevenLabsError(f"ElevenLabs is rate limiting this key or the quota is used up{': ' + detail if detail else ''}", e.code)
    if e.code in (400, 404, 422):
        return ElevenLabsError(f"ElevenLabs refused the request ({e.code}){': ' + detail if detail else ''}", e.code, fatal=True)
    return ElevenLabsError(f"ElevenLabs error {e.code}{': ' + detail if detail else ''}", e.code)


class ElevenLabsClient:
    """``key`` is the API key. Nothing here logs it."""

    def __init__(self, key: str):
        self.key = key

    def _request(self, path: str, body: dict | None = None) -> urllib.request.Request:
        return urllib.request.Request(
            API + path, data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"xi-api-key": self.key, "Accept": "application/json" if body is None else "audio/pcm",
                     "Content-Type": "application/json", "User-Agent": "wireface"},
            method="GET" if body is None else "POST")

    def stream(self, text: str, voice_id: str, model_id: str, sample_rate: int,
               stop: threading.Event | None = None) -> Iterator[bytes]:
        """Raw PCM for ``text``, as it is generated. Stops quietly when ``stop`` is set."""
        fmt = FORMATS.get(sample_rate)
        if not fmt:
            raise ElevenLabsError(f"ElevenLabs cannot produce {sample_rate} Hz audio", fatal=True)
        path = f"/v1/text-to-speech/{urllib.parse.quote(voice_id, safe='')}/stream?output_format={fmt}"
        try:
            resp = urllib.request.urlopen(self._request(path, {"text": text, "model_id": model_id}), timeout=TIMEOUT_S)
        except urllib.error.HTTPError as e:
            raise _error(e) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise ElevenLabsError(f"could not reach ElevenLabs: {getattr(e, 'reason', e)}") from None
        try:
            while not (stop and stop.is_set()):
                chunk = resp.read(CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
        except (OSError, TimeoutError) as e:
            raise ElevenLabsError(f"the ElevenLabs stream broke: {e}") from None
        finally:
            resp.close()

    def voices(self) -> list[dict]:
        """The voices this account can use: [{"id", "name", "category"}], premade ones last."""
        try:
            with urllib.request.urlopen(self._request("/v1/voices"), timeout=TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise _error(e) from None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            raise ElevenLabsError(f"could not reach ElevenLabs: {getattr(e, 'reason', e)}") from None
        out = [{"id": v["voice_id"], "name": v.get("name") or v["voice_id"], "category": v.get("category") or ""}
               for v in data.get("voices", []) if v.get("voice_id")]
        return sorted(out, key=lambda v: (v["category"] == "premade", v["name"].lower()))


class ElevenLabsEngine:
    """What ``speech.Voice`` calls to speak a sentence. ``settings()`` is asked every time, so a changed key or
    voice applies from the next sentence: it returns (key or None, voice_id or None, model_id)."""

    name = "elevenlabs"

    def __init__(self, settings: Callable[[], tuple[str | None, str | None, str]], sample_rate: int = 24000):
        self.settings = settings
        self.sample_rate = sample_rate
        self._first_voice: tuple[str, str] | None = None        # (key, voice id) of the account's first voice

    def available(self) -> bool:
        return bool(self.settings()[0])

    def _voice_id(self, client: ElevenLabsClient, chosen: str | None) -> str:
        if chosen:
            return chosen
        if self._first_voice and self._first_voice[0] == client.key:
            return self._first_voice[1]
        voices = client.voices()
        if not voices:
            raise ElevenLabsError("this ElevenLabs account has no voices to use: choose one in Settings", fatal=True)
        self._first_voice = (client.key, voices[0]["id"])
        return voices[0]["id"]

    def synth(self, text: str, cb, stop: threading.Event) -> None:
        """Speak ``text``: ``cb(samples, 0.0)`` gets each piece as float32 (it returns 0 to stop)."""
        key, voice_id, model_id = self.settings()
        if not key:
            raise ElevenLabsError("there is no ElevenLabs API key", fatal=True)
        client = ElevenLabsClient(key)
        pending = b""
        for raw in client.stream(text, self._voice_id(client, voice_id), model_id, self.sample_rate, stop):
            samples, pending = decode_pcm(pending + raw)
            if len(samples) and not cb(samples, 0.0):
                return
