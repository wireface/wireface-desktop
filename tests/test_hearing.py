"""The microphone's per-block work (hearing.Ears) with stand-in models, and the face stopping when it hears its name."""
import asyncio
import collections

import numpy as np
import pytest

from wireface import config, hearing, profile as profiles
from wireface.hearing import BLOCK, Ears, VoiceLevel, level_db, name_keywords, sounds_like

LEX = {"CLAUDE": ["K", "L", "AO1", "D"], "OPEN": ["OW1", "P", "AH0", "N"], "CODE": ["K", "OW1", "D"],
       "CLOUDY": ["K", "L", "AW1", "D", "IY0"], "CLAUSE": ["K", "L", "AO1", "Z"], "TESTS": ["T", "EH1", "S", "T", "S"],
       "PASS": ["P", "AE1", "S"], "PARTLY": ["P", "AA1", "R", "T", "L", "IY0"], "INCLUDE": ["IH0", "N", "K", "L", "UW1", "D"]}


def test_name_keywords_one_pronunciation_per_name():
    # a second vowel ("K L AA1 D") made the face stop on its own "partly cloudy"
    assert name_keywords(["Claude"], LEX) == ["K L AO1 D @CLAUDE"]
    assert name_keywords(["open code"], LEX) == ["OW1 P AH0 N K OW1 D @OPEN_CODE"]
    assert name_keywords(["", "42"], LEX) == []


def test_words_that_sound_like_the_name():
    assert sounds_like("partly cloudy", ["Claude"], LEX)          # one sound off: the spotter can take it for the name
    assert sounds_like("the clause", ["Claude"], LEX) and sounds_like("include", ["Claude"], LEX)
    assert not sounds_like("the tests pass", ["Claude"], LEX)


def test_level_and_the_voice_level_filter():
    loud = np.full(16000, 0.1, np.float32)
    assert abs(level_db(loud) - 20 * np.log10(0.1)) < 0.1
    v = VoiceLevel(margin_db=14)
    assert v.accepts(-60)                                   # nothing learned yet: everything counts
    v.add(-20)
    v.add(-22)
    assert v.accepts(-30) and not v.accepts(-40)            # a TV 20 dB below you is someone else


class FakeVad:
    """Speech is "detected" while the block's level is above a threshold; a segment ends after 2 quiet blocks."""

    def __init__(self):
        self.buf, self.quiet, self.segments, self.resets = [], 0, collections.deque(), 0

    def reset(self):
        self.buf, self.quiet = [], 0
        self.resets += 1

    def accept_waveform(self, x):
        loud = np.abs(x).max() > 0.01
        if loud:
            self.buf.append(x)
            self.quiet = 0
        elif self.buf:
            self.quiet += 1
            if self.quiet >= 2:
                self.segments.append(np.concatenate(self.buf))
                self.buf, self.quiet = [], 0

    def is_speech_detected(self):
        return bool(self.buf)

    def empty(self):
        return not self.segments

    @property
    def front(self):
        return type("S", (), {"samples": self.segments[0]})()

    def pop(self):
        self.segments.popleft()


class FakeSpotter:
    """Says the name was heard on chosen blocks (one decode step per block)."""

    def __init__(self, on_blocks):
        self.on_blocks, self.n, self._ready = set(on_blocks), 0, False

    def create_stream(self):
        return type("Stream", (), {"accept_waveform": lambda s, sr, x: None})()

    def is_ready(self, s):
        self._ready = not self._ready
        if self._ready:
            self.n += 1
        return self._ready

    def decode_stream(self, s):
        pass

    def get_result(self, s):
        return "@CLAUDE" if self.n in self.on_blocks else ""

    def reset_stream(self, s):
        pass


def speech(level=0.1):
    return np.full(BLOCK, level, np.float32)


def texts(events):
    return [e[1] for e in events if e[0] == "text"]


def quiet():
    return np.zeros(BLOCK, np.float32)


