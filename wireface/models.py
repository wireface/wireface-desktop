"""Speech and face models: what is needed, whether it is installed, and downloading what is missing.

Standard library only, so ``scripts\\setup.ps1`` can run it before anything else is installed and the app can
call it while running. The models live in one folder shared by every face (``config.MODELS``).

    python -m wireface.models --check            list what is installed / missing (exit 1 if anything is)
    python -m wireface.models --install          download and unpack what is missing
    python -m wireface.models --check --load     also load every model once and list the audio devices
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config

SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
USER_AGENT = "wireface-setup"

Progress = Callable[[str, int, int, str], None]    # (model key, bytes done, bytes total (0: unknown), phase)


class MissingModels(RuntimeError):
    """A feature can't start because its model files are not installed; the message says what to do."""


@dataclass(frozen=True)
class Model:
    key: str
    label: str
    url: str
    dest: Path                       # the folder (archives) or the file this model ends up as
    check: tuple[str, ...]           # files, relative to ``dest`` (empty: ``dest`` itself is the file)
    size_mb: int
    needed_for: tuple[str, ...]      # "listening", "voice", "photo"
    archive: bool = True
    optional: bool = False           # only installed on request

    def missing_files(self) -> list[str]:
        if not self.check:
            return [] if self.dest.is_file() else [self.dest.name]
        return [f for f in self.check if not (self.dest / f).exists()]

    @property
    def present(self) -> bool:
        return not self.missing_files()


