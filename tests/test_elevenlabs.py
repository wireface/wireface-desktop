import asyncio
import io
import json
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from wireface import config, credentials, profile as profiles, tts_elevenlabs as el
from wireface.speech import Voice


class Resp:
    """A urlopen() result."""

    def __init__(self, chunks=(), body=b""):
        self.chunks, self.body, self.closed = list(chunks), body, False

    def read(self, n=-1):
        if self.chunks:
            return self.chunks.pop(0)
        out, self.body = self.body, b""
        return out

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def http_error(code, detail=None):
    body = json.dumps({"detail": detail}).encode() if detail is not None else b""
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


def pcm(values):
    return np.array(values, dtype="<i2").tobytes()


@pytest.fixture
def requests(monkeypatch):
    """Capture what is sent to ElevenLabs; set requests.reply to what comes back (a Resp or an exception)."""
    class Spy:
        sent = []
        reply = Resp()
        queue = []                                  # replies for successive requests (then ``reply`` again)

    def fake_urlopen(req, timeout=None):
        Spy.sent.append(req)
        reply = Spy.queue.pop(0) if Spy.queue else Spy.reply
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    Spy.sent, Spy.queue = [], []
    return Spy


# --- the audio -------------------------------------------------------------------------------------------------
def test_pcm_is_decoded_even_when_a_chunk_splits_a_sample():
    data = pcm([0, 16384, -16384, 32767])
    first, rest = el.decode_pcm(data[:5])                      # 2.5 samples
    second, rest2 = el.decode_pcm(rest + data[5:])
    both = np.concatenate([first, second])
    assert rest2 == b"" and len(first) == 2
    assert np.allclose(both, [0, 0.5, -0.5, 32767 / 32768])


# --- requests --------------------------------------------------------------------------------------------------
def test_a_sentence_is_streamed_with_the_right_request(requests):
    requests.reply = Resp(chunks=[pcm([1, 2]), pcm([3])])
    chunks = list(el.ElevenLabsClient("sk-key-12345").stream("Hello there.", "voice/1", "eleven_flash_v2_5", 24000))
    assert chunks == [pcm([1, 2]), pcm([3])] and requests.reply.closed
    (req,) = requests.sent
    assert req.full_url == "https://api.elevenlabs.io/v1/text-to-speech/voice%2F1/stream?output_format=pcm_24000"
    assert req.get_header("Xi-api-key") == "sk-key-12345" and req.method == "POST"
    assert json.loads(req.data) == {"text": "Hello there.", "model_id": "eleven_flash_v2_5"}


def test_stopping_ends_the_stream_early_and_closes_it(requests):
    stop = threading.Event()
    requests.reply = Resp(chunks=[pcm([1]), pcm([2]), pcm([3])])
    got = []
    for chunk in el.ElevenLabsClient("k" * 10).stream("x", "v", "m", 24000, stop):
        got.append(chunk)
        stop.set()
    assert len(got) == 1 and requests.reply.closed


@pytest.mark.parametrize("error, fatal, text", [
    (http_error(401, {"status": "invalid_api_key", "message": "Invalid API key"}), True, "did not accept the API key: Invalid API key"),
    (http_error(429), False, "rate limiting"),
    (http_error(422, [{"msg": "bad voice"}]), True, "bad voice"),
    (http_error(500), False, "error 500"),
    (urllib.error.URLError("no route"), False, "could not reach ElevenLabs"),
])
def test_failures_say_what_went_wrong(requests, error, fatal, text):
    requests.reply = error
    with pytest.raises(el.ElevenLabsError, match=text) as info:
        list(el.ElevenLabsClient("k" * 10).stream("x", "v", "m", 24000))
    assert info.value.fatal is fatal


def test_unsupported_rates_are_refused_up_front(requests):
    with pytest.raises(el.ElevenLabsError, match="8000"):
        list(el.ElevenLabsClient("k" * 10).stream("x", "v", "m", 8000))
    assert requests.sent == []


def test_voices_are_listed_custom_first(requests):
    requests.reply = Resp(body=json.dumps({"voices": [
        {"voice_id": "a", "name": "Rachel", "category": "premade"}, {"voice_id": "b", "name": "My clone", "category": "cloned"},
        {"voice_id": "c", "name": "Adam", "category": "premade"}, {"name": "broken"}]}).encode())
    assert [v["name"] for v in el.ElevenLabsClient("k" * 10).voices()] == ["My clone", "Adam", "Rachel"]


