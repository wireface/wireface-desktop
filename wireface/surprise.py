"""Does what was just said stand out from what the room usually sounds like? ("Ignore constant background sound")

A TV, a radio, music or people chatting across the room are there most of the time; you talking to the face is not.
``RoomSound`` keeps the last minute of what the microphone heard as the energy in 20 frequency bands per 50 ms block,
and an utterance is compared with the room's usual loud level: per band, the level the room is at or below 80% of the
time. Speech that rises well above it stands out ("strongly", "somewhat"); speech at the room's usual level is more of
the same ("no"), which is how a TV that has been on for a while sounds.

Everything heard is learned, the user's voice included, so a sound that keeps going becomes the background within a
minute. You talking now and then doesn't: it is a small share of the minute. A long dictation would be, so speech at
your own level is never dropped for this (``hearing.OWN_LEVEL_DB``), and the hearing keeps listening for a while
after the face spoke or something stood out (``hearing.CONVERSATION_WINDOW_S``).

Measured with real speech (the speech models' test clips; a reverberant "TV" playing other clips continuously): a
quiet room, you +34..41 dB ("strongly"); after a minute of TV, more TV -15..-3 dB ("no", 13 of 13); you 6 dB louder
than the TV, over it, +7..13 dB; 12 dB louder, +12..19 dB; a loud steady fan without noise reduction, you +10..16 dB.
After a minute of dictation (60% talking) you were only -2..+6 dB: hence the own-level rule above.

This works on the audio the speech detector sees (after noise reduction, when that is on): steady noise like a fan is
already gone by then; this is for sound that comes and goes but never stops for long. It can't tell you from another
person at the same distance: the judge (``judge.py``) does that from the words.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000
BLOCK = 800                    # 50 ms, as hearing.py reads the microphone
FFT = 1024
BANDS = 20
LOW_HZ, HIGH_HZ = 100.0, 7600.0
SPEECH_HZ = (250.0, 4000.0)    # where speech has its energy: the bands that are compared
MEMORY_S = 60.0                # how much of the room is remembered
WARMUP_S = 5.0                 # until then nothing is judged ("unknown")
ROOM_PERCENTILE = 80           # the room's usual loud level: what it is at or below this share of the time
STRONG_DB = 12.0               # this far above the room's usual loud level: it stands out strongly
SOME_DB = 6.0                  # this far: somewhat; less: it sounds like the room
FLOOR_DB = -100.0              # digital silence
USUAL_EVERY = 10               # blocks: how often the room's usual level is worked out again for the live meter


def _mel(hz):
    return 2595.0 * np.log10(1.0 + np.asarray(hz) / 700.0)


def _hz(mel):
    return 700.0 * (10 ** (np.asarray(mel) / 2595.0) - 1.0)


def _filterbank() -> np.ndarray:
    """(FFT // 2 + 1, BANDS) triangular mel filters."""
    edges = _hz(np.linspace(_mel(LOW_HZ), _mel(HIGH_HZ), BANDS + 2))
    freqs = np.fft.rfftfreq(FFT, 1.0 / SAMPLE_RATE)
    fb = np.zeros((len(freqs), BANDS), np.float32)
    for b in range(BANDS):
        lo, mid, hi = edges[b], edges[b + 1], edges[b + 2]
        rise = (freqs - lo) / (mid - lo)
        fall = (hi - freqs) / (hi - mid)
        fb[:, b] = np.clip(np.minimum(rise, fall), 0.0, None)
    return fb


_FB = _filterbank()
_WINDOW = np.hanning(BLOCK).astype(np.float32)
_CENTRES = _hz(np.linspace(_mel(LOW_HZ), _mel(HIGH_HZ), BANDS + 2))[1:-1]
SPEECH_BANDS = np.flatnonzero((_CENTRES >= SPEECH_HZ[0]) & (_CENTRES <= SPEECH_HZ[1]))


def band_db(blocks: np.ndarray) -> np.ndarray:
    """Band energies (dB) of one block (BLOCK,) -> (BANDS,), or of several (n, BLOCK) -> (n, BANDS)."""
    x = np.asarray(blocks, np.float32)
    one = x.ndim == 1
    x = x.reshape(-1, x.shape[-1])
    if x.shape[1] != BLOCK:                                  # a short last block: padded with silence
        x = np.pad(x[:, :BLOCK], ((0, 0), (0, max(0, BLOCK - x.shape[1]))))
    spec = np.abs(np.fft.rfft(x * _WINDOW, FFT)) ** 2
    db = 10.0 * np.log10(spec @ _FB + 1e-10)
    db = np.maximum(db, FLOOR_DB).astype(np.float32)
    return db[0] if one else db


def stands_out(lift_db: float | None) -> str:
    if lift_db is None:
        return "unknown"
    return "strongly" if lift_db >= STRONG_DB else "somewhat" if lift_db >= SOME_DB else "no"


class RoomSound:
    """The last minute of what the room sounded like, and how far an utterance rises above it."""

    def __init__(self, memory_s: float = MEMORY_S):
        self.size = max(1, int(memory_s * SAMPLE_RATE / BLOCK))
        self.ring = np.zeros((self.size, BANDS), np.float32)
        self.count = 0                                       # blocks seen (the ring holds the last ``size``)
        self.last: np.ndarray | None = None                  # the latest block's band energies
        self._usual: np.ndarray | None = None                # the room's usual level as of block ``_usual_at``
        self._usual_at = 0

    @property
    def ready(self) -> bool:
        """It has heard enough of the room to judge (``WARMUP_S``)."""
        return self.count >= WARMUP_S * SAMPLE_RATE / BLOCK

    def observe(self, block: np.ndarray) -> None:
        """One block the microphone heard (not while the face itself was speaking)."""
        self.last = band_db(block)
        self.ring[self.count % self.size] = self.last
        self.count += 1

    def usual(self) -> np.ndarray | None:
        """The room's usual loud level per band, or None until it has heard enough."""
        n = min(self.count, self.size)
        if not self.ready:
            return None
        return np.percentile(self.ring[:n], ROOM_PERCENTILE, axis=0)

    def now_lift(self) -> float | None:
        """How far the latest block rises above the room's usual loud level, in the speech bands (the voice
        detector's meter); None while it is still learning the room. The room's level is refreshed every
        ``USUAL_EVERY`` blocks, not for every block."""
        if self.last is None:
            return None
        if self._usual is None or self.count - self._usual_at >= USUAL_EVERY:
            self._usual, self._usual_at = self.usual(), self.count
        if self._usual is None:
            return None
        return float((self.last[SPEECH_BANDS] - self._usual[SPEECH_BANDS]).mean())

    def lift(self, audio: np.ndarray) -> float | None:
        """How far (dB) the speech in ``audio`` rises above the room's usual loud level, in the speech bands; None
        while it is still learning the room. Only the louder half of the utterance counts (not its pauses)."""
        room = self.usual()
        n = len(audio) // BLOCK
        if room is None or n == 0:
            return None
        feats = band_db(np.asarray(audio[: n * BLOCK], np.float32).reshape(n, BLOCK))[:, SPEECH_BANDS]
        energy = feats.mean(axis=1)
        speech = feats[energy >= np.median(energy)]
        return float((speech.mean(axis=0) - room[SPEECH_BANDS]).mean())
