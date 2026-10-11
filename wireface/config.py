"""Paths and tunables shared by every face. Everything lives inside this folder except, if you have one, the
shared model folder (``MODELS_DIR``; see ``_models_dir``). What belongs to one face is in ``profile.py``."""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return type(default)(raw)


ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("WIREFACE_DATA", str(ROOT / "data")))
LOGS = DATA / "logs"
DEPS = Path(os.environ.get("WIREFACE_DEPS", str(ROOT / "deps")))    # installed by scripts\setup.ps1


def _models_dir() -> Path:
    """Model weights, shared by every face: ``MODELS_DIR`` if set, else the shared ``B:\\models`` if that
    exists on this PC, else ``deps\\models`` inside this folder (where scripts\\setup.ps1 installs them)."""
    env = os.environ.get("MODELS_DIR")
    if env:
        return Path(env)
    shared = Path(r"B:\models")
    return shared if shared.is_dir() else DEPS / "models"


MODELS = _models_dir()
SPEECH_DIR = Path(os.environ.get("SPEECH_DIR", str(MODELS / "speech")))

WEB = ROOT / "web"
SHELL_RUNTIME = ROOT / "shell"                                           # Electron main process (in the repo)
SHELL_TOOLS = DEPS / "shell"                                             # electron + mediapipe
ELECTRON_EXE = SHELL_TOOLS / "node_modules" / "electron" / "dist" / "electron.exe"
LANDMARKER = MODELS / "mediapipe" / "face_landmarker.task"

AGENT_NAME = _env("WIREFACE_NAME", "Claude")
PORT = _env("WIREFACE_PORT", 8791)
# where the agent's set_face tool has new faces drawn (facemaker.py): wireface.dev's portrait service (site/portrait)
PORTRAIT_URL = _env("WIREFACE_PORTRAIT_URL", "https://wireface.dev/api/portrait")

# --- speech ---------------------------------------------------------------------------------------
KWS_DIR = SPEECH_DIR / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"  # its en.phone is the lip-sync lexicon
VAD_MODEL = SPEECH_DIR / "silero_vad.onnx"
DENOISER_MODEL = SPEECH_DIR / "gtcrn_simple.onnx"                       # streaming noise reduction for the microphone
POCKET_DIR = SPEECH_DIR / "sherpa-onnx-pocket-tts-int8-2026-01-26"
KOKORO_DIR = SPEECH_DIR / "kokoro-int8-en-v0_19"
OFFLINE_ASR_DIR = SPEECH_DIR / "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming"
TTS_ENGINE = _env("TTS_ENGINE", "pocket")          # "pocket" | "kokoro"
KOKORO_SID = _env("KOKORO_SID", 0)
TTS_SPEED = _env("TTS_SPEED", 1.05)
ASR_THREADS = _env("ASR_THREADS", 6)

# --- Claude Code ------------------------------------------------------------------------------------
SANDBOX = DATA / "sandbox"                                            # where agents work unless told otherwise
DEFAULT_CWD = Path(os.environ.get("WIREFACE_CWD", str(SANDBOX)))
