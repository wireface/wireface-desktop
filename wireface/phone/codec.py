"""Phone audio: G.711 mu-law at 8 kHz (what Twilio's media streams carry) to and from 16-bit PCM at the rates a
speech model uses. Pure numpy (``audioop``, which did this, is gone from Python 3.13). Resamplers keep a little state
between chunks, so a stream of 20 ms frames comes out seamless."""
from __future__ import annotations

import numpy as np

BIAS = 0x84
CLIP = 32635


def _decode_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    exponent = (u >> 4) & 7
    magnitude = (((u & 0x0F) << 3) + BIAS) << exponent
    return np.where(u & 0x80, BIAS - magnitude, magnitude - BIAS).astype(np.int16)


DECODE = _decode_table()
SEG_END = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF], dtype=np.int32)


def ulaw_to_pcm(data: bytes) -> np.ndarray:
    """mu-law bytes -> int16 samples (same rate)."""
    return DECODE[np.frombuffer(data, dtype=np.uint8)]


def pcm_to_ulaw(samples: np.ndarray) -> bytes:
    """int16 samples -> mu-law bytes (same rate): the reference G.711 encoder (14-bit, as in Sun's g711.c)."""
    x = samples.astype(np.int32) >> 2
    mask = np.where(x < 0, 0x7F, 0xFF)
    mag = np.minimum(np.abs(x), CLIP >> 2) + (BIAS >> 2)
    seg = np.searchsorted(SEG_END, mag)                       # the first segment whose end is >= mag
    uval = (seg << 4) | ((mag >> (seg + 1)) & 0x0F)
    return ((uval ^ mask) & 0xFF).astype(np.uint8).tobytes()


class Up2:
    """8 kHz -> 16 kHz: each new sample between two old ones is their average (the last one is carried over)."""

    def __init__(self):
        self.last = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if not len(x):
            return x
        prev = np.concatenate([[self.last], x[:-1]]).astype(np.int32)
        mids = ((prev + x.astype(np.int32)) // 2).astype(np.int16)
        self.last = int(x[-1])
        out = np.empty(len(x) * 2, dtype=np.int16)
        out[0::2], out[1::2] = mids, x
        return out


class Down3:
    """24 kHz -> 8 kHz: the mean of each three samples (a crude low-pass, plenty for a phone line); leftovers wait
    for the next chunk."""

    def __init__(self):
        self.rest = np.zeros(0, dtype=np.int16)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.concatenate([self.rest, x])
        n = len(x) // 3 * 3
        self.rest = x[n:]
        return x[:n].astype(np.int32).reshape(-1, 3).mean(axis=1).astype(np.int16)
