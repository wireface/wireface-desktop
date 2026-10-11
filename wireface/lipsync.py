"""Lip sync: the assistant's synthesized speech -> mouth shapes, timed to the speaker.

The TTS hands audio over well before it is heard (Pocket generates a whole sentence, then
delivers it in 1.2 s pieces; the MME output adds ~0.2 s), so the mouth track is computed ahead
of playback. Two layers:

* **Text-aligned visemes** (normal case). We know the sentence being spoken: its phonemes
  (CMU dictionary, ``phonemes.py``) are force-aligned to the audio with a small Viterbi
  aligner over acoustic classes (silence, closure, nasal murmur, sibilant, vowel by formant
  target, ...). Alignment is open-ended, so it runs on each piece as it arrives. This is what
  closes the lips on every m/b/p, tucks the lip for f/v and rounds for "oo"/"w", including
  the cases audio alone can't tell apart (in this voice an "m" looks just like "oo").
* **Acoustic shapes** (fallback for unknown text or a poor alignment): loudness x vowel
  openness (first formant), spread/rounding (second formant), frication, and a lip closure
  before low-frequency bursts.

Both are smoothed zero-phase (no lag) and shown ``LEAD_S`` early, since lips move before the
sound they make. Each 10 ms frame carries the channels in ``CHANNELS`` (0..1).

Frames are indexed by the audio player's sample counter. The player reports which sample
reached the speaker at what wall-clock time (``AudioPlayer.clock``), so the face shows each
frame exactly when it is audible, whatever the device latency.
"""
from __future__ import annotations

import time
import warnings
from dataclasses import dataclass

import numpy as np

from . import phonemes

CHANNELS = ("jaw", "wide", "round", "close", "fric", "bite", "tongue", "energy")
J, W, R, C, F, B, T, E = range(len(CHANNELS))
HOP_S = 0.010            # one frame per 10 ms
WIN_S = 0.032            # analysis window
LEAD_S = 0.030           # show shapes slightly early: lips move before the sound they make
LPC_ORDER = 12           # at 12 kHz (the spectrum is band-limited and decimated by two)
REF_DB0 = -22.0          # starting guess for the loudest speech level (dBFS); adapts upwards at once
MIN_CONFIDENCE = -1.6    # mean log-score gap between the forced path and free classification
MAX_ALIGN_FRAMES = 3000  # longer "sentences" (30 s) use the acoustic shapes


# --- precise wall clock --------------------------------------------------------------------------
def _calibrate() -> float:
    """time.time() ticks every ~15.6 ms on Windows (Python < 3.13); anchor perf_counter to a tick edge."""
    t0 = time.time()
    deadline = time.perf_counter() + 0.05
    while time.perf_counter() < deadline:
        t1 = time.time()
        if t1 != t0:
            return t1 - time.perf_counter()
    return time.time() - time.perf_counter()


_OFFSET = _calibrate()


def resync():
    """Re-anchor to the system clock (briefly busy-waits; call it off the audio thread, before speaking).

    perf_counter and the system clock drift apart over hours, and Windows steps the system clock when it
    syncs (e.g. after sleep); the face compares against Date.now(), so a stale anchor shifts or loses the lips.
    """
    global _OFFSET
    _OFFSET = _calibrate()


def wall_clock() -> float:
    """Unix time with sub-millisecond resolution (what the face compares against Date.now())."""
    return time.perf_counter() + _OFFSET


# --- frames ----------------------------------------------------------------------------------------
@dataclass
class MouthFrames:
    """Consecutive frames: frame k is shown when sample ``start + k * hop`` reaches the speaker."""
    start: int
    hop: int
    sr: int
    values: np.ndarray          # (n, len(CHANNELS)), 0..1
    source: str = "acoustic"    # "aligned" | "acoustic"
    text: str | None = None     # the sentence being spoken (for captions in step with the voice)

    def __len__(self) -> int:
        return len(self.values)

    def to_json(self) -> dict:
        return {"s0": int(self.start), "hop": self.hop, "sr": self.sr, "ch": list(CHANNELS), "src": self.source,
                "text": self.text, "frames": np.round(self.values, 3).tolist()}


def _smooth(x: np.ndarray, sigma: float) -> np.ndarray:
    """Zero-phase gaussian smoothing (edges padded with the edge value)."""
    if sigma <= 0 or len(x) < 2:
        return x
    r = int(np.ceil(sigma * 3))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    return np.convolve(np.pad(x, r, mode="edge"), k, mode="valid")


