"""sherpa-onnx speech models (all CPU): TTS with lip sync, and VAD + offline STT for the microphone.

Note: sherpa-onnx TTS callbacks return 1 to *continue* and 0 to stop (the
docstring says the opposite; the runtime and upstream examples agree on this).
"""
from __future__ import annotations

import collections
import logging
import queue
import re
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx as so

from . import config
from .lipsync import MouthTracker, resync, wall_clock
from .phonemes import split_sentences
from .tts_elevenlabs import ElevenLabsError

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        data = data.reshape(-1, ch)[:, 0]
    return data, sr


# --- input side --------------------------------------------------------------
def make_vad() -> so.VoiceActivityDetector:
    cfg = so.VadModelConfig(
        silero_vad=so.SileroVadModelConfig(model=str(config.VAD_MODEL), threshold=0.5,
                                           min_silence_duration=0.6, min_speech_duration=0.25),
        sample_rate=SAMPLE_RATE,
    )
    return so.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)


def make_offline_recognizer() -> so.OfflineRecognizer:
    """Parakeet 0.6B (non-streaming): transcribes each finished utterance."""
    d = config.OFFLINE_ASR_DIR
    return so.OfflineRecognizer.from_transducer(
        encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.int8.onnx"),
        joiner=str(d / "joiner.int8.onnx"), tokens=str(d / "tokens.txt"),
        num_threads=config.ASR_THREADS, model_type="nemo_transducer")


def transcribe(rec: so.OfflineRecognizer, audio: np.ndarray) -> str:
    stream = rec.create_stream()
    stream.accept_waveform(SAMPLE_RATE, audio)
    rec.decode_stream(stream)
    return stream.result.text.strip()


