"""The microphone: voice activity detection, then a transcript of each finished utterance.

There is no wake word: while listening is on, whatever is said near the microphone is sent to the agent. The
microphone is ignored while the face is speaking (and for a moment after), so it never hears itself, with one
exception: **its name**. A small streaming keyword spotter listens all the time, and when it hears the face's name
(``on_name``) the face stops talking and the microphone opens at once (``listen_now``), so you can cut in and steer.

Each 50 ms block of audio goes through:

    keyword spotter (the name)      on the raw audio: it copes with noise better than with denoised audio
    denoiser (GTCRN, streaming)     removes steady noise (fans, hum, hiss, typing) before the next two steps
    voice activity detector         finds where speech starts and ends
    speech recognition (Parakeet)   transcribes each finished utterance
    voice level filter              drops utterances far quieter than your own voice (a TV, people across the room)
    room sound (``surprise``)       how far each utterance stands out from what the room usually sounds like; with
                                    "ignore constant background sound" on, one that doesn't is dropped before
                                    recognition (unless the face is in a conversation, or you said its name);
                                    "show voice detector" draws it live on the face and in the settings

The denoiser and the spotter are optional: without their models (or with them turned off) the rest works as before.
Each transcript comes with what was measured on the way (``info``: its level against yours, how much it stood out),
for the judge (``judge.py``) to weigh. ``Ears`` does the per-block work and is what the tests drive; ``Hearing`` owns
the microphone thread.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import tempfile
import threading
import time

import numpy as np

from . import config, models, speech
from .surprise import RoomSound, stands_out

log = logging.getLogger(__name__)

BLOCK = 800                  # 50 ms at 16 kHz: how often the name can be noticed
DEAF_AFTER_SPEECH_S = 0.5    # the room still rings with the voice for a moment
MIN_UTTERANCE_S = 0.3
OPEN_AFTER_NAME_S = 1.5      # after the name, listen even if the voice is still fading out
PRE_ROLL_S = 0.25            # audio from before the name was noticed, given to the detector when it opens
# The spotter works in 160 ms chunks, so how quickly it notices the name depends on where the word falls in a chunk.
# A second stream, fed the same audio this much later, falls the other way: whichever notices first wins (measured:
# median 0.29 s after the word instead of 0.35 s, for ~1.5 ms more work per block).
SECOND_STREAM_LAG_S = 0.08
FOCUS_DB = 14.0              # "ignore distant voices": utterances this much quieter than your voice are dropped
FOCUS_HISTORY = 10           # how many of your recent utterances set your level
NAME_REFRACTORY_S = 1.0      # one name, one interrupt
# "ignore constant background sound": while the face is in a conversation (it spoke, its name was said, or something
# stood out from the room or was said at your level this recently) nothing is dropped for sounding like the room
CONVERSATION_WINDOW_S = 8.0
# ... and speech at your own level is never dropped for it: after a minute of dictation your voice is a large part of
# what the room sounds like (measured: it stood out by only -2..+6 dB), but it is still you
OWN_LEVEL_DB = 3.0
# "show voice detector": what each block measured goes to the page this often; an utterance ends this long before the
# voice detector hands it over (its minimum silence, speech.make_vad)
METER_EVERY_S = 0.1
VAD_TAIL_S = 0.6


def level_db(audio: np.ndarray) -> float:
    """The level of speech in ``audio`` (dBFS): the RMS of its louder half of 20 ms frames (pauses don't count)."""
    n = 320
    if len(audio) < n:
        return -120.0
    frames = audio[: len(audio) // n * n].reshape(-1, n)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    loud = np.sort(rms)[len(rms) // 2:]
    return float(20 * np.log10(np.sqrt(np.mean(loud ** 2)) + 1e-9))


class VoiceLevel:
    """Your speaking level, learned from what was accepted; quieter voices (much quieter) are someone else."""

    def __init__(self, margin_db: float = FOCUS_DB, history: int = FOCUS_HISTORY):
        self.margin = margin_db
        self.levels: collections.deque[float] = collections.deque(maxlen=history)

    @property
    def reference(self) -> float | None:
        if len(self.levels) < 2:
            return None
        return float(np.percentile(self.levels, 75))

    def accepts(self, db: float) -> bool:
        ref = self.reference
        return ref is None or db >= ref - self.margin

    def add(self, db: float) -> None:
        self.levels.append(db)


def _name_words(name: str) -> list[str]:
    return [w for w in name.replace("-", " ").split() if w.replace("'", "").isalpha()]


def name_keywords(names: list[str], lex: dict[str, list[str]] | None = None) -> list[str]:
    """Keyword-spotter lines (``"K L AO1 D @CLAUDE"``): one per name, as the dictionary says it. Extra spellings or
    vowels cost false stops: with "Claude" also as "K L AA1 D", the face stopped on its own "partly cloudy" in 11 of 15
    tests (3 of 15 without), and it caught the user no better. Add other names in the settings instead."""
    from .phonemes import word_phones

    lines: list[str] = []
    for name in names:
        words = _name_words(name)
        phones = [p for w in words for p in word_phones(w, lex)]
        if phones:
            lines.append(f"{' '.join(phones)} @{'_'.join(w.upper() for w in words)}")
    return list(dict.fromkeys(lines))


def _edit_distance(a: list[str], b: list[str]) -> int:
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def sounds_like(text: str, names: list[str], lex: dict[str, list[str]] | None = None) -> bool:
    """Whether ``text`` has a word that sounds like one of ``names`` (one sound off, anywhere in the word): the
    spotter can take "clause", "cloud" or "clawed" for "Claude", so the face saying one of them is not the user."""
    from .phonemes import word_phones

    strip = lambda ps: [p.rstrip("012") for p in ps]
    targets = [strip([p for w in _name_words(n) for p in word_phones(w, lex)]) for n in names]
    targets = [t for t in targets if t]
    for word in re.findall(r"[A-Za-z']+", text):
        ph = strip(word_phones(word, lex))
        for t in targets:
            n = len(t)
            windows = [ph[i:i + n] for i in range(max(1, len(ph) - n + 1))]
            if any(_edit_distance(w, t) <= 1 for w in windows):
                return True
    return False


def make_spotter(lines: list[str]):
    """The streaming keyword spotter for these keyword lines (the small zipformer KWS model), or None."""
    import sherpa_onnx as so

    k = config.KWS_DIR
    # the 16-frame chunk model: in tests with a talking face it caught the name 20 times out of 20, ~0.4 s after it
    # was said (the 8-frame one is ~0.05 s quicker but missed more)
    files = {"encoder": k / "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
             "decoder": k / "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
             "joiner": k / "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx", "tokens": k / "tokens.txt"}
    if not lines or not all(p.is_file() for p in files.values()):
        return None
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="wireface-kws-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return so.KeywordSpotter(tokens=str(files["tokens"]), encoder=str(files["encoder"]), decoder=str(files["decoder"]),
                                 joiner=str(files["joiner"]), keywords_file=path, num_threads=1, keywords_score=2.0,
                                 keywords_threshold=0.15, num_trailing_blanks=0)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def make_denoiser():
    """The streaming GTCRN denoiser, or None if its model is not installed."""
    import sherpa_onnx as so

    if not config.DENOISER_MODEL.is_file():
        return None
    m = so.OfflineSpeechDenoiserModelConfig(num_threads=1)
    m.gtcrn.model = str(config.DENOISER_MODEL)
    return so.OnlineSpeechDenoiser(so.OnlineSpeechDenoiserConfig(model=m))


class Ears:
    """What happens to each block of microphone audio. ``block`` returns events: ("voice",) while someone is speaking,
    ("name", word) when the face's name is heard, ("text", transcript, info) for each finished utterance, ("dropped",
    db) for one the level filter turned away, ("background", lift_db) for one that sounded like the room (only with
    ``gate`` on: "ignore constant background sound").

    With ``metering`` on ("show voice detector") it also keeps, for the page, ``meter``: one ``[t, lift_db, speech,
    deaf]`` per block (how far the block stands out from the room, whether the voice detector hears speech, whether the
    face is talking over it), and ``marks``: what became of each utterance (``fate``: heard, would (the background
    filter would have dropped it, but it is off), background, distant, empty) and when the name was heard. The
    hearing thread collects both every ``METER_EVERY_S``."""

    def __init__(self, vad, recognize, denoiser=None, spotter=None, focus: bool = True, gate: bool = False):
        self.vad = vad
        self.recognize = recognize                  # audio -> text
        self.denoiser = denoiser
        self.spotter = spotter
        self._streams = [spotter.create_stream(), spotter.create_stream()] if spotter is not None else []
        self._lag = np.zeros(int(SECOND_STREAM_LAG_S * speech.SAMPLE_RATE), np.float32)
        self.focus = focus
        self.level = VoiceLevel()
        self.gate = gate
        self.room = RoomSound()
        self.engaged_at = -1e9                      # the face spoke, its name was said, or something stood out
        self.ring: collections.deque[np.ndarray] = collections.deque(maxlen=max(1, int(PRE_ROLL_S * speech.SAMPLE_RATE / BLOCK)))
        self.deaf_until = 0.0
        self.open_until = 0.0
        self.voiced_at = 0.0
        self.named_at = -1e9
        self.metering = False
        self.meter: list[list] = []
        self.marks: list[dict] = []

    def _mark(self, now: float, seconds: float, fate: str, lift: float | None, vs_user: float | None = None,
              text: str | None = None, why: str | None = None) -> None:
        self.marks.append({"kind": "utterance", "t": round(now - VAD_TAIL_S, 3), "dur": round(seconds, 2), "fate": fate,
                           "lift": None if lift is None else round(lift, 1), "stands_out": stands_out(lift),
                           "vs_user": None if vs_user is None else round(vs_user, 1), "text": text, "why": why})

    def listen_now(self, now: float) -> None:
        """The name was heard: listen from here (the voice is being stopped), including the moment just before."""
        self.open_until = now + OPEN_AFTER_NAME_S
        self.deaf_until = 0.0
        self.vad.reset()
        for old in self.ring:
            self.vad.accept_waveform(old)
        self.ring.clear()

    def block(self, raw: np.ndarray, now: float, speaking: bool) -> list[tuple]:
        events: list[tuple] = []
        if self._streams:
            late = np.concatenate([self._lag, raw])
            self._lag = late[len(raw):]
            for stream, audio in zip(self._streams, (raw, late[:len(raw)])):
                stream.accept_waveform(speech.SAMPLE_RATE, audio)
                while self.spotter.is_ready(stream):
                    self.spotter.decode_stream(stream)
                    word = self.spotter.get_result(stream)
                    if word:
                        self.spotter.reset_stream(stream)
                        if now - self.named_at >= NAME_REFRACTORY_S:
                            self.named_at = self.engaged_at = now
                            events.append(("name", word))
                            if self.metering:
                                self.marks.append({"kind": "name", "t": round(now, 3)})
        x = raw
        if self.denoiser is not None:
            out = np.asarray(self.denoiser.run(raw, speech.SAMPLE_RATE).samples, dtype=np.float32)
            if len(out):
                x = out
        if speaking:
            self.engaged_at = now
        if speaking and now >= self.open_until:
            self.deaf_until = now + DEAF_AFTER_SPEECH_S
        if now < self.deaf_until:
            self.vad.reset()
            self.ring.append(x)
            if self.metering:
                self.meter.append([round(now, 3), None, 0, 1])
            return events
        self.room.observe(x)                        # (the face's own voice is not the room)
        self.vad.accept_waveform(x)
        voiced = self.vad.is_speech_detected()
        if self.metering:
            lift_now = self.room.now_lift()
            self.meter.append([round(now, 3), None if lift_now is None else round(lift_now, 1), int(voiced), 0])
        if voiced and now - self.voiced_at > 0.4:
            if not self.focus or self.level.accepts(level_db(x)):
                self.voiced_at = now
                events.append(("voice",))
        while not self.vad.empty():
            audio = np.asarray(self.vad.front.samples, dtype=np.float32)
            self.vad.pop()
            if len(audio) < MIN_UTTERANCE_S * speech.SAMPLE_RATE:
                continue
            seconds = len(audio) / speech.SAMPLE_RATE
            db = level_db(audio)
            ref = self.level.reference
            vs_user = None if ref is None else db - ref
            if self.focus and not self.level.accepts(db):
                events.append(("dropped", db))
                if self.metering:
                    self._mark(now, seconds, "distant", self.room.lift(audio), vs_user)
                continue
            lift = self.room.lift(audio)
            standing = stands_out(lift)
            named = now - self.named_at < seconds + 1.0           # its name was heard in it
            talking = now - self.engaged_at < CONVERSATION_WINDOW_S + seconds
            yours = vs_user is not None and vs_user >= -OWN_LEVEL_DB
            if standing in ("strongly", "somewhat") or yours:
                self.engaged_at = now
            like_the_room = standing == "no" and not (named or talking or yours)
            if self.gate and like_the_room:
                events.append(("background", lift))
                if self.metering:
                    self._mark(now, seconds, "background", lift, vs_user)
                continue
            text = self.recognize(audio)
            if self.metering:
                why = None if standing != "no" else "its name" if named else "your level" if yours \
                    else "in a conversation" if talking else None
                self._mark(now, seconds, "empty" if not text else "would" if like_the_room else "heard", lift, vs_user,
                           text or None, why)
            if text:
                info = {"duration_s": round(seconds, 2), "level_db": round(db, 1),
                        "level_vs_user_db": None if vs_user is None else round(vs_user, 1),
                        "lift_db": None if lift is None else round(lift, 1), "stands_out": standing, "named": named}
                # your level is learned from what was said; with the background filter on, only from what stood out
                # (a TV heard while the room was still being learned must not become "your level")
                if not self.gate or standing in ("strongly", "somewhat"):
                    self.level.add(db)
                events.append(("text", text, info))
        return events


class Hearing:
    """``on_voice()`` is called while someone is speaking, ``on_text(text, info)`` with each utterance's transcript
    (``info``: what ``Ears`` measured), ``on_error(message)`` if the microphone or models can't be opened,
    ``on_name(word)`` when the face's name is heard (even while it speaks); all from the listening thread.
    ``is_speaking()`` says whether the face is talking right now. ``configure`` sets the name and the filters (applied
    from the next block). With ``meter`` on ("show voice detector"), ``on_meter(reading)`` gets what the blocks
    measured every ``METER_EVERY_S`` (see ``Ears``)."""

    def __init__(self, on_voice, on_text, on_error, is_speaking, on_name=None, on_meter=None):
        self.on_voice, self.on_text, self.on_error, self.is_speaking = on_voice, on_text, on_error, is_speaking
        self.on_name = on_name
        self.on_meter = on_meter
        self.names: list[str] = []
        self.denoise = True
        self.focus = True
        self.gate = False                                # "ignore constant background sound"
        self.meter = False                               # "show voice detector"
        self._changed = threading.Event()
        self._listen = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def configure(self, names: list[str] | None = None, denoise: bool | None = None, focus: bool | None = None,
                  gate: bool | None = None, meter: bool | None = None) -> None:
        if meter is not None:
            self.meter = meter                           # read for every block: the ears need not be rebuilt for it
        if (names, denoise, focus, gate) == (None, None, None, None):
            return
        if names is not None:
            self.names = list(names)
        if denoise is not None:
            self.denoise = denoise
        if focus is not None:
            self.focus = focus
        if gate is not None:
            self.gate = gate
        self._changed.set()

    def listen_now(self) -> None:
        """Open the microphone at once (the face was told to stop talking)."""
        self._listen.set()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hearing", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _ears(self, recognizer, old: Ears | None = None) -> Ears:
        spotter = None
        if self.names and self.on_name:
            try:
                spotter = make_spotter(name_keywords(self.names))
            except Exception:
                log.exception("no name spotter")
        denoiser = None
        if self.denoise:
            try:
                denoiser = make_denoiser()
            except Exception:
                log.exception("no denoiser")
        ears = Ears(speech.make_vad(), lambda audio: speech.transcribe(recognizer, audio), denoiser, spotter, self.focus,
                    self.gate)
        if old is not None:
            ears.level = old.level                                   # keep what it learned about your voice
            ears.room = old.room                                     # and about the room
        log.info("listening: name spotter %s, denoiser %s, voice level filter %s, background filter %s",
                 f"on ({', '.join(self.names)})" if spotter else "off", "on" if denoiser else "off",
                 "on" if self.focus else "off", "on" if self.gate else "off")
        return ears

    def _run(self):
        try:
            import sounddevice as sd

            gone = models.missing_message("listening")
            if gone:
                raise models.MissingModels(gone)
            recognizer = speech.make_offline_recognizer()
            self._changed.clear()
            ears = self._ears(recognizer)
            mic = sd.InputStream(samplerate=speech.SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                                 latency="low")
            mic.start()
        except models.MissingModels as e:
            log.warning("could not start listening: %s", e)
            self.on_error(str(e))
            return
        except Exception as e:
            log.exception("could not start listening")
            self.on_error(f"I can't listen: {e}")
            return
        log.info("listening")
        sent = 0.0
        try:
            while not self._stop.is_set():
                samples, _overflow = mic.read(BLOCK)
                now = time.monotonic()
                if self._changed.is_set():
                    self._changed.clear()
                    ears = self._ears(recognizer, ears)
                if self._listen.is_set():
                    self._listen.clear()
                    ears.listen_now(now)
                ears.metering = self.meter and self.on_meter is not None
                if not ears.metering:
                    if ears.meter or ears.marks:
                        ears.meter, ears.marks = [], []
                elif now - sent >= METER_EVERY_S and (ears.meter or ears.marks):
                    self.on_meter({"blocks": ears.meter, "marks": ears.marks, "room_ready": ears.room.ready,
                                   "gate": ears.gate, "focus": ears.focus})
                    ears.meter, ears.marks, sent = [], [], now
                for ev in ears.block(samples[:, 0].copy(), now, self.is_speaking()):
                    if ev[0] == "voice":
                        self.on_voice()
                    elif ev[0] == "text":
                        self.on_text(ev[1], ev[2])
                    elif ev[0] == "name" and self.on_name:
                        self.on_name(ev[1])
                    elif ev[0] == "dropped":
                        log.info("ignored a quieter voice (%.0f dB; yours is about %.0f dB)", ev[1], ears.level.reference or 0)
                    elif ev[0] == "background":
                        log.info("ignored speech that sounded like the room (%.1f dB above its usual level)", ev[1])
        except Exception as e:
            log.exception("listening stopped")
            self.on_error(f"I stopped listening: {e}")
        finally:
            mic.stop()
            mic.close()
            log.info("not listening")