def _shift(x: np.ndarray, frames: int) -> np.ndarray:
    """Move a track earlier by ``frames`` (anticipation), holding the last value."""
    if frames <= 0 or len(x) == 0:
        return x
    return np.concatenate([x[frames:], np.full(min(frames, len(x)), x[-1])])


def _ramp(x, lo, hi):
    """0 at ``lo``, 1 at ``hi`` (either direction), clipped."""
    return np.clip((np.asarray(x, dtype=np.float64) - lo) / (hi - lo), 0.0, 1.0)


# --- raw acoustic features ---------------------------------------------------------------------------
class _Analyzer:
    """Per-frame acoustic features for a batch of windowed frames (all vectorized)."""

    def __init__(self, sr: int, win: int):
        self.sr, self.win = sr, win
        self.window = np.hanning(win).astype(np.float64)
        self.nfft = 1 << int(np.ceil(np.log2(2 * win)))           # zero-padded: linear autocorrelation
        self.freqs = np.fft.rfftfreq(self.nfft, 1 / sr)
        f = self.freqs
        self.bands = {
            "low": (f >= 80) & (f < 900), "mid": (f >= 900) & (f < 2500),
            "high": (f >= 2500) & (f < 5000), "top": (f >= 5000) & (f < min(11000, sr / 2)),
        }
        self.all = (f >= 80) & (f < min(11000, sr / 2))
        self.cent = (f >= 80) & (f < min(8000, sr / 2))
        # autocorrelation of the window itself, to undo its taper when judging periodicity
        wp = np.abs(np.fft.rfft(self.window, self.nfft)) ** 2
        wac = np.fft.irfft(wp, self.nfft)[:win]
        self.wac = wac / wac[0]
        self.lag_lo, self.lag_hi = int(sr / 400), min(int(sr / 70), win - 1)
        # LPC: every second autocorrelation lag of the band-limited, pre-emphasized spectrum = the
        # autocorrelation at half the rate, so 12 poles cover formants up to 6 kHz at 24 kHz input
        self.dec = 2 if sr >= 16000 else 1
        self.fs_lpc = sr / self.dec
        lp = f < self.fs_lpc / 2 * 0.92
        pre = np.abs(1 - 0.97 * np.exp(-2j * np.pi * f / self.fs_lpc)) ** 2
        self.lpc_shape = np.where(lp, pre, 0.0)

    def __call__(self, frames: np.ndarray) -> dict[str, np.ndarray]:
        x = frames.astype(np.float64) * self.window
        rms = np.sqrt(np.mean(x ** 2, axis=1) / np.mean(self.window ** 2))
        db = 20 * np.log10(rms + 1e-9)
        spec = np.abs(np.fft.rfft(x, self.nfft, axis=1)) ** 2
        total = spec[:, self.all].sum(axis=1) + 1e-12
        feats = {"db": db}
        for name, mask in self.bands.items():
            feats[name] = spec[:, mask].sum(axis=1) / total
        c = spec[:, self.cent]
        feats["centroid"] = (c * self.freqs[self.cent]).sum(axis=1) / (c.sum(axis=1) + 1e-12)
        # periodicity: normalized autocorrelation peak in the pitch range (window taper divided out)
        ac = np.fft.irfft(spec, self.nfft, axis=1)[:, :self.win]
        r0 = ac[:, :1] + 1e-12
        lags = slice(self.lag_lo, self.lag_hi)
        feats["voicing"] = np.clip((ac[:, lags] / r0 / self.wac[lags]).max(axis=1), 0, 1)
        feats["f1"], feats["f2"] = self.formants(spec)
        return feats

    def formants(self, spec: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        r = np.fft.irfft(spec * self.lpc_shape, self.nfft, axis=1)[:, : (LPC_ORDER + 1) * self.dec: self.dec]
        n = len(r)
        f1 = np.full(n, np.nan)
        f2 = np.full(n, np.nan)
        ok = r[:, 0] > 1e-10
        if not ok.any():
            return f1, f2
        a = _levinson(r[ok] / r[ok, :1] * np.r_[1.0 + 1e-6, np.ones(LPC_ORDER)])   # tiny white-noise floor
        # roots of the prediction polynomial = eigenvalues of its companion matrix (batched)
        p = LPC_ORDER
        comp = np.zeros((len(a), p, p))
        comp[:, 0, :] = -a[:, 1:]
        comp[:, np.arange(1, p), np.arange(p - 1)] = 1.0
        roots = np.linalg.eigvals(comp)
        freq = np.angle(roots) * self.fs_lpc / (2 * np.pi)
        bw = -np.log(np.abs(roots) + 1e-12) * self.fs_lpc / np.pi
        freq = np.where((roots.imag > 0) & (freq > 180) & (bw < 650), freq, np.inf)
        freq.sort(axis=1)
        first = freq[:, 0]
        ok1 = (first >= 200) & (first <= 1100)
        later = np.where(freq > first[:, None] + 250, freq, np.inf).min(axis=1)   # F2: >= 250 Hz above F1
        ok2 = ok1 & (later <= 3300)
        idx = np.flatnonzero(ok)
        f1[idx[ok1]] = first[ok1]
        f2[idx[ok2]] = later[ok2]
        return f1, f2


def _levinson(r: np.ndarray) -> np.ndarray:
    """Levinson-Durbin over a batch of autocorrelation rows -> prediction polynomials a (a[:, 0] = 1)."""
    n, m = r.shape
    p = m - 1
    a = np.zeros((n, p + 1))
    a[:, 0] = 1.0
    err = r[:, 0].copy()
    for i in range(1, p + 1):
        acc = r[:, i] + np.einsum("nj,nj->n", a[:, 1:i], r[:, i - 1:0:-1])
        k = -acc / np.maximum(err, 1e-12)
        a_prev = a[:, 1:i].copy()
        a[:, 1:i] = a_prev + k[:, None] * a_prev[:, ::-1]
        a[:, i] = k
        err *= 1 - k ** 2
    return a


def _fill_gaps(x: np.ndarray, valid: np.ndarray, max_gap: int = 8) -> np.ndarray:
    """Linear interpolation over short invalid runs; long ones stay NaN."""
    out = np.where(valid, x, np.nan)
    good = np.flatnonzero(~np.isnan(out))
    if len(good) < 2:
        return out
    filled = np.interp(np.arange(len(x)), good, out[good])
    idx = np.arange(len(x))
    prev = np.maximum.accumulate(np.where(~np.isnan(out), idx, -10 ** 6))
    nxt = np.minimum.accumulate(np.where(~np.isnan(out), idx, 10 ** 6)[::-1])[::-1]
    return np.where(np.isnan(out) & ((nxt - prev) <= max_gap + 1), filled, out)


def _median3(x: np.ndarray) -> np.ndarray:
    if len(x) < 3:
        return x
    p = np.pad(x, 1, mode="edge")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)      # all-NaN columns stay NaN
        return np.nanmedian(np.stack([p[:-2], p[1:-1], p[2:]]), axis=0)