def test_the_name_is_heard_even_while_the_face_talks_and_opens_the_microphone():
    vad = FakeVad()
    ears = Ears(vad, lambda a: f"{len(a)} samples", spotter=FakeSpotter({3}), focus=False)
    events = []
    t = 0.0
    for i in range(1, 6):                                   # the face is talking: deaf, but the name gets through
        t += 0.05
        events += ears.block(speech(), t, speaking=True)
    assert ("name", "@CLAUDE") in events and not any(e[0] == "text" for e in events)
    ears.listen_now(t)                                      # the app stopped the voice
    assert len(vad.buf) > 0                                 # the moment before the name was kept for the detector
    for _ in range(4):                                      # the user keeps talking while the voice is still fading
        t += 0.05
        events += ears.block(speech(), t, speaking=True)
    for _ in range(3):
        t += 0.05
        events += ears.block(quiet(), t, speaking=False)
    assert any(e[0] == "text" for e in events)


def test_while_the_face_talks_nothing_else_is_heard():
    ears = Ears(FakeVad(), lambda a: "heard", focus=False)
    out = []
    for i in range(10):
        out += ears.block(speech(), i * 0.05, speaking=True)
    for i in range(10, 13):
        out += ears.block(quiet(), i * 0.05, speaking=False)
    assert out == []


def test_quieter_voices_are_dropped_once_your_level_is_known():
    ears = Ears(FakeVad(), lambda a: "words", focus=True)
    t, out = 0.0, []

    def say(level):
        nonlocal t
        res = []
        for _ in range(6):
            t += 0.05
            res += ears.block(speech(level), t, speaking=False)
        for _ in range(3):
            t += 0.05
            res += ears.block(quiet(), t, speaking=False)
        return res

    assert texts(say(0.1)) == ["words"] and texts(say(0.1)) == ["words"]
    far = say(0.012)                                        # about 18 dB quieter: a TV across the room
    assert not texts(far) and any(e[0] == "dropped" for e in far)
    assert texts(say(0.05)) == ["words"]                    # you, a bit softer: still you


def test_the_denoiser_feeds_detection_but_not_the_name_spotter():
    seen = []

    class Denoiser:
        def run(self, x, sr):
            seen.append(float(np.abs(x).max()))
            return type("R", (), {"samples": np.zeros_like(x)})()

    vad = FakeVad()
    ears = Ears(vad, lambda a: "x", denoiser=Denoiser(), focus=False)
    ears.block(speech(), 0.05, speaking=False)
    assert seen == [pytest.approx(0.1)] and not vad.buf       # the detector got the cleaned (here: silent) audio


