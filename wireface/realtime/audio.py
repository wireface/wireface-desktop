"""The realtime voice's speaker: plays the model's voice as it streams in, with lip sync.

It uses the same ``AudioPlayer`` and ``MouthTracker`` as the classic voice, so the face's mouth follows it the same
way (frames keyed to the player's clock). The microphone is the classic one (``hearing.py``): speech recognition runs
on this PC and the words go to the agent, so the realtime model never hears the user.
"""
from __future__ import annotations

import collections
import logging
import queue
import threading

import numpy as np

from ..lipsync import MouthTracker, resync, wall_clock
from ..speech import AudioPlayer

log = logging.getLogger(__name__)


class Speaker:
    """Streams 16-bit PCM to the speakers; ``on_mouth(frames)`` gets lip-sync frames (from its worker thread)."""

    def __init__(self, sr: int, on_mouth=None, stream: bool = True, device=None):
        self.sr = sr
        self.player = AudioPlayer(sr, device=device, stream=stream)
        self.on_mouth = on_mouth
        self.mouth = MouthTracker(sr) if on_mouth else None
        self.caption: str | None = None          # the words being spoken (shown with the mouth frames)
        self.spoken: collections.deque[tuple[int, str]] = collections.deque(maxlen=64)   # (player index, words)
        self._queued = 0                         # samples handed to write() so far: the index the next audio gets
        self._q: queue.Queue = queue.Queue()
        self._epoch = 0
        self._working = False
        self._start: int | None = None           # player index of the current reply's first sample
        self._thread = threading.Thread(target=self._run, name="realtime-speaker", daemon=True)
        self._thread.start()

    def write(self, pcm: bytes) -> None:
        self._q.put(("audio", pcm, self._epoch))
        self._queued += len(pcm) // 2

    def words(self, text: str, n_samples: int = 0) -> int:
        """These words go with the audio just written (the last ``n_samples`` of it): kept to tell the face's own voice
        from the user's (``spoken``). Returns the player index they start at."""
        index = max(0, self._queued - n_samples)
        self.spoken.append((index, text))
        return index

    def playing_index(self) -> int | None:
        """The player index coming out of the speakers now (None before anything has played)."""
        clock = self.player.clock
        if clock is None:
            return None
        first, at, end = clock
        now = first + (wall_clock() - at) * self.sr
        return int(min(now, end) if end is not None else now)

    def end(self) -> None:
        """The reply is complete: close the mouth after its last sound."""
        self._q.put(("end", None, self._epoch))

    def stop(self) -> None:
        """Silence now and forget what is queued."""
        self._epoch += 1
        self._queued = self.player.written
        self.player.clear()
        if self.mouth:
            self.mouth.cancel()
        self._start = None

    def close(self) -> None:
        self.stop()
        self._q.put(None)
        self.player.close()

    @property
    def busy(self) -> bool:
        return self._working or not self._q.empty() or self.player.pending_s() > 0 or self.player.drain_s() > 0

    def heard_ms(self) -> int:
        """How much of the current reply has come out of the speakers."""
        clock, start = self.player.clock, self._start
        if clock is None or start is None:
            return 0
        first, at, end = clock
        now = first + (wall_clock() - at) * self.sr
        if end is not None:
            now = min(now, end)
        return max(0, int((now - start) * 1000 / self.sr))

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            kind, pcm, epoch = item
            if epoch != self._epoch:
                continue
            self._working = True
            try:
                if kind == "audio":
                    self._play(pcm, epoch)
                elif self.mouth:
                    self._mouth(self.mouth.flush, epoch)
                    self._start = None
            except Exception:
                log.exception("realtime speaker")
            finally:
                self._working = False

    def _play(self, pcm: bytes, epoch: int) -> None:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if not len(samples):
            return
        if self.player.pending_s() <= 0 and self.player.drain_s() <= 0 and self.mouth:
            resync()                                   # the start of a reply: re-anchor the clock the face uses
        index = self.player.write(samples)
        if self._start is None:
            self._start = index
        if self.mouth:
            self._mouth(lambda: self.mouth.feed(samples, index), epoch)

    def _mouth(self, produce, epoch: int) -> None:
        try:
            for frames in produce():
                if epoch != self._epoch:
                    return
                frames.text = self.caption
                self.on_mouth(frames)
        except Exception:
            log.exception("lip sync failed (speech continues)")
            self.mouth.cancel()