def manifest() -> list[Model]:
    s = config.SPEECH_DIR
    return [
        Model("vad", "Silero voice activity detector", f"{SHERPA}/asr-models/silero_vad.onnx",
              config.VAD_MODEL, (), 1, ("listening",), archive=False),
        Model("denoiser", "GTCRN noise reduction", f"{SHERPA}/speech-enhancement-models/gtcrn_simple.onnx",
              config.DENOISER_MODEL, (), 1, ("noise",), archive=False),
        Model("asr", "Parakeet speech recognition", f"{SHERPA}/asr-models/{config.OFFLINE_ASR_DIR.name}.tar.bz2",
              config.OFFLINE_ASR_DIR,
              ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"), 501, ("listening",)),
        Model("pocket", "Pocket text to speech", f"{SHERPA}/tts-models/{config.POCKET_DIR.name}.tar.bz2",
              config.POCKET_DIR,
              ("lm_flow.int8.onnx", "lm_main.int8.onnx", "encoder.onnx", "decoder.int8.onnx",
               "text_conditioner.onnx", "vocab.json", "token_scores.json", "test_wavs"), 98, ("voice",)),
        Model("lexicon", "Pronunciation lexicon and name spotter", f"{SHERPA}/kws-models/{config.KWS_DIR.name}.tar.bz2",
              config.KWS_DIR, ("en.phone", "tokens.txt", "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
                               "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
                               "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx"), 33, ("voice",)),
        Model("kokoro", "Kokoro text to speech", f"{SHERPA}/tts-models/{config.KOKORO_DIR.name}.tar.bz2",
              config.KOKORO_DIR, ("model.int8.onnx", "voices.bin", "tokens.txt", "espeak-ng-data"), 103, ("voice",),
              optional=True),
        Model("landmarker", "Face landmarker (photo faces)",
              "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/"
              "face_landmarker.task", config.LANDMARKER, (), 4, ("photo",), archive=False),
    ]


def wanted(with_kokoro: bool = False) -> list[Model]:
    """What a normal install needs: everything except Kokoro (unless it is the configured engine)."""
    kokoro = with_kokoro or config.TTS_ENGINE == "kokoro"
    return [m for m in manifest() if not m.optional or (m.key == "kokoro" and kokoro)]


def status(keys: list[str] | None = None) -> list[dict]:
    return [{"key": m.key, "label": m.label, "present": m.present, "missing": m.missing_files(),
             "size_mb": m.size_mb, "needed_for": list(m.needed_for), "optional": m.optional}
            for m in manifest() if keys is None or m.key in keys]


def engine_key(engine: str | None = None) -> str:
    return "kokoro" if (engine or config.TTS_ENGINE) == "kokoro" else "pocket"


def missing_for(feature: str, engine: str | None = None) -> list[Model]:
    """The models ``feature`` ("listening", "voice" or "photo") needs that are not installed.
    ``engine`` is the local voice in use (default: ``config.TTS_ENGINE``)."""
    out = []
    for m in manifest():
        if feature not in m.needed_for or m.present:
            continue
        if feature == "voice" and m.key in ("pocket", "kokoro") and m.key != engine_key(engine):
            continue
        out.append(m)
    return out


def missing_message(feature: str, engine: str | None = None) -> str | None:
    """A sentence for the user when ``feature`` can't work for lack of models; None when it is fine."""
    gone = missing_for(feature, engine)
    if not gone:
        return None
    what = {"listening": "listen", "voice": "speak", "photo": "map a photo"}.get(feature, feature)
    names = ", ".join(m.label for m in gone)
    return (f"I can't {what}: the speech models are not installed ({names}). "
            f"Run scripts\\setup.ps1 (or 'python -m wireface.models --install') to download them.")


# --- downloading -------------------------------------------------------------------------------------------
def _open(url: str, start: int = 0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if start:
        req.add_header("Range", f"bytes={start}-")
    return urllib.request.urlopen(req, timeout=60)


def download(url: str, target: Path, progress: Callable[[int, int], None] | None = None, retries: int = 4) -> None:
    """Fetch ``url`` into ``target`` through ``target.part`` (resuming an earlier partial download)."""
    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(retries):
        have = part.stat().st_size if part.exists() else 0
        try:
            with _open(url, have) as resp:
                if have and resp.status != 206:            # the server ignored the range: start again
                    have = 0
                length = int(resp.headers.get("Content-Length") or 0)
                total = have + length if length else 0
                with open(part, "ab" if have else "wb") as out:
                    done = have
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            if total and part.stat().st_size != total:
                raise OSError(f"incomplete download ({part.stat().st_size} of {total} bytes)")
            os.replace(part, target)
            return
        except urllib.error.HTTPError as e:
            if e.code == 416 and part.exists():            # asked to resume past the end: it was already complete
                os.replace(part, target)
                return
            last = e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = e
        time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"could not download {url}: {last}")


def safe_extract(archive: Path, into: Path) -> None:
    """Unpack a .tar.bz2, refusing anything that would land outside ``into``."""
    root = into.resolve()
    with tarfile.open(archive, "r:bz2") as tar:
        for member in tar.getmembers():
            target = (into / member.name).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"unsafe path in archive: {member.name}")
            if member.issym() or member.islnk():
                link = (target.parent / member.linkname).resolve() if member.issym() else (into / member.linkname).resolve()
                if root != link and root not in link.parents:
                    raise RuntimeError(f"unsafe link in archive: {member.name}")
        tar.extractall(into)


