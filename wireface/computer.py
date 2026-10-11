"""Computer use for the agent: screenshots of the desktop, and the mouse and keyboard, on Windows.

Claude Code has no desktop control of its own on Windows, so the face gives it some, as an in-process MCP server
(``backends/claude.py`` adds it): ``screenshot`` shows the screen, and ``click``, ``drag``, ``scroll``, ``type_text`` and
``key`` act on it. Coordinates are in the pixels of the last screenshot (scaled down to keep images small), so the agent
points at what it saw; they are mapped back to the screen here. Everything goes through user32 / gdi32 with ctypes:
nothing to install.

All calls run in a worker thread, made aware of display scaling so screen pixels are real pixels.
"""
from __future__ import annotations

import base64
import ctypes
import struct
import sys
import time
import zlib
from ctypes import wintypes

import numpy as np

MAX_WIDTH = 1366              # screenshots wider than this are scaled down (fewer tokens, still readable)
DPI_AWARE_V2 = ctypes.c_void_p(-4)

if sys.platform == "win32":
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


MOUSE, KEYBOARD = 0, 1
LEFTDOWN, LEFTUP, RIGHTDOWN, RIGHTUP, MIDDLEDOWN, MIDDLEUP, WHEEL = 0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x800
KEYUP, UNICODE = 0x2, 0x4
BUTTONS = {"left": (LEFTDOWN, LEFTUP), "right": (RIGHTDOWN, RIGHTUP), "middle": (MIDDLEDOWN, MIDDLEUP)}
VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B, "cmd": 0x5B,
      "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
      "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
      "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C, "capslock": 0x14,
      **{f"f{i}": 0x6F + i for i in range(1, 25)}}


def _aware():
    """Real pixels in this thread, whatever the display scaling."""
    try:
        user32.SetThreadDpiAwarenessContext(DPI_AWARE_V2)
    except (AttributeError, OSError):
        pass


def screen_size() -> tuple[int, int]:
    _aware()
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _grab() -> np.ndarray:
    """The primary screen as an (h, w, 3) RGB array."""
    _aware()
    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    src = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    try:
        gdi32.SelectObject(mem, bmp)
        gdi32.BitBlt(mem, 0, 0, w, h, src, 0, 0, 0x00CC0020 | 0x40000000)     # SRCCOPY | CAPTUREBLT
        header = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)   # top-down 32-bit BGRA
        buf = ctypes.create_string_buffer(w * h * 4)
        bmi = ctypes.create_string_buffer(header + b"\0" * 16)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, bmi, 0)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, src)
    return np.frombuffer(buf.raw, np.uint8).reshape(h, w, 4)[:, :, 2::-1]


def _shrink(img: np.ndarray, width: int) -> np.ndarray:
    """Area-average down to ``width`` (keeps text legible better than skipping pixels)."""
    h, w = img.shape[:2]
    if w <= width:
        return img
    scale = w / width
    new_h = max(1, round(h / scale))
    xs = np.linspace(0, w, width + 1).astype(int)
    ys = np.linspace(0, h, new_h + 1).astype(int)
    col = np.add.reduceat(img.astype(np.uint32), xs[:-1], axis=1) / np.diff(xs)[None, :, None]
    out = np.add.reduceat(col, ys[:-1], axis=0) / np.diff(ys)[:, None, None]
    return out.astype(np.uint8)


def png(img: np.ndarray) -> bytes:
    h, w = img.shape[:2]
    raw = b"".join(b"\0" + img[y].tobytes() for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class Computer:
    """The desktop as the agent sees it: ``scale`` maps screenshot pixels back to screen pixels."""

    def __init__(self, max_width: int = MAX_WIDTH):
        self.max_width = max_width
        self.scale = 1.0

    def screenshot(self) -> tuple[str, int, int]:
        """-> (base64 PNG, width, height) of the screen, scaled down to at most ``max_width``."""
        img = _grab()
        small = _shrink(img, self.max_width)
        self.scale = img.shape[1] / small.shape[1]
        return base64.b64encode(png(small)).decode("ascii"), small.shape[1], small.shape[0]

    def _at(self, x: float, y: float) -> tuple[int, int]:
        w, h = screen_size()
        sx, sy = round(float(x) * self.scale), round(float(y) * self.scale)
        if not (0 <= sx < w and 0 <= sy < h):
            raise ValueError(f"({x}, {y}) is outside the screenshot")
        return sx, sy

    @staticmethod
    def _send(*inputs: INPUT) -> None:
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    @staticmethod
    def _mouse(flags: int, data: int = 0) -> INPUT:
        return INPUT(type=MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, data, flags, 0, None)))

    @staticmethod
    def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
        return INPUT(type=KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(vk, scan, flags, 0, None)))

    def move(self, x, y) -> None:
        _aware()
        user32.SetCursorPos(*self._at(x, y))

    def click(self, x, y, button: str = "left", clicks: int = 1) -> None:
        down, up = BUTTONS.get(button, BUTTONS["left"])
        self.move(x, y)
        time.sleep(0.03)
        for _ in range(max(1, min(3, int(clicks)))):
            self._send(self._mouse(down), self._mouse(up))
            time.sleep(0.05)

    def drag(self, x1, y1, x2, y2) -> None:
        self.move(x1, y1)
        self._send(self._mouse(LEFTDOWN))
        _aware()
        (ax, ay), (bx, by) = self._at(x1, y1), self._at(x2, y2)
        for k in range(1, 21):                                    # a visible path, so drop targets notice it
            user32.SetCursorPos(round(ax + (bx - ax) * k / 20), round(ay + (by - ay) * k / 20))
            time.sleep(0.01)
        self._send(self._mouse(LEFTUP))

    def scroll(self, x, y, amount: int) -> None:
        """``amount`` notches: positive scrolls up, negative down."""
        self.move(x, y)
        self._send(self._mouse(WHEEL, ctypes.c_uint32(int(amount) * 120).value))

    def type_text(self, text: str) -> None:
        for ch in str(text):
            if ch == "\n":
                self._send(self._key(VK["enter"]), self._key(VK["enter"], flags=KEYUP))
                continue
            for unit in struct.unpack(f"<{len(ch.encode('utf-16-le')) // 2}H", ch.encode("utf-16-le")):
                self._send(self._key(scan=unit, flags=UNICODE), self._key(scan=unit, flags=UNICODE | KEYUP))
            time.sleep(0.004)

    def key(self, combo: str) -> None:
        """A key or chord: "enter", "ctrl+s", "alt+tab", "ctrl+shift+esc"."""
        codes = []
        for part in str(combo).lower().replace(" ", "").split("+"):
            if part in VK:
                codes.append(VK[part])
            elif len(part) == 1 and (part.isalnum()):
                codes.append(ord(part.upper()))
            else:
                raise ValueError(f"unknown key {part!r}")
        self._send(*[self._key(c) for c in codes], *[self._key(c, flags=KEYUP) for c in reversed(codes)])
