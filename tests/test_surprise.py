"""How far speech stands out from the room (surprise.RoomSound), and the hearing's "ignore constant background sound"."""
import numpy as np

from wireface import surprise
from wireface.hearing import BLOCK, Ears, VoiceLevel
from wireface.surprise import RoomSound, band_db, stands_out

SR = surprise.SAMPLE_RATE
rng = np.random.default_rng(7)


def speechlike(seconds: float, rms: float) -> np.ndarray:
    """Noise in the speech band (300-3000 Hz), switched on and off at a syllable rate, at about ``rms`` while on."""
    n = int(seconds * SR)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    spec[(f < 300) | (f > 3000)] = 0
    x = np.fft.irfft(spec, n)
    x /= np.sqrt(np.mean(x ** 2)) + 1e-12
    env = (np.sin(2 * np.pi * 4 * np.arange(n) / SR) > -0.3).astype(np.float32)
    return (x * env * rms).astype(np.float32)


def feed(room: RoomSound, x: np.ndarray):
    for i in range(0, len(x) - BLOCK + 1, BLOCK):
        room.observe(x[i:i + BLOCK])


def test_band_energies():
    assert band_db(np.zeros(BLOCK)).shape == (surprise.BANDS,)
    assert (band_db(np.zeros(BLOCK)) == surprise.FLOOR_DB).all()
    two = band_db(np.stack([np.zeros(BLOCK), speechlike(0.05, 0.1)]))
    assert two.shape == (2, surprise.BANDS) and two[1].mean() > two[0].mean() + 50
    assert len(surprise.SPEECH_BANDS) >= 8


def test_nothing_is_judged_until_the_room_has_been_heard():
    room = RoomSound()
    assert room.lift(speechlike(1, 0.1)) is None and stands_out(None) == "unknown"
    feed(room, np.zeros(int(2 * SR), np.float32))
    assert room.lift(speechlike(1, 0.1)) is None                   # 2 s is not enough yet


def test_in_a_quiet_room_you_stand_out():
    room = RoomSound()
    feed(room, (rng.standard_normal(10 * SR) * 3e-4).astype(np.float32))
    assert stands_out(room.lift(speechlike(2, 0.05))) == "strongly"


def test_a_tv_that_keeps_talking_becomes_the_background_and_you_still_stand_out_over_it():
    room = RoomSound()
    tv = speechlike(70, 0.01)
    feed(room, tv[: 60 * SR])
    assert stands_out(room.lift(tv[60 * SR: 63 * SR])) == "no"
    you = speechlike(2, 0.05) + tv[63 * SR: 65 * SR]               # 14 dB above the TV, over it
    assert stands_out(room.lift(you)) in ("strongly", "somewhat")


def test_the_live_reading_block_by_block():
    room = RoomSound()
    feed(room, (rng.standard_normal(2 * SR) * 3e-4).astype(np.float32))
    assert room.now_lift() is None and not room.ready                 # still learning the room
    feed(room, (rng.standard_normal(8 * SR) * 3e-4).astype(np.float32))
    assert room.ready and abs(room.now_lift()) < 6                     # the room itself: about its usual level
    room.observe(speechlike(0.05, 0.05)[:BLOCK] + 0.0)
    assert room.now_lift() >= surprise.STRONG_DB                       # a voice close by stands out at once


def test_the_room_is_forgotten_after_a_minute():
    room = RoomSound()
    feed(room, speechlike(60, 0.05))                                 # a loud minute
    feed(room, (rng.standard_normal(61 * SR) * 3e-4).astype(np.float32))    # then a quiet one
    assert stands_out(room.lift(speechlike(2, 0.01))) == "strongly"


# --- the hearing's gate ----------------------------------------------------------------------------------------------
class LevelVad:
    """Speech while a block is loud (abs max over 0.01); a segment ends after 2 quiet blocks."""

    def __init__(self):
        self.buf, self.quiet, self.segments = [], 0, []

    def reset(self):
        self.buf, self.quiet = [], 0

    def accept_waveform(self, x):
        if np.abs(x).max() > 0.01:
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
        self.segments.pop(0)


class Room:
    """Drives an Ears: a TV that keeps talking (utterances with short gaps), and the user now and then."""

    def __init__(self, gate: bool):
        self.ears = Ears(LevelVad(), lambda a: "words", focus=False, gate=gate)
        self.t = 0.0

    def say(self, audio: np.ndarray) -> list[tuple]:
        out = []
        audio = np.concatenate([audio, np.zeros(3 * BLOCK, np.float32)])
        for i in range(0, len(audio) - BLOCK + 1, BLOCK):
            self.t += 0.05
            out += self.ears.block(audio[i:i + BLOCK], self.t, speaking=False)
        return [e for e in out if e[0] != "voice"]                     # (someone is talking: not what is tested)

    def tv_for(self, seconds: float):
        for _ in range(int(seconds / 1.15)):
            self.say(np.abs(speechlike(1.0, 0.01)) + 0.012)            # always above the detector's threshold