@dataclass
class _Cues:
    """Soft per-frame detectors shared by the acoustic shapes and the aligner (all 0..1 except f1/f2)."""
    loud: np.ndarray
    voiced: np.ndarray
    noisy: np.ndarray
    centroid: np.ndarray
    upper: np.ndarray
    f1: np.ndarray
    f2: np.ndarray
    db: np.ndarray


def _cues(raw: dict[str, np.ndarray]) -> _Cues:
    loud = _ramp(raw["db"] - raw["ref"], -32.0, -4.0)        # 0 = whisper/silence, 1 = loud vowel
    voiced = _ramp(raw["voicing"], 0.35, 0.65) * _ramp(loud, 0.03, 0.15)
    noisy = _ramp(raw["high"] + raw["top"], 0.35, 0.7) * (1 - 0.8 * voiced) * _ramp(loud, 0.02, 0.1)
    vowelish = voiced * (1 - noisy) > 0.3
    f1 = _median3(_fill_gaps(raw["f1"], vowelish))
    f2 = _median3(_fill_gaps(raw["f2"], vowelish))
    return _Cues(loud, voiced, noisy, raw["centroid"], raw["mid"] + raw["high"] + raw["top"], f1, f2, raw["db"])


# --- acoustic shapes (fallback) ------------------------------------------------------------------------------
def shape(raw: dict[str, np.ndarray]) -> np.ndarray:
    """Mouth channels (n, len(CHANNELS)) from audio alone. Non-causal: uses the future."""
    n = len(raw["db"])
    out = np.zeros((n, len(CHANNELS)))
    if n == 0:
        return out
    q = _cues(raw)
    loud, voiced, noisy = q.loud, q.voiced, q.noisy
    vowel = voiced * (1 - noisy)
    f1n = np.nan_to_num(_ramp(q.f1, 300.0, 800.0), nan=0.4)  # openness of the vowel
    front = np.nan_to_num(_ramp(q.f2, 1500.0, 2300.0), nan=0.0)
    back = np.nan_to_num(_ramp(q.f2, 1350.0, 850.0), nan=0.0)

    jaw = loud ** 1.1 * (vowel * (0.22 + 0.78 * f1n) + (1 - vowel) * 0.14)
    nasal = voiced * _ramp(q.upper, 0.14, 0.05) * _ramp(loud, 0.08, 0.25) * _ramp(loud, 0.9, 0.6)
    close = 0.75 * nasal
    # stop closures: a quiet gap released by a burst; a low-frequency (diffuse, falling) burst is labial
    gap_lvl = loud < 0.12
    db = q.db
    jump = np.zeros(n)
    jump[2:] = db[2:] - np.minimum(db[1:-1], db[:-2])
    for k in np.flatnonzero((jump > 12) & (loud > 0.25)):
        g = k - 1
        while g >= 0 and k - g <= 14 and (gap_lvl[g] or nasal[g] > 0.3):
            g -= 1
        if k - 1 - g < 3:                                   # needs >= 30 ms of closure
            continue
        labial = float(_ramp(q.centroid[k:k + 2].mean(), 2600.0, 1400.0))
        if labial > 0.05:
            close[g + 1:k] = np.maximum(close[g + 1:k], labial)

    sib = noisy * _ramp(q.centroid, 3500.0, 5500.0)         # s, z: spread, teeth showing
    shh = noisy * _ramp(q.centroid, 4500.0, 2800.0)         # sh, ch: rounded
    wide = np.clip(vowel * loud ** 0.5 * front * (1 - 0.4 * f1n) + 0.55 * sib, 0, 1)
    rnd = np.clip(vowel * loud ** 0.5 * back * (1 - 0.5 * f1n) + 0.6 * shh, 0, 1)
    fric = np.clip(noisy * loud ** 0.4, 0, 1)

    jaw = _smooth(jaw, 1.2) * (1 - 0.6 * _smooth(fric, 1.0))
    close = np.maximum(_smooth(close, 0.8), close * 0.9)
    out[:, J] = np.clip(jaw * (1 - close), 0, 1)
    out[:, W] = _smooth(wide, 3.0)
    out[:, R] = _smooth(rnd, 3.5)
    out[:, C] = np.clip(close, 0, 1)
    out[:, F] = _smooth(fric, 1.5)
    out[:, E] = _smooth(loud, 3.0)
    return out