# --- the face hears its name -----------------------------------------------------------------------------------------
class SpokenVoice:
    """A voice whose player is playing ``spoken`` lines right now."""

    def __init__(self, lines, sr=24000):
        from wireface.lipsync import wall_clock

        self.spoken = collections.deque(lines)
        self.player = type("P", (), {})()
        self.player.sr = sr
        self.player.written = lines[-1][0] + sr * 3 if lines else 0
        self.player.clock = (lines[-1][0] + sr // 2 if lines else 0, wall_clock(), None)
        self.busy = True
        self.stopped = 0

    def stop(self):
        self.stopped += 1
        self.busy = False


@pytest.fixture
def app(tmp_path, monkeypatch):
    from wireface.app import App

    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DEFAULT_CWD", tmp_path)
    a = App(profiles.resolve("names"), shell=False)
    a.sent = []
    monkeypatch.setattr(a.backend, "send", lambda text: a.sent.append(text))
    a.opened = []
    monkeypatch.setattr(a.hearing, "listen_now", lambda: a.opened.append(1))
    return a


def test_saying_the_name_stops_the_voice_and_listens(app):
    app.voice = SpokenVoice([(0, "I've updated the parser."), (48000, "The tests pass now.")])
    assert app._name_heard("@CLAUDE")
    assert app.voice.stopped and app.opened
    assert any(e["kind"] == "notice" and "its name" in e["summary"] for e in app.activity.recent())


def test_the_faces_own_name_coming_out_of_the_speakers_does_not_stop_it(app):
    app.voice = SpokenVoice([(0, "Hello."), (48000, "I'm Claude, and I fixed the parser.")])
    assert not app._name_heard("@CLAUDE")
    assert not app.voice.stopped and not app.opened


def test_the_name_when_nothing_is_said_does_nothing(app):
    app.voice = SpokenVoice([(0, "Hello.")])
    app.voice.busy = False
    assert not app._name_heard("@CLAUDE")


def test_addressing_it_by_name_while_the_agent_works_replaces_the_request(app, monkeypatch):
    halted = []

    async def halt():
        halted.append(1)

    monkeypatch.setattr(app.backend, "halt", halt)
    monkeypatch.setattr(type(app.backend), "busy", property(lambda self: True))

    async def go():
        app.loop = asyncio.get_running_loop()
        await app._apply_realtime()                                 # (the test stand-in for the realtime voice)
        app.realtime.state = "connected"
        app.heard("Claude, ignore that and tell me the weather in Paris.")
        await asyncio.sleep(0)
        assert halted and app.sent == ["Claude, ignore that and tell me the weather in Paris."]
        halted.clear()
        app.sent.clear()
        app.heard("and the humidity too")                           # not addressed: it waits behind the work
        await asyncio.sleep(0)
        assert not halted and app.sent == ["and the humidity too"]
        app._named_at = __import__("time").monotonic()               # just stopped it by name: the next words steer
        app.heard("ignore that and tell me the weather in Paris")
        await asyncio.sleep(0)
        assert halted

    asyncio.run(go())


def test_just_the_name_is_not_sent_to_the_agent(app):
    app.heard("Claude?")
    app.heard("Hey Claude.")
    assert app.sent == []
    app.heard("Claude, use the other file instead.")
    assert app.sent == ["Claude, use the other file instead."]


def test_the_faces_own_sound_alike_word_does_not_stop_it(app):
    app.voice = SpokenVoice([(0, "Hello."), (48000, "Paris is about twenty degrees, partly cloudy and still.")])
    assert not app._name_heard("@CLAUDE")


def test_the_name_comes_from_the_settings(app):
    assert app.hearing.names == ["Claude"]                          # not recognition's misspellings ("clod")
    asyncio.run(app.set_config({"name": "Jarvis", "room": {"aliases": ["Jarvis bot"]}}))
    assert app.hearing.names == ["Jarvis", "Jarvis bot"]
    asyncio.run(app.set_config({"voice": {"hearing": {"name_interrupt": False}}}))
    assert app.hearing.names == []


# --- "show voice detector" -------------------------------------------------------------------------------------------
def test_the_name_heard_is_marked_on_the_detector():
    ears = Ears(FakeVad(), lambda a: "x", spotter=FakeSpotter({2}), focus=False)
    ears.metering = True
    for i in range(1, 4):
        ears.block(speech(), i * 0.05, speaking=False)
    assert [m["kind"] for m in ears.marks] == ["name"]


def test_showing_the_voice_detector_switches_the_meter_on_without_restarting_the_ears(app, monkeypatch):
    assert app.status()["detector"] is False and not app.hearing.meter
    app.hearing._changed.clear()
    asyncio.run(app.set_config({"voice": {"hearing": {"show_detector": True}}}))
    assert app.hearing.meter and not app.hearing._changed.is_set()   # the models aren't loaded again for it
    assert app.status()["detector"] is True
    msgs = []
    monkeypatch.setattr(app, "broadcast", msgs.append)

    async def go():
        app.loop = asyncio.get_running_loop()
        app._on_meter({"blocks": [[1.0, 3.2, 1, 0]], "marks": [], "room_ready": True, "gate": False, "focus": True})
        await asyncio.sleep(0)

    asyncio.run(go())
    assert msgs == [{"type": "meter", "blocks": [[1.0, 3.2, 1, 0]], "marks": [], "room_ready": True, "gate": False,
                     "focus": True}]


def test_the_hearing_thread_sends_what_it_measured_while_the_detector_is_shown(monkeypatch):
    import sys
    import types

    clock = {"t": 100.0, "reads": 0}
    readings, heard = [], []

    class Mic:
        def __init__(self, **kw):
            pass

        start = stop = close = lambda self: None

        def read(self, n):
            clock["reads"] += 1
            clock["t"] += 0.05
            if clock["reads"] >= 60:
                h.stop()
            loud = 10 <= clock["reads"] % 30 < 20                    # half a second of speech now and then
            return np.full((n, 1), 0.1 if loud else 0.0, np.float32), False

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=Mic))
    monkeypatch.setattr(hearing, "time", types.SimpleNamespace(monotonic=lambda: clock["t"]))
    monkeypatch.setattr(hearing.models, "missing_message", lambda *a: None)
    monkeypatch.setattr(hearing.speech, "make_offline_recognizer", lambda: None)
    monkeypatch.setattr(hearing.speech, "make_vad", FakeVad)
    monkeypatch.setattr(hearing.speech, "transcribe", lambda r, a: "words")
    monkeypatch.setattr(hearing, "make_denoiser", lambda: None)
    h = hearing.Hearing(lambda: None, lambda text, info: heard.append(text), lambda m: None, lambda: False,
                        on_meter=readings.append)
    h.configure(meter=True)
    h._run()                                                        # (the thread's body, here in the test)
    blocks = [b for r in readings for b in r["blocks"]]
    marks = [m for r in readings for m in r["marks"]]
    assert 20 <= len(readings) <= 31 and len(blocks) >= 55           # every 0.1 s, a couple of blocks each
    assert heard == ["words", "words"] and [m["fate"] for m in marks] == ["heard", "heard"]
    assert set(readings[-1]) == {"blocks", "marks", "room_ready", "gate", "focus"}
    h2 = hearing.Hearing(lambda: None, lambda text, info: None, lambda m: None, lambda: False, on_meter=readings.append)
    readings.clear()
    clock["reads"] = 0
    h = h2
    h._run()                                                        # the detector not shown: nothing is sent
    assert readings == []