# --- output side -------------------------------------------------------------
class AudioPlayer:
    """Low-latency output stream fed from a queue; ``clear()`` silences within one block (~20 ms).

    Every queued sample gets an index (``write`` returns it), and ``clock`` says which index reached
    the speaker at what wall-clock time, so the face's mouth can follow the sound exactly.
    """

    def __init__(self, samplerate: int, device=None, stream: bool = True, latency="low"):
        self.sr = samplerate
        self._buf: collections.deque[tuple[np.ndarray, int]] = collections.deque()
        self._cur: np.ndarray | None = None
        self._cur_start = 0
        self._pos = 0
        self._lock = threading.Lock()
        self.written = 0                  # samples ever queued: the lip-sync timeline
        # last audible block: (index of its first sample, wall time it reaches the speaker, index where the
        # queued audio ran out in it or None)
        self.clock: tuple[int, float, int | None] | None = None
        self.latency = 0.0
        self.underflows = 0               # the sound card asked for audio we couldn't hand over in time
        self.stream = None
        if stream:
            import sounddevice as sd

            # "low": ~0.1 s of buffering on MME instead of ~0.2 s, so a voice that is told to stop stops sooner
            self.stream = sd.OutputStream(samplerate=samplerate, channels=1, dtype="float32", device=device,
                                          blocksize=int(samplerate * 0.02), callback=self._cb, latency=latency)
            self.latency = float(self.stream.latency)
            self.stream.start()

    def _cb(self, outdata, frames, time_info, status):
        out = outdata[:, 0]
        filled = 0
        first = None
        if status is not None and status.output_underflow:
            self.underflows += 1
        with self._lock:
            while filled < frames:
                if self._cur is None or self._pos >= len(self._cur):
                    if not self._buf:
                        break
                    (self._cur, self._cur_start), self._pos = self._buf.popleft(), 0
                if first is None:
                    first = self._cur_start + self._pos
                n = min(frames - filled, len(self._cur) - self._pos)
                out[filled:filled + n] = self._cur[self._pos:self._pos + n]
                self._pos += n
                filled += n
        out[filled:] = 0
        if first is None:
            # Ran dry exactly on a block boundary: the last block was full, so it didn't know it was the last.
            # Mark where the audio ended, or the mouth (and ``drain_s``) would wait for more forever.
            clock = self.clock
            if clock is not None and clock[2] is None:
                self.clock = (clock[0], clock[1], clock[0] + frames)
        else:
            # when this block reaches the DAC (MME reports it, ~0.2 s ahead); else the stream's latency
            try:
                ahead = time_info.outputBufferDacTime - time_info.currentTime
            except AttributeError:
                ahead = 0.0
            if not 0.0 < ahead < 2.0:
                ahead = self.latency
            self.clock = (first, wall_clock() + ahead, first + filled if filled < frames else None)

    def write(self, samples) -> int:
        """Queue audio; returns the index of its first sample."""
        a = np.array(samples, dtype=np.float32, copy=True).ravel()
        with self._lock:
            start = self.written
            self._buf.append((a, start))
            self.written += len(a)
        return start

    def clear(self):
        with self._lock:
            self._buf.clear()
            self._cur = None

    def pending_s(self) -> float:
        with self._lock:
            n = sum(len(b) for b, _ in self._buf)
            if self._cur is not None:
                n += len(self._cur) - self._pos
        return n / self.sr

    def drain_s(self) -> float:
        """Seconds until the last sample handed to the sound card is heard (it buffers ~0.2 s), at most 1."""
        clock = self.clock
        if clock is None:
            return 0.0
        first, at, end = clock
        left = self.latency if end is None else at + (end - first) / self.sr - wall_clock()
        return max(0.0, min(1.0, left))

    def close(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()


def make_tts(engine: str) -> so.OfflineTts:
    """The local sherpa-onnx synthesizer: "pocket" (voice cloning) or "kokoro"."""
    if engine == "pocket":
        p = config.POCKET_DIR
        model = so.OfflineTtsModelConfig(pocket=so.OfflineTtsPocketModelConfig(
            lm_flow=str(p / "lm_flow.int8.onnx"), lm_main=str(p / "lm_main.int8.onnx"),
            encoder=str(p / "encoder.onnx"), decoder=str(p / "decoder.int8.onnx"),
            text_conditioner=str(p / "text_conditioner.onnx"), vocab_json=str(p / "vocab.json"),
            token_scores_json=str(p / "token_scores.json")), num_threads=4)
    else:
        k = config.KOKORO_DIR
        model = so.OfflineTtsModelConfig(kokoro=so.OfflineTtsKokoroModelConfig(
            model=str(k / "model.int8.onnx"), voices=str(k / "voices.bin"),
            tokens=str(k / "tokens.txt"), data_dir=str(k / "espeak-ng-data")), num_threads=4)
    return so.OfflineTts(so.OfflineTtsConfig(model=model))


SENTENCE_GAP_S = 0.12     # pause between sentences (each is generated on its own)
# Pocket often swallows the first word of a generation ("I'm sorry" -> "sorry"). A dash in front gives it a
# short lead-in and the word a proper onset; the extra pause is trimmed off again.
POCKET_LEAD_IN = "- "
LEAD_TRIM_KEEP_S = 0.04    # silence kept before the first sound of a sentence


def trim_lead_in(samples: np.ndarray, sr: int, keep_s: float = LEAD_TRIM_KEEP_S, max_s: float = 0.5,
                 threshold: float = 0.006) -> np.ndarray:
    """Drop leading near-silence (at most ``max_s``), keeping ``keep_s`` before the first sound."""
    hop = max(1, sr // 200)                                  # 5 ms windows
    n = min(len(samples), int(sr * max_s))
    for k in range(0, n, hop):
        if np.sqrt(np.mean(samples[k:k + hop] ** 2)) > threshold:
            return samples[max(0, k - int(sr * keep_s)):]
    return samples[n:] if n < len(samples) else samples


def pocket_voices() -> list[str]:
    """Reference voices that ship with Pocket (``test_wavs/<voice>.wav``)."""
    return sorted(p.stem for p in (config.POCKET_DIR / "test_wavs").glob("*.wav") if not p.stem.startswith("sample_"))


class Voice:
    """Streaming, interruptible TTS fed sentence by sentence while the reply is still being written.

    ``say`` queues text and returns at once; a worker thread synthesizes each sentence into the player as
    soon as the one before it is generated (not played), so there is no gap while a long reply is spoken.

    ``on_mouth(frames)`` receives lip-sync frames and ``on_sentence(text)`` each sentence as its synthesis
    starts, both from the worker thread.

    Two voices can be behind it: the local one (sherpa-onnx; ``local=False`` leaves it out, e.g. while its models
    are not installed) and a ``cloud`` engine (ElevenLabs). ``prefer_cloud()`` is asked for every sentence; when the
    cloud engine fails, ``on_cloud_error(error)`` hears about it and the sentence is spoken locally if it can be.
    """

    def __init__(self, voice: str, engine: str | None = None, device=None, on_mouth=None, on_sentence=None,
                 tts=None, stream: bool = True, local: bool = True, cloud=None, prefer_cloud=None,
                 on_cloud_error=None):
        self.engine = engine or config.TTS_ENGINE
        self.tts = tts if tts is not None else (self._make_tts() if local else None)
        self.cloud = cloud
        if self.tts is None and cloud is None:
            raise ValueError("a voice needs the local models or a cloud engine")
        self.sr = self.tts.sample_rate if self.tts else cloud.sample_rate
        if cloud is not None:
            cloud.sample_rate = self.sr                # one player for both: the cloud engine speaks at its rate
        self.prefer_cloud = prefer_cloud or (lambda: False)
        self.on_cloud_error = on_cloud_error
        self.player = AudioPlayer(self.sr, device=device, stream=stream)
        self.on_mouth = on_mouth
        self.on_sentence = on_sentence
        self.mouth = MouthTracker(self.sr) if on_mouth else None
        self._stop = threading.Event()
        self._queue: queue.Queue[tuple[str, int] | None] = queue.Queue()
        self._epoch = 0                    # bumped by stop(): text queued before it is dropped
        self._working = False
        self._first = True                 # next sentence opens a reply (no gap before it)
        self._gen = None
        self.spoken: collections.deque[tuple[int, str]] = collections.deque(maxlen=64)   # (player index, sentence)
        self.voice = ""
        self.set_voice(voice)
        self._thread = threading.Thread(target=self._run, name="voice", daemon=True)
        self._thread.start()

    def _make_tts(self) -> so.OfflineTts:
        return make_tts(self.engine)

    def attach_local(self) -> bool:
        """The local models were installed after this voice started: add them. False if they don't fit the player."""
        if self.tts is not None:
            return True
        tts = self._make_tts()
        if tts.sample_rate != self.sr:
            log.warning("the local voice speaks at %d Hz, the player runs at %d Hz: not using it", tts.sample_rate, self.sr)
            return False
        self.tts = tts
        self.set_voice(self.voice)
        return True

    def set_voice(self, voice: str):
        """Pocket: the reference voice to speak in (takes effect from the next sentence)."""
        if self.engine == "pocket" and self.tts is not None:
            ref, sr = read_wav(config.POCKET_DIR / "test_wavs" / f"{voice}.wav")
            gen = so.GenerationConfig()
            gen.reference_audio = ref.tolist()
            gen.reference_sample_rate = sr
            gen.speed = config.TTS_SPEED
            self._gen = gen
        self.voice = voice

    # --- queue ---------------------------------------------------------------------------------------
    def say(self, text: str):
        text = clean_for_speech(text)
        for sentence in split_sentences(text) or ([text] if text else []):
            if speakable(sentence):
                self._queue.put((sentence, self._epoch))

    @property
    def busy(self) -> bool:
        """Text is waiting, being synthesized, or still coming out of the speaker."""
        return self._working or not self._queue.empty() or self.player.pending_s() > 0 or self.player.drain_s() > 0

    def stop(self):
        """Cut the speech off now and forget anything queued."""
        self._epoch += 1
        self._stop.set()
        self.player.clear()
        if self.mouth:
            self.mouth.cancel()

    def close(self):
        self.stop()
        self._queue.put(None)
        self.player.close()

    # --- worker --------------------------------------------------------------------------------------
    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            sentence, epoch = item
            if epoch != self._epoch:
                continue
            self._working = True
            try:
                self._stop.clear()
                if self._epoch != epoch:               # stop() landed between the check and the clear
                    continue
                self._speak(sentence)
            except Exception:
                log.exception("speech failed")
            finally:
                self._working = False

    def _speak(self, sentence: str):
        sr = self.sr
        if self.player.pending_s() <= 0:               # the reply's first sentence (or we fell silent)
            self._first = True
            if self.mouth:
                resync()
        if not self._first:
            self.player.write(np.zeros(int(sr * SENTENCE_GAP_S), np.float32))
        self._first = False
        fresh = [True]                                 # next chunk opens the sentence: trim its lead-in
        wrote = [False]                                # audio of this sentence has been queued
        if self.on_sentence:
            self.on_sentence(sentence)

        def cb(samples, _progress):
            if self._stop.is_set():
                return 0
            if fresh[0]:
                fresh[0] = False
                samples = trim_lead_in(np.asarray(samples, dtype=np.float32), sr)
                if not len(samples):
                    return 1
            index = self.player.write(samples)
            if not wrote[0]:
                self.spoken.append((index, sentence))
            wrote[0] = True
            if self.mouth:
                self._mouth(lambda: self.mouth.feed(samples, index, sentence))
            return 1

        done = False
        if self.cloud is not None and self.prefer_cloud():
            fresh[0] = False                           # a cloud voice starts cleanly: nothing to trim
            try:
                self.cloud.synth(sentence, cb, self._stop)
                done = True
            except ElevenLabsError as e:
                log.warning("cloud voice failed: %s", e)
                if self.on_cloud_error:
                    self.on_cloud_error(e)
                done = wrote[0] or self.tts is None    # half a sentence is spoken already: don't say it twice
            fresh[0] = True
        if not done and self.tts is not None:
            if self.engine == "pocket":
                self.tts.generate(POCKET_LEAD_IN + sentence, self._gen, cb)
            else:
                self.tts.generate(sentence, sid=config.KOKORO_SID, speed=config.TTS_SPEED, callback=cb)
        if self.mouth and not self._stop.is_set():
            self._mouth(self.mouth.flush)

    def _mouth(self, produce):
        """Run the lip-sync tracker and hand on its frames; a failure there never stops the speech."""
        try:
            for f in produce():
                if self._stop.is_set():             # cut off: this audio will never play
                    return
                self.on_mouth(f)
        except Exception:
            log.exception("lip sync failed (speech continues)")
            self.mouth.cancel()


def speakable(text: str) -> bool:
    """Something to pronounce (not just punctuation left over from stripped markup)."""
    return bool(re.search(r"[A-Za-z0-9]", text))


def clean_for_speech(text: str) -> str:
    """Strip markdown and other things that sound bad when read aloud."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)]*\)", r"\1", text)      # [label](target) -> label
    text = re.sub(r"https?://\S+", "a link", text)
    # maths markup: $1234 \times 5678$ -> 1234 times 5678 (a plain "$20" stays)
    if re.search(r"\\[a-zA-Z]+", text):
        for cmd, word in ((r"\times", " times "), (r"\cdot", " times "), (r"\div", " divided by "),
                          (r"\approx", " about "), (r"\pm", " plus or minus ")):
            text = text.replace(cmd, word)
        text = re.sub(r"\\[a-zA-Z]+", " ", text).replace("$", "")
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)                     # list bullets
    text = re.sub(r"(?<=\w)_+(?=\w)", " ", text)                             # snake_case reads as words
    text = re.sub(r"[*_#`>|]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


class SpeechStream:
    """Turns a reply as it streams in (text deltas) into pieces ready to speak.

    ``feed`` returns the text that is complete so far, up to the last sentence end or line break; fenced
    code blocks are dropped (nobody wants code read aloud). ``flush`` returns what's left.
    """

    _END = re.compile(r"[.!?:;][\"')\]*_`]*\s+|\n+")

    def __init__(self):
        self.buf = ""
        self.in_code = False

    def feed(self, delta: str) -> str:
        self.buf += delta
        out = []
        while True:
            if self.in_code:
                end = self.buf.find("```")
                if end < 0:
                    self.buf = self.buf[-2:]            # a closing fence may be split across deltas
                    break
                self.buf = self.buf[end + 3:]
                self.in_code = False
                continue
            start = self.buf.find("```")
            if start >= 0:
                out.append(self.buf[:start])
                self.buf = self.buf[start + 3:]
                self.in_code = True
                continue
            # hold back a tail that could be the start of a fence
            hold = 2 if self.buf.endswith("``") else 1 if self.buf.endswith("`") else 0
            text = self.buf[:len(self.buf) - hold] if hold else self.buf
            last = None
            for last in self._END.finditer(text):
                pass
            if last:
                out.append(text[:last.end()])
                self.buf = self.buf[last.end():]
            break
        return " ".join(p.strip() for p in out if p.strip())

    def flush(self) -> str:
        rest, self.buf = ("" if self.in_code else self.buf), ""
        self.in_code = False
        return rest.strip()