def test_with_the_gate_on_the_tv_is_dropped_before_recognition_and_you_are_heard():
    room = Room(gate=True)
    room.tv_for(70)
    out = room.say(np.abs(speechlike(1.0, 0.01)) + 0.012)
    assert [e[0] for e in out] == ["background"]                    # never transcribed
    out = room.say(np.abs(speechlike(1.5, 0.08)) + 0.012)            # you, close to the microphone
    texts = [e for e in out if e[0] == "text"]
    assert len(texts) == 1 and texts[0][2]["stands_out"] in ("strongly", "somewhat")
    out = room.say(np.abs(speechlike(1.0, 0.01)) + 0.012)            # the TV just after you spoke: you may go on
    assert [e[0] for e in out] == ["text"] and out[0][2]["stands_out"] == "no"


def test_with_the_gate_off_everything_is_transcribed_as_before():
    room = Room(gate=False)
    room.tv_for(70)
    out = room.say(np.abs(speechlike(1.0, 0.01)) + 0.012)
    assert [e[0] for e in out] == ["text"] and out[0][1] == "words"
    assert out[0][2]["stands_out"] == "no"                           # measured all the same, for the judge


def fates(room: Room) -> list[str]:
    return [m["fate"] for m in room.ears.marks if m["kind"] == "utterance"]


def test_the_voice_detector_meters_each_block_and_says_what_became_of_each_utterance():
    room = Room(gate=False)
    room.ears.metering = True
    room.tv_for(70)
    blocks = room.ears.meter
    assert len(blocks) > 1000 and all(len(b) == 4 for b in blocks)
    assert blocks[0][1] is None and blocks[-1][1] is not None          # nothing until the room is learned, then a reading
    assert any(b[2] for b in blocks) and not any(b[3] for b in blocks)  # speech was heard; the face never talked
    room.ears.marks.clear()
    room.ears.engaged_at = -1e9
    room.ears.level = VoiceLevel()                                     # (with the filter off it learned the TV as "you")
    room.say(np.abs(speechlike(1.0, 0.01)) + 0.012)                   # more TV: the filter is off, so it still goes on
    room.say(np.abs(speechlike(1.5, 0.08)) + 0.012)                   # you
    room.say(np.abs(speechlike(1.0, 0.01)) + 0.012)                   # the TV just after: kept, you may be going on
    tv, you, after = [m for m in room.ears.marks if m["kind"] == "utterance"]
    assert tv["fate"] == "would" and tv["stands_out"] == "no" and tv["text"] == "words"
    assert you["fate"] == "heard" and you["stands_out"] in ("strongly", "somewhat") and you["lift"] > tv["lift"]
    assert after["fate"] == "heard" and after["why"] == "in a conversation"
    assert you["dur"] >= 1.5 and you["t"] < room.t                     # placed where it was said, before it was handed over
    room.ears.block(np.zeros(BLOCK, np.float32), room.t + 0.05, speaking=True)
    assert room.ears.meter[-1][1:] == [None, 0, 1]                     # the face talking: it doesn't listen


def test_the_detector_shows_what_the_filters_dropped():
    room = Room(gate=True)
    room.ears.metering = True
    room.tv_for(70)
    room.ears.marks.clear()
    room.ears.engaged_at = -1e9
    room.say(np.abs(speechlike(1.0, 0.01)) + 0.012)
    assert fates(room) == ["background"] and room.ears.marks[0]["text"] is None
    quiet = Room(gate=False)
    quiet.ears.metering, quiet.ears.focus = True, True
    for _ in range(2):
        quiet.say(np.abs(speechlike(1.5, 0.08)) + 0.012)              # your level
    quiet.say(np.full(BLOCK * 20, 0.0105, np.float32))                # ~18 dB quieter: someone across the room
    assert fates(quiet) == ["heard", "heard", "distant"] and quiet.ears.marks[-1]["vs_user"] < -14


def test_without_the_detector_nothing_is_kept():
    room = Room(gate=False)
    room.tv_for(10)
    assert room.ears.meter == [] and room.ears.marks == []


def test_the_name_gets_through_the_gate():
    room = Room(gate=True)
    room.tv_for(70)
    room.ears.named_at = room.t                                     # the spotter heard the name just now
    room.t += 9                                                     # (but no conversation going on)
    room.ears.engaged_at = -1e9
    out = room.say(np.abs(speechlike(1.0, 0.01)) + 0.012)
    assert [e[0] for e in out] == ["background"]                    # the name was too long ago
    room.ears.named_at = room.t
    out = room.say(np.abs(speechlike(1.0, 0.01)) + 0.012)
    assert [e[0] for e in out] == ["text"] and out[0][2]["named"]