# --- "go away" / "come back" ----------------------------------------------------------------------------------------
def test_telling_the_face_to_go_away_and_come_back(app):
    for said in ("Go away.", "Claude, go away", "go away, Claude", "Hey Claude, hide.", "Disappear please",
                 "get out of the way for a bit", "Claude, vanish."):
        assert app.face_command(said) is False, said
    for said in ("Come back.", "Claude, come back please", "come back claude", "Show yourself", "Where are you?"):
        assert app.face_command(said) is True, said
    for said in ("tell me how to make the dog go away", "hide the debug output", "come back to this later",
                 "Claude, why did the error go away?"):
        assert app.face_command(said) is None, said


def test_go_away_hides_the_face_without_bothering_the_agent(app, monkeypatch):
    halted = []

    async def halt():
        halted.append(1)

    monkeypatch.setattr(app.backend, "halt", halt)
    monkeypatch.setattr(type(app.backend), "busy", property(lambda self: True))
    events = []
    app.event = lambda topic, payload=None: events.append((topic, payload))

    async def go():
        app.loop = asyncio.get_running_loop()
        app.heard("Claude, go away.")
        await asyncio.sleep(0)
        assert not app.face_visible and ("face.visibility", {"visible": False}) in events
        assert app.status()["face_visible"] is False
        app.heard("come back")
        await asyncio.sleep(0)
        assert app.face_visible and events[-1] == ("face.visibility", {"visible": True})
        assert app.sent == [] and not halted                    # the work carries on, untouched

    asyncio.run(go())


def test_a_mangled_come_back_still_brings_the_face_back_while_it_is_away(app):
    # what recognition actually wrote when the user said "come back" (2026-10-07)
    mangled = ("Come", "Welcome back.", "You're not back. Show your face.", "back", "Claude, are you back?")
    for said in mangled:
        assert app.face_command(said) is None, said            # while the face is here these are for the agent
    app.face_visible = False
    for said in mangled:
        assert app.face_command(said) is True, said
    for said in ("Quest", "go back to the previous file", "show me the test results", "welcome back the old parser"):
        assert app.face_command(said) is None, said            # other words in it: still for the agent
    assert app.face_command("Show me your face, please.") is True
    app.face_visible = True
    assert app.face_command("can you go away for a bit") is False
    assert app.face_command("the error went away") is None
