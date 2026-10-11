"""Face settings: how the talking face looks and moves.

The face itself is a web page (``web/face*``) shown by a small Electron shell (``shell/``) as a
floating face or a floating panel. Settings live in ``data/face.json``; the API validates every
change with ``validate``. The face always wears a photo (a built-in portrait or the user's own) over the glowing
wireframe; ``skin_amount`` slides it from the wireframe (0) to the full skin (1).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
import struct
import threading
import time

from . import config

log = logging.getLogger(__name__)

PATH = config.DATA / "face.json"
WINDOW_STATE_PATH = config.DATA / "face_window.json"     # window positions, owned by the Electron shell
PHOTO_DIR = config.DATA / "face"

MODES = ("floating", "windowed")
STYLES = ("photo",)
LEGACY_STYLES = ("hologram", "sculpt")       # older faces: now the photo style, hologram with the skin slid off
TEETH_STYLES = ("normal", "braces", "vampire", "goofy", "sharp", "turkey")
BACKGROUNDS = ("none", "solid", "gradient", "grid", "stars")

# expressions the agent (its face tool) and the panel can put on the face, for a few seconds
EXPRESSIONS = ("neutral", "happy", "excited", "amused", "surprised", "curious", "thinking", "concerned", "sad",
               "afraid", "frustrated", "calm")
EXPRESSION_ALIASES = {"fear": "afraid", "scared": "afraid", "worried": "concerned", "joy": "happy", "smile": "happy",
                      "glad": "happy", "excitement": "excited", "surprise": "surprised", "shocked": "surprised",
                      "confused": "thinking", "pondering": "thinking", "upset": "sad", "angry": "frustrated",
                      "annoyed": "frustrated", "relaxed": "calm", "laugh": "amused", "interested": "curious"}
PORTRAITS_DIR = config.WEB / "face" / "portraits"
DEFAULT_PORTRAIT = "sarah"                     # what a fresh face wears


def expression(name: str) -> str:
    """The expression called ``name`` (or one of its everyday names); ValueError if there is none."""
    key = str(name or "").strip().lower()
    key = EXPRESSION_ALIASES.get(key, key)
    if key not in EXPRESSIONS:
        raise ValueError(f"unknown expression {name!r} (one of {', '.join(EXPRESSIONS)})")
    return key


def portraits() -> list[dict]:
    """The built-in photo faces (``web/face/portraits/portraits.json``): id, label, the image URLs, and the URL of the
    face landmarks found in it (``<id>.json``, from scripts/portrait-landmarks.py; None if there are none yet)."""
    try:
        listed = json.loads((PORTRAITS_DIR / "portraits.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for p in listed if isinstance(listed, list) else []:
        pid = str(p.get("id", ""))
        if re.fullmatch(r"[a-z0-9-]{1,40}", pid) and (PORTRAITS_DIR / f"{pid}.jpg").is_file():
            out.append({"id": pid, "label": str(p.get("label") or pid), "image": f"/static/face/portraits/{pid}.jpg",
                        "thumb": f"/static/face/portraits/{pid}-thumb.jpg"
                        if (PORTRAITS_DIR / f"{pid}-thumb.jpg").is_file() else f"/static/face/portraits/{pid}.jpg",
                        "landmarks": f"/static/face/portraits/{pid}.json" if (PORTRAITS_DIR / f"{pid}.json").is_file() else None})
    return out


def write_photo(photo_dir, jpeg: bytes, landmarks: list):
    """Put a picture and its 468 face landmarks ([x, y], 0..1) where the face page fetches them."""
    if len(landmarks) < 468 or not all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in landmarks[:468]):
        raise ValueError("expected 468 face landmarks")
    pts = [[round(float(p[0]), 5), round(float(p[1]), 5)] for p in landmarks[:468]]
    photo_dir.mkdir(parents=True, exist_ok=True)
    (photo_dir / "photo.jpg").write_bytes(jpeg)
    (photo_dir / "landmarks.json").write_text(json.dumps(pts), encoding="utf-8")


def jpeg_size(jpeg: bytes) -> tuple[int, int]:
    """(width, height) from a JPEG's frame header; (0, 0) if there is none."""
    i = 2
    while i + 9 <= len(jpeg) and jpeg[i] == 0xFF:
        marker = jpeg[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", jpeg[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", jpeg[i + 2:i + 4])[0]
    return 0, 0