def install_model(m: Model, progress: Progress | None = None) -> None:
    def report(phase):
        return lambda done, total: progress(m.key, done, total, phase) if progress else None

    m.dest.parent.mkdir(parents=True, exist_ok=True)
    if not m.archive:
        download(m.url, m.dest, report("download"))
        return
    archive = m.dest.parent / f".{m.dest.name}.tar.bz2"
    work = m.dest.parent / f".{m.dest.name}.extract"
    try:
        download(m.url, archive, report("download"))
        if progress:
            progress(m.key, 0, 0, "unpack")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        safe_extract(archive, work)
        entries = list(work.iterdir())
        top = entries[0] if len(entries) == 1 and entries[0].is_dir() else work
        if m.dest.exists():
            shutil.rmtree(m.dest)
        shutil.move(str(top), str(m.dest))
        gone = m.missing_files()
        if gone:
            raise RuntimeError(f"{m.label}: the archive did not contain {', '.join(gone)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if m.present:                     # keep a failed archive: the next run resumes or re-checks it
            archive.unlink(missing_ok=True)


def install(keys: list[str] | None = None, progress: Progress | None = None, with_kokoro: bool = False) -> list[str]:
    """Download whatever of ``keys`` (default: a normal install) is not there yet; returns the keys installed."""
    chosen = [m for m in (manifest() if keys else wanted(with_kokoro)) if keys is None or m.key in keys]
    done = []
    for m in chosen:
        if m.present:
            continue
        install_model(m, progress)
        done.append(m.key)
    return done


# --- checking that they work ----------------------------------------------------------------------------------
def verify_load() -> list[str]:
    """Load each speech model once (catches a truncated download or a sherpa-onnx mismatch); returns problems."""
    problems: list[str] = []

    def attempt(label, fn):
        try:
            fn()
            print(f"  ok       {label}")
        except Exception as e:                                    # noqa: BLE001 - report whatever went wrong
            problems.append(f"{label}: {e}")
            print(f"  FAILED   {label}: {e}")

    from . import speech

    attempt("voice activity detector", speech.make_vad)
    attempt("speech recognition", speech.make_offline_recognizer)
    attempt(f"text to speech ({config.TTS_ENGINE})", lambda: speech.make_tts(config.TTS_ENGINE))
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        has_in = any(d["max_input_channels"] > 0 for d in devices)
        has_out = any(d["max_output_channels"] > 0 for d in devices)
        print(f"  {'ok      ' if has_in else 'MISSING '} microphone")
        print(f"  {'ok      ' if has_out else 'MISSING '} speakers")
        if not has_in:
            problems.append("no microphone found")
        if not has_out:
            problems.append("no audio output found")
    except Exception as e:                                        # noqa: BLE001
        problems.append(f"audio devices: {e}")
        print(f"  FAILED   audio devices: {e}")
    return problems


def _cli_progress():
    state = {"key": None, "at": 0.0}

    def show(key, done, total, phase):
        now = time.monotonic()
        if key != state["key"]:
            state["key"] = key
            print(f"\n{key}: {phase}", flush=True)
        if now - state["at"] < 0.4 and not (total and done >= total):
            return
        state["at"] = now
        if phase == "unpack":
            print("  unpacking...", flush=True)
        elif total:
            print(f"\r  {done / 1e6:7.1f} / {total / 1e6:.1f} MB ({100 * done // total}%)", end="", flush=True)
        else:
            print(f"\r  {done / 1e6:7.1f} MB", end="", flush=True)

    return show


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wireface.models", description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="list installed / missing models")
    ap.add_argument("--install", action="store_true", help="download what is missing")
    ap.add_argument("--with-kokoro", action="store_true", help="also install the Kokoro voice (optional)")
    ap.add_argument("--load", action="store_true", help="with --check: load every model and list audio devices")
    args = ap.parse_args(argv)
    if not (args.check or args.install):
        ap.error("give --check and/or --install")

    print(f"Models folder: {config.MODELS}")
    if args.install:
        try:
            done = install(progress=_cli_progress(), with_kokoro=args.with_kokoro)
        except Exception as e:                                    # noqa: BLE001
            print(f"\nInstall failed: {e}", file=sys.stderr)
            return 1
        print("\nInstalled: " + (", ".join(done) if done else "nothing to do (all present)"))
    bad = 0
    shown = {m.key for m in wanted(args.with_kokoro)}
    for row in status():
        if row["key"] not in shown and not row["present"]:       # an optional model nobody asked for
            continue
        state = "ok     " if row["present"] else "MISSING"
        bad += not row["present"]
        print(f"  {state}  {row['label']}" + ("" if row["present"] else f"  (needs {', '.join(row['missing'])})"))
    if args.load and not bad:
        bad += len(verify_load())
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