# --- phonemes -> acoustic classes and mouth shapes --------------------------------------------------------------
# vowel targets for a female voice: (F1, F2) in Hz and the mouth shape (jaw, wide, round)
VOWELS = {
    "AA": ((850, 1300), (0.85, 0.12, 0.0)), "AE": ((800, 1900), (0.72, 0.45, 0.0)),
    "AH": ((650, 1400), (0.55, 0.12, 0.0)), "AO": ((650, 1000), (0.62, 0.0, 0.55)),
    "EH": ((600, 2000), (0.5, 0.4, 0.0)), "ER": ((480, 1500), (0.3, 0.0, 0.45)),
    "IH": ((430, 2200), (0.32, 0.45, 0.0)), "IY": ((330, 2600), (0.2, 0.7, 0.0)),
    "UH": ((450, 1300), (0.32, 0.0, 0.5)), "UW": ((350, 1000), (0.15, 0.0, 0.9)),
    "OH": ((560, 1050), (0.5, 0.0, 0.6)),                       # first half of OW
}
DIPHTHONGS = {"AW": ("AA", "UW"), "AY": ("AA", "IY"), "EY": ("EH", "IY"), "OW": ("OH", "UW"), "OY": ("AO", "IY")}
# consonant -> (acoustic class(es), mouth shape {channel: value})
CONSONANTS = {
    "M": (("nasal",), {C: 1.0}), "B": (("closure_v", "release"), {C: 1.0}), "P": (("closure", "release"), {C: 1.0}),
    "F": (("weak",), {B: 1.0, J: 0.08}), "V": (("weak",), {B: 1.0, J: 0.08}),
    "TH": (("weak",), {T: 1.0, J: 0.16, W: 0.2}), "DH": (("weak",), {T: 0.8, J: 0.16, W: 0.2}),
    "S": (("sibilant",), {J: 0.06, W: 0.45, F: 1.0}), "Z": (("sibilant",), {J: 0.06, W: 0.45, F: 1.0}),
    "SH": (("hush",), {J: 0.12, R: 0.65, F: 1.0}), "ZH": (("hush",), {J: 0.12, R: 0.65, F: 1.0}),
    "CH": (("closure", "hush"), {J: 0.12, R: 0.6, F: 0.8}), "JH": (("closure_v", "hush"), {J: 0.12, R: 0.6, F: 0.8}),
    "T": (("closure", "release"), {J: 0.16, W: 0.15, T: 0.3}), "D": (("closure_v", "release"), {J: 0.16, W: 0.15, T: 0.3}),
    "N": (("nasal",), {J: 0.16, W: 0.12, T: 0.3}), "L": (("liquid",), {J: 0.28, T: 0.6}),
    "K": (("closure", "release"), {J: 0.24}), "G": (("closure_v", "release"), {J: 0.24}), "NG": (("nasal",), {J: 0.22}),
    "R": (("liquid",), {J: 0.2, R: 0.45}), "W": (("glide",), {J: 0.08, R: 0.95}), "Y": (("glide",), {J: 0.18, W: 0.45}),
    "HH": (("aspirate",), {}),                                   # shaped like the following vowel
}
MIN_FRAMES = {"vowel": 3, "nasal": 3, "closure": 2, "closure_v": 2, "release": 1, "sil": 1}
# duration prior: log-cost of each extra frame (consonants are short unless the audio insists)
LOOP_COST = {"nasal": -0.08, "liquid": -0.15, "glide": -0.15, "aspirate": -0.15, "weak": -0.05,
             "closure": -0.04, "closure_v": -0.06}