def wear_portrait(pid: str, photo_dir) -> dict:
    """Copy a built-in portrait and its landmarks (``<id>.json``, made by scripts/portrait-landmarks.py) into
    ``photo_dir``; returns the config's new ``photo``. ValueError if there is no such portrait, or no landmarks."""
    if not any(p["id"] == pid for p in portraits()):
        raise ValueError(f"no portrait {pid!r}")
    try:
        jpeg = (PORTRAITS_DIR / f"{pid}.jpg").read_bytes()
        landmarks = json.loads((PORTRAITS_DIR / f"{pid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError(f"the portrait {pid!r} has no landmarks yet") from None
    write_photo(photo_dir, jpeg, landmarks if isinstance(landmarks, list) else [])
    w, h = jpeg_size(jpeg)
    return {"version": secrets.token_hex(4), "w": w, "h": h, "portrait": pid}


def default_photo(photo_dir) -> dict | None:
    """Put the default portrait on (or, failing that, the first that can be): its ``photo``; None if none can."""
    for pid in dict.fromkeys([DEFAULT_PORTRAIT, *(p["id"] for p in portraits())]):
        try:
            return wear_portrait(pid, photo_dir)
        except (OSError, ValueError) as e:
            log.warning("portrait %s: %s", pid, e)
    return None

# colour presets for the context menu and the Face page (primary glow, secondary rim, panel background)
PRESETS = {
    "claude": {"primary": "#d97757", "secondary": "#f2c38f", "background": "#14100c"},
    "cyan": {"primary": "#22d3ee", "secondary": "#6366f1", "background": "#060b18"},
    "green": {"primary": "#34f5a0", "secondary": "#0ea5e9", "background": "#03110b"},
    "amber": {"primary": "#fbbf24", "secondary": "#f97316", "background": "#140c02"},
    "violet": {"primary": "#a78bfa", "secondary": "#ec4899", "background": "#0e0718"},
    "rose": {"primary": "#fb7185", "secondary": "#f59e0b", "background": "#16060a"},
    "ice": {"primary": "#e0f2fe", "secondary": "#7dd3fc", "background": "#0b1220"},
}

DEFAULTS: dict = {
    "mode": "floating",           # floating (face only) | windowed (floating panel)
    "style": "photo",             # always (an older hologram or sculpt is read as photo: see validate)
    "skin_amount": 1.0,           # 0..1   wireframe (0) to skin (1): a cross-fade of the whole face
    "colors": {"primary": "#d97757", "secondary": "#f2c38f", "skin": "#c9a48e", "iris": "#4f86c6",
               "lips": "#b06a6e", "mouth": "#5c1018", "background": "#14100c"},   # mouth: inside of it
    "glow": 1.0,                  # 0..2   glow / bloom strength
    "wire": 0.85,                 # 0..1   mesh line opacity (the wireframe)
    "fill": 0.35,                 # 0..1   surface opacity (the wireframe)
    "scanlines": 0.3,             # 0..1
    "points": 0.0,                # 0..1   vertex dots
    "edge_fade": 0.5,             # 0..1   soft edge around the face
    "background": "gradient",     # panel background in windowed mode
    "eyes": True,                 # eyeballs with irises (off = hollow wireframe eyes)
    "iris_from_photo": True,      # a photo face keeps its own eye colour (off = colors.iris)
    "teeth_width": 1.0,           # 0.5..1.5 how far the teeth reach towards the mouth corners
    "teeth_size": 1.0,            # 0.5..1.5 how tall the teeth are
    "teeth_style": "normal",      # photographic teeth used by photo faces
    "follow_mouse": True,         # eyes and head follow the pointer
    "motion": 1.0,                # 0..2   idle head motion
    "expressiveness": 1.0,        # 0..2   expressions and gestures
    "mouth_gain": 1.0,            # 0.3..2 lip-sync amplitude
    "sync_offset_ms": 0,          # -200..200: positive = mouth later
    "captions": True,             # subtitles in the windowed panel
    "on_top": True,               # keep above other windows
    "zoom": 1.0,                  # 0.5..2 face size inside the windowed panel
    "photo": None,                # {"version": str, "w": int, "h": int, "portrait": id or None}; load() puts one on
}

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_NUMBERS = {"skin_amount": (0, 1), "glow": (0, 2), "wire": (0, 1), "fill": (0, 1), "scanlines": (0, 1), "points": (0, 1),
            "edge_fade": (0, 1), "motion": (0, 2), "expressiveness": (0, 2), "mouth_gain": (0.3, 2),
            "sync_offset_ms": (-200, 200), "zoom": (0.5, 2), "teeth_width": (0.5, 1.5), "teeth_size": (0.5, 1.5)}
_BOOLS = ("eyes", "iris_from_photo", "follow_mouse", "captions", "on_top")
_CHOICES = {"mode": MODES, "style": STYLES, "background": BACKGROUNDS, "teeth_style": TEETH_STYLES}


def validate(patch: dict, base: dict | None = None) -> dict:
    """Merge a (partial) settings patch onto ``base``; raises ValueError on anything invalid."""
    out = copy.deepcopy(base if base is not None else DEFAULTS)
    for key, value in patch.items():
        if key == "style" and value in LEGACY_STYLES:      # the wireframe is now the photo slid all the way off
            out["style"] = "photo"
            if value == "hologram":
                out["skin_amount"] = 0.0
        elif key in _CHOICES:
            if value not in _CHOICES[key]:
                raise ValueError(f"{key}: {value!r} not in {_CHOICES[key]}")
            out[key] = value
        elif key in _NUMBERS:
            lo, hi = _NUMBERS[key]
            try:
                v = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key}: not a number") from None
            if not lo <= v <= hi:
                raise ValueError(f"{key}: {v} outside [{lo}, {hi}]")
            out[key] = round(v) if key == "sync_offset_ms" else v
        elif key in _BOOLS:
            if not isinstance(value, bool):
                raise ValueError(f"{key}: expected true/false")
            out[key] = value
        elif key == "colors":
            if not isinstance(value, dict):
                raise ValueError("colors: expected an object")
            for name, c in value.items():
                if name not in DEFAULTS["colors"] or not isinstance(c, str) or not _COLOR.match(c):
                    raise ValueError(f"colors.{name}: expected #rrggbb")
                out["colors"][name] = c.lower()
        elif key == "preset":
            if value not in PRESETS:
                raise ValueError(f"preset: {value!r} not in {tuple(PRESETS)}")
            out["colors"].update(PRESETS[value])
        elif key == "photo":
            raise ValueError("photo: choose a portrait or upload one through /api/face/photo")
        else:
            raise ValueError(f"unknown face setting {key!r}")
    return out


def load(path=PATH, photo_dir=PHOTO_DIR) -> dict:
    """The saved settings, brought up to date: anything no longer valid is dropped, an older style becomes the photo
    style, and a face without a photo (a fresh one, or an old hologram) wears the default portrait. What changed is
    saved back, unless the file could not be read."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        saved = {}
    except (OSError, ValueError):
        log.warning("face.json can't be read: using the defaults")
        saved = None
    out = copy.deepcopy(DEFAULTS)
    data = dict(saved) if isinstance(saved, dict) else {}
    photo = data.pop("photo", None)
    for key, value in data.items():                    # skip (and so drop) anything no longer valid
        try:
            out = validate({key: value}, out)
        except ValueError:
            log.warning("face.json: ignoring %s=%r", key, value)
    if isinstance(photo, dict) and all((photo_dir / n).is_file() for n in ("photo.jpg", "landmarks.json")):
        out["photo"] = photo
    else:
        out["photo"] = default_photo(photo_dir)
    if isinstance(saved, dict) and out != saved:
        try:
            save(out, path)
        except OSError as e:
            log.warning("face.json: not saved (%s)", e)
    return out


_save_lock = threading.Lock()


def save(cfg: dict, path=PATH):
    """Atomic write. Sliders send bursts of changes, each saved from an executor thread: one at a time."""
    with _save_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:          # briefly opened by someone else (indexer, antivirus, an editor)
                if attempt == 9:
                    raise
                time.sleep(0.05)