# --- the engine ----------------------------------------------------------------------------------------------------
def test_engine_uses_the_chosen_voice_or_the_accounts_first(requests):
    settings = ["key-123456", None, "eleven_flash_v2_5"]
    engine = el.ElevenLabsEngine(lambda: tuple(settings))
    heard = []
    voices = json.dumps({"voices": [{"voice_id": "first", "name": "A", "category": "cloned"}]}).encode()
    requests.queue = [Resp(body=voices), Resp(chunks=[pcm([100, 200])])]       # lists the voices, then speaks
    engine.synth("Hi.", lambda samples, p: heard.append(samples) or 1, threading.Event())
    assert "/v1/voices" in requests.sent[0].full_url and "/text-to-speech/first/stream" in requests.sent[1].full_url
    assert len(heard) == 1 and heard[0].dtype == np.float32


def test_engine_without_a_key_fails_before_any_request(requests):
    with pytest.raises(el.ElevenLabsError, match="no ElevenLabs API key") as info:
        el.ElevenLabsEngine(lambda: (None, "v", "m")).synth("x", lambda s, p: 1, threading.Event())
    assert info.value.fatal and requests.sent == []


# --- keys ------------------------------------------------------------------------------------------------------------
@pytest.fixture
def creds(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(credentials, "registry_env", lambda name: None)
    for name in ("ELEVENLABS_API_KEY", "XI_API_KEY", "MY_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
                 "TWILIO_AUTH_TOKEN", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return credentials.Credentials(profiles.resolve("keys"))


def test_no_key_anywhere(creds):
    assert creds.elevenlabs() == (None, None) and creds.secret_values() == set()


def test_key_comes_from_the_environment(creds, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "  env-key-123456  ")
    assert creds.elevenlabs() == ("env-key-123456", "env:ELEVENLABS_API_KEY")


def test_the_variable_can_be_named_in_the_config_and_xi_is_a_fallback(creds, monkeypatch):
    monkeypatch.setenv("XI_API_KEY", "xi-key-123456")
    assert creds.elevenlabs()[1] == "env:XI_API_KEY"
    creds.forget_cache()
    creds.profile.config = profiles.validate({"voice": {"elevenlabs": {"api_key_env": "MY_KEY"}}}, creds.profile.config)
    monkeypatch.setenv("MY_KEY", "mine-key-123456")
    assert creds.elevenlabs() == ("mine-key-123456", "env:MY_KEY")


def test_a_key_set_in_windows_after_start_is_found_through_the_registry(creds, monkeypatch):
    monkeypatch.setattr(credentials, "registry_env", lambda name: "reg-key-123456" if name == "ELEVENLABS_API_KEY" else None)
    assert creds.elevenlabs() == ("reg-key-123456", "env:ELEVENLABS_API_KEY")


def test_a_saved_key_wins_and_can_be_forgotten(creds, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "env-key-123456")
    creds.set_elevenlabs("saved-key-123456")
    assert creds.elevenlabs() == ("saved-key-123456", "saved")
    assert json.loads(creds.profile.secrets_file.read_text())["elevenlabs_api_key"] == "saved-key-123456"
    assert creds.secret_values() == {"saved-key-123456"}
    creds.set_elevenlabs(None)
    assert creds.elevenlabs()[1] == "env:ELEVENLABS_API_KEY" and not creds.profile.secrets_file.exists()


def test_nonsense_is_not_saved_as_a_key(creds):
    for bad in ("", "short", "has a space in it 12345", "x" * 300):
        with pytest.raises(ValueError):
            creds.set_elevenlabs(bad)
    assert not creds.profile.secrets_file.exists()


# --- the voice choosing between the two ----------------------------------------------------------------------------
class FakeLocal:
    sample_rate = 24000

    def __init__(self):
        self.spoken = []

    def generate(self, text, sid=0, speed=1.0, callback=None):
        self.spoken.append(text)
        callback(np.full(240, 0.1, np.float32), 0.0)


class FakeCloud:
    sample_rate = 24000

    def __init__(self, fail=None, partial=False):
        self.spoken, self.fail, self.partial = [], fail, partial

    def synth(self, text, cb, stop):
        self.spoken.append(text)
        if self.partial:
            cb(np.full(240, 0.3, np.float32), 0.0)
        if self.fail:
            raise self.fail
        if not self.partial:
            cb(np.full(480, 0.3, np.float32), 0.0)


def make_voice(**kw):
    kw.setdefault("tts", FakeLocal())
    return Voice("x", engine="kokoro", stream=False, **kw)


def wait_idle(v, timeout=5):
    end = time.time() + timeout
    time.sleep(0.05)
    while time.time() < end and (v._working or not v._queue.empty()):
        time.sleep(0.02)


def test_the_cloud_voice_speaks_when_preferred():
    cloud, local = FakeCloud(), FakeLocal()
    v = make_voice(tts=local, cloud=cloud, prefer_cloud=lambda: True)
    v.say("Hello there.")
    wait_idle(v)
    assert cloud.spoken == ["Hello there."] and local.spoken == [] and v.player.written >= 480


def test_the_local_voice_speaks_when_the_cloud_is_not_wanted():
    cloud, local = FakeCloud(), FakeLocal()
    v = make_voice(tts=local, cloud=cloud, prefer_cloud=lambda: False)
    v.say("Hello there.")
    wait_idle(v)
    assert cloud.spoken == [] and local.spoken == ["Hello there."]


def test_a_cloud_failure_falls_back_to_the_local_voice_and_is_reported():
    errors, local = [], FakeLocal()
    cloud = FakeCloud(fail=el.ElevenLabsError("quota used up", 429))
    v = make_voice(tts=local, cloud=cloud, prefer_cloud=lambda: True, on_cloud_error=errors.append)
    v.say("Hello there.")
    wait_idle(v)
    assert local.spoken == ["Hello there."] and [str(e) for e in errors] == ["quota used up"]


def test_half_a_sentence_is_not_said_twice():
    local = FakeLocal()
    cloud = FakeCloud(fail=el.ElevenLabsError("stream broke"), partial=True)
    v = make_voice(tts=local, cloud=cloud, prefer_cloud=lambda: True)
    v.say("Hello there.")
    wait_idle(v)
    assert local.spoken == []


def test_a_voice_can_run_on_the_cloud_alone_and_gain_the_local_models_later(monkeypatch):
    cloud = FakeCloud()
    v = Voice("x", engine="kokoro", stream=False, local=False, cloud=cloud, prefer_cloud=lambda: True)
    assert v.tts is None and v.sr == 24000
    v.say("No local models yet.")
    wait_idle(v)
    assert cloud.spoken == ["No local models yet."]
    local = FakeLocal()
    monkeypatch.setattr(v, "_make_tts", lambda: local)
    assert v.attach_local() and v.tts is local


def test_a_voice_needs_something_to_speak_with():
    with pytest.raises(ValueError):
        Voice("x", engine="kokoro", stream=False, local=False)


# --- the face: the key never leaves the process ---------------------------------------------------------------------
def test_the_page_only_learns_whether_there_is_a_key(tmp_path, monkeypatch):
    from wireface.app import App

    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DEFAULT_CWD", tmp_path)
    monkeypatch.setattr(credentials, "registry_env", lambda name: None)
    for name in ("ELEVENLABS_API_KEY", "XI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    app = App(profiles.resolve("page"), shell=False)

    async def no_voice():
        return None

    monkeypatch.setattr(app, "_ensure_voice", no_voice)
    assert app.config_view()["elevenlabs"] == {"available": False, "source": None, "using": False, "models": el.MODELS}
    asyncio.run(app.set_elevenlabs_key("sk-secret-123456"))
    view = app.config_view()
    assert view["elevenlabs"]["available"] and view["elevenlabs"]["source"] == "saved" and view["elevenlabs"]["using"]
    assert "sk-secret" not in json.dumps(view) and "sk-secret" not in json.dumps(app.status())
    assert app.status()["voice_engine"] == "elevenlabs"
    ev = app.activity.emit("tool", name="Bash", output="echo sk-secret-123456")
    assert "sk-secret" not in ev.output
    asyncio.run(app.set_config({"voice": {"engine": "local"}}))
    assert app.status()["voice_engine"] == "local"
    asyncio.run(app.set_elevenlabs_key(None))
    assert not app.config_view()["elevenlabs"]["available"]