def _class_scores(q: _Cues, needed: set[str]) -> dict[str, np.ndarray]:
    """Log-scores (higher = better match) of each frame for the acoustic classes in ``needed``."""
    lg = lambda m: np.log(np.clip(m, 1e-3, 1.0))   # noqa: E731
    L, V, N = q.loud, q.voiced, q.noisy
    quiet = 1 - L
    murmur = _ramp(q.centroid, 650.0, 380.0) * np.nan_to_num(_ramp(q.f1, 520.0, 380.0), nan=0.6)
    sib = _ramp(q.centroid, 3800.0, 5600.0)
    onset = np.zeros_like(L)
    onset[2:] = _ramp(q.db[2:] - np.minimum(q.db[1:-1], q.db[:-2]), 2.0, 10.0)
    out: dict[str, np.ndarray] = {}
    for c in needed:
        if c == "sil":
            m = quiet ** 2
        elif c == "closure":
            m = quiet ** 1.5
        elif c == "closure_v":
            m = np.maximum(quiet ** 1.5, V * murmur * quiet ** 0.5)
        elif c == "release":
            m = 0.25 + 0.75 * np.maximum(N, onset)
        elif c == "nasal":
            m = V * (1 - N) * (0.1 + 0.9 * murmur) * L ** 0.3
        elif c in ("liquid", "glide"):
            m = V * (1 - N) * L ** 0.5 * (0.35 + 0.4 * murmur)
        elif c == "sibilant":
            m = N * (0.15 + 0.85 * sib) * L ** 0.3
        elif c == "hush":
            m = N * (0.3 + 0.7 * (1 - sib)) * L ** 0.3
        elif c == "weak":
            m = (0.35 + 0.65 * quiet) * (0.4 + 0.6 * N) * (1 - 0.6 * V * L)
        elif c == "aspirate":
            m = (0.4 + 0.6 * N) * (0.3 + 0.7 * quiet) * (1 - 0.5 * V)
        elif c.startswith("v:"):
            f1t, f2t = VOWELS[c[2:]][0]
            q1 = np.nan_to_num(np.exp(-((q.f1 - f1t) / 200.0) ** 2), nan=0.4)
            q2 = np.nan_to_num(np.exp(-((q.f2 - f2t) / 700.0) ** 2), nan=0.4)
            m = V * (1 - N) * L ** 0.4 * (0.45 + 0.35 * q1 + 0.2 * q2) * (1 - 0.4 * murmur)
        else:
            raise KeyError(c)
        out[c] = lg(m)
    return out


@dataclass
class _State:
    cls: str            # acoustic class
    phone: int          # index into the phone list (-1 = pause)
    loop: bool          # last state of its chain: may repeat
    optional: bool = False   # a pause that may be skipped
    enter: float = 0.0  # log-penalty for entering (pauses)


def _phone_states(words: list[list[str]]) -> tuple[list[_State], list[str]]:
    """Expand words into a left-to-right state chain with minimum durations and optional pauses."""
    states: list[_State] = [_State("sil", -1, True, True)]
    plist: list[str] = []

    def chain(cls: str, frames: int, idx: int):
        for i in range(frames):
            states.append(_State(cls, idx, i == frames - 1 and cls != "release"))

    for wi, word in enumerate(words):
        if word == [phonemes.PAUSE]:
            if not states[-1].optional:
                states.append(_State("sil", -1, True, True, 0.0))
            continue
        if wi > 0 and not states[-1].optional:
            states.append(_State("sil", -1, True, True, -2.5))     # an unmarked pause between words
        for ph in word:
            base = ph.rstrip("012")
            idx = len(plist)
            plist.append(ph)
            if base in DIPHTHONGS:
                a, b = DIPHTHONGS[base]
                chain("v:" + a, 2, idx)
                chain("v:" + b, 2, idx)
            elif base in VOWELS:
                chain("v:" + base, MIN_FRAMES["vowel"], idx)
            elif base in CONSONANTS:
                for cls in CONSONANTS[base][0]:
                    chain(cls, MIN_FRAMES.get(cls, 2), idx)
    if not states[-1].optional:
        states.append(_State("sil", -1, True, True))
    return states, plist


def align(raw: dict[str, np.ndarray], words: list[list[str]], open_end: bool = False):
    """Viterbi-align ``words`` (phone lists) to the frames in ``raw``.

    Returns ``(phone index per frame (-1 = pause), class per frame, phone list, confidence)`` or
    None when the sentence has more phones than frames can hold.
    """
    n = len(raw["db"])
    states, plist = _phone_states(words)
    s_count = len(states)
    if n == 0 or not plist or (not open_end and sum(not s.optional for s in states) > n):
        return None
    q = _cues(raw)
    scores = _class_scores(q, {s.cls for s in states})
    emit = np.stack([scores[s.cls] for s in states], axis=1)               # (n, S)
    loop = np.array([LOOP_COST.get(s.cls, 0.0) if s.loop else -np.inf for s in states])
    enter = np.array([s.enter for s in states])
    skippable = np.array([s.optional for s in states])
    skip_ok = np.zeros(s_count, bool)
    skip_ok[2:] = skippable[1:-1]                    # s-2 -> s jumps over an optional state s-1

    delta = np.full(s_count, -np.inf)
    delta[0] = emit[0, 0]
    delta[1] = emit[0, 1] + enter[1]
    if skippable[1] and s_count > 2:
        delta[2] = emit[0, 2]
    back = np.zeros((n, s_count), np.int16 if s_count < 32000 else np.int32)
    idx = np.arange(s_count)
    for t in range(1, n):
        stay = delta + loop
        adv = np.full(s_count, -np.inf)
        adv[1:] = delta[:-1] + enter[1:]
        skip = np.full(s_count, -np.inf)
        skip[2:] = np.where(skip_ok[2:], delta[:-2] + enter[2:], -np.inf)
        cand = np.stack([stay, adv, skip])
        k = cand.argmax(axis=0)
        delta = cand[k, idx] + emit[t]
        back[t] = idx - k
    if open_end:
        s = int(np.argmax(delta))
    else:
        s = s_count - 1 if delta[-1] >= delta[-2] or not skippable[-1] else s_count - 2
    if not np.isfinite(delta[s]):
        return None
    path = np.empty(n, np.int64)
    for t in range(n - 1, -1, -1):
        path[t] = s
        s = back[t, s]
    phone_of = np.array([st.phone for st in states])[path]
    cls_of = np.array([st.cls for st in states])[path]
    # confidence: how much worse the forced path scores than free classification over the same classes
    best_free = emit.max(axis=1)
    conf = float(np.mean(emit[np.arange(n), path] - best_free))
    return phone_of, cls_of, plist, conf


def visemes(raw: dict[str, np.ndarray], phone_of: np.ndarray, cls_of: np.ndarray, plist: list[str]) -> np.ndarray:
    """Mouth channels from an alignment, shaped by loudness and smoothed with coarticulation."""
    n = len(phone_of)
    tgt = np.zeros((n, len(CHANNELS)))
    core_close = np.zeros(n)
    core_bite = np.zeros(n)
    # segments of consecutive frames on the same phone
    edges = np.flatnonzero(np.diff(phone_of)) + 1
    starts = np.r_[0, edges]
    ends = np.r_[edges, n]
    segs = [(int(a), int(b), int(phone_of[a])) for a, b in zip(starts, ends)]

    def vowel_shape(name: str, stress: str) -> np.ndarray:
        jaw, wide, rnd = VOWELS[name][1]
        v = np.zeros(len(CHANNELS))
        v[J], v[W], v[R] = jaw * (0.72 if stress == "0" else 1.0), wide, rnd
        return v

    for i, (a, b, p) in enumerate(segs):
        if p < 0:
            continue
        ph = plist[p]
        base, stress = ph.rstrip("012"), ph[len(ph.rstrip("012")):]
        if base in DIPHTHONGS:
            va, vb = (vowel_shape(x, stress) for x in DIPHTHONGS[base])
            u = np.linspace(0, 1, b - a)[:, None]
            u = np.clip((u - 0.25) / 0.6, 0, 1)
            tgt[a:b] = va * (1 - u) + vb * u
        elif base in VOWELS:
            tgt[a:b] = vowel_shape(base, stress)
        elif base == "HH":
            nxt = next((plist[q] for _, _, q in segs[i + 1:i + 2] if q >= 0), "")
            nb = nxt.rstrip("012")
            if nb in VOWELS:
                tgt[a:b] = vowel_shape(nb, "1") * 0.6
            elif nb in DIPHTHONGS:
                tgt[a:b] = vowel_shape(DIPHTHONGS[nb][0], "1") * 0.6
        elif base in CONSONANTS:
            held = np.flatnonzero(cls_of[a:b] != "release") + a      # stops: shape held until the release
            for ch, val in CONSONANTS[base][1].items():
                tgt[held, ch] = val
            core = held[1:-1] if len(held) > 2 else held
            if base in ("M", "B", "P"):
                core_close[core] = 1.0
            elif base in ("F", "V"):
                core_bite[core] = 1.0

    q = _cues(raw)
    # dynamics from the audio: louder syllables open wider; vowel openness nudged by the measured F1
    loud = _smooth(q.loud, 2.0)
    f1n = np.nan_to_num(_ramp(q.f1, 300.0, 800.0), nan=0.5)
    vowelish = (tgt[:, C] == 0) & (tgt[:, F] == 0) & (tgt[:, B] == 0)
    jaw = tgt[:, J] * (0.55 + 0.45 * loud)
    jaw = np.where(vowelish & (tgt[:, J] > 0.25), 0.8 * jaw + 0.2 * (0.2 + 0.7 * f1n) * loud, jaw)

    out = np.zeros_like(tgt)
    out[:, J] = _smooth(jaw, 2.0)
    out[:, W] = _smooth(tgt[:, W], 3.0)
    out[:, R] = _smooth(_shift(np.maximum(tgt[:, R], _shift(tgt[:, R], 6) * 0.7), 2), 3.5)   # rounding anticipates
    out[:, C] = np.maximum(_smooth(tgt[:, C], 1.5), core_close)
    out[:, F] = _smooth(tgt[:, F], 1.5)
    out[:, B] = np.maximum(_smooth(tgt[:, B], 1.5), core_bite)
    out[:, T] = _smooth(tgt[:, T], 2.0)
    out[:, E] = _smooth(q.loud, 3.0)
    out[:, J] *= 1 - out[:, C]
    out[:, W] *= 1 - 0.7 * out[:, C]
    return np.clip(out, 0, 1)


# --- streaming -----------------------------------------------------------------------------------------------
class MouthTracker:
    """Streams one sentence's audio (in playback order) into mouth frames.

    ``feed`` returns the frames that are final; the last ``hold_s`` stay back until more audio
    arrives (the alignment of the open end can still move, and a burst there may close the lips
    before it), ``flush`` returns the rest. Audio that doesn't continue the previous chunk, or a
    different sentence text, starts a new utterance.
    """

    def __init__(self, sr: int, lead_s: float = LEAD_S, hold_s: float = 0.2,
                 lexicon: dict[str, list[str]] | None = None):
        self.sr = sr
        self.hop = max(1, round(sr * HOP_S))
        self.win = max(64, round(sr * WIN_S) // 2 * 2)
        self.hold = int(round(hold_s / HOP_S))
        self.context = 40
        self.lead = int(round(lead_s * sr))
        self.analyzer = _Analyzer(sr, self.win)
        self.lexicon = lexicon
        self.ref_db = REF_DB0             # loudest recent speech level; carried across utterances
        self.last_confidence: float | None = None
        self.start: int | None = None
        self._reset(None, None)

    def _reset(self, start: int | None, text: str | None):
        self.start = start                           # sample index of frame 0's centre
        self.expect = start                          # next contiguous sample index
        self.text = text
        self.words = phonemes.phones(text, self.lexicon) if text else []
        self.audio = np.zeros(self.win // 2, np.float32)   # left pad so frame 0 is centred on `start`
        self.audio_off = 0                           # padded-timeline offset of self.audio[0]
        self.raw: dict[str, np.ndarray] = {}
        self.n = 0
        self.emitted = 0

    def _analyze(self):
        total = self.audio_off + len(self.audio)
        count = (total - self.n * self.hop - self.win) // self.hop + 1
        if count <= 0:
            return
        base = self.n * self.hop - self.audio_off
        idx = base + np.arange(count)[:, None] * self.hop + np.arange(self.win)[None, :]
        feats = self.analyzer(self.audio[idx])
        refs = np.empty(count)
        ref = self.ref_db
        for i, d in enumerate(feats["db"]):          # causal running peak, ~1 dB/s release
            ref = max(ref - 0.01, d, -45.0)
            refs[i] = ref
        self.ref_db = ref
        feats["ref"] = refs
        for k, v in feats.items():
            self.raw[k] = np.concatenate([self.raw[k], v]) if k in self.raw else v
        self.n += count
        keep_from = self.n * self.hop - self.audio_off   # audio needed by the next frame's window
        if keep_from > 0:
            self.audio = self.audio[keep_from:]
            self.audio_off += keep_from

    def _values(self, final: bool) -> tuple[np.ndarray, int, str]:
        """Channel values for frames [lo, n) and lo."""
        if self.words and self.n <= MAX_ALIGN_FRAMES:
            res = align(self.raw, self.words, open_end=not final)
            if res is not None:
                phone_of, cls_of, plist, conf = res
                self.last_confidence = conf
                if conf >= MIN_CONFIDENCE:
                    return visemes(self.raw, phone_of, cls_of, plist), 0, "aligned"
        lo = max(0, self.emitted - self.context)
        return shape({k: v[lo:self.n] for k, v in self.raw.items()}), lo, "acoustic"

    def _emit(self, upto: int, final: bool = False) -> MouthFrames | None:
        if upto <= self.emitted:
            return None
        values, lo, source = self._values(final)
        out = MouthFrames(self.start + self.emitted * self.hop - self.lead, self.hop, self.sr,
                          values[self.emitted - lo: upto - lo], source, self.text)
        self.emitted = upto
        drop = self.emitted - self.context        # without text, old features are no longer needed
        if (not self.words or self.n > MAX_ALIGN_FRAMES) and drop > 512:
            self.raw = {k: v[drop:] for k, v in self.raw.items()}
            self.n -= drop
            self.emitted -= drop
            self.audio_off -= drop * self.hop
            self.start += drop * self.hop
        return out

    def feed(self, samples, index: int, text: str | None = None) -> list[MouthFrames]:
        """Add audio of ``text`` that starts at player sample ``index``; returns frames now final."""
        out = []
        if self.start is None or index != self.expect or text != self.text:
            out += self.flush()
            self._reset(index, text)
        samples = np.asarray(samples, dtype=np.float32).ravel()
        self.audio = np.concatenate([self.audio, samples])
        self.expect = index + len(samples)
        self._analyze()
        frames = self._emit(self.n - self.hold)
        return out + ([frames] if frames else [])

    def flush(self) -> list[MouthFrames]:
        """End of the utterance: the remaining frames, closing into silence."""
        if self.start is None:
            return []
        self.audio = np.concatenate([self.audio, np.zeros(self.win // 2 + 3 * self.hop, np.float32)])
        self._analyze()
        frames = self._emit(self.n, final=True)
        self.start = None
        return [frames] if frames else []

    def cancel(self):
        """Speech was cut off: drop everything not yet sent."""
        self.start = None


def analyze(audio: np.ndarray, sr: int, text: str | None = None, lead_s: float = LEAD_S) -> MouthFrames:
    """Whole-clip convenience (tests, tools): frames for ``audio`` starting at sample 0."""
    tr = MouthTracker(sr, lead_s=lead_s)
    parts = tr.feed(audio, 0, text) + tr.flush()
    values = np.concatenate([p.values for p in parts]) if parts else np.zeros((0, len(CHANNELS)))
    src = "aligned" if parts and all(p.source == "aligned" for p in parts) else "acoustic"
    return MouthFrames(parts[0].start if parts else -tr.lead, tr.hop, sr, values, src)
