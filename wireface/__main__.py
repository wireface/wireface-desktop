"""``python -m wireface``: start Wireface.

  --face NAME     run the face called NAME (its own config and files in data\\faces\\NAME), so several
                  faces can run at once; without it, the default face
  --config PATH   run the face configured by this profile.json (its other files go to data\\faces\\<file name>)
  --room NAME     join the room NAME (faces in one room share the microphone and take turns speaking)
  --browser       don't open the desktop shell; print a URL to open in a browser instead
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import sys

from . import config, profile as profiles
from .version import VERSION


def hidden_console():
    """Started without a console (pythonw): make a hidden one for child processes to inherit.

    Claude Code and the commands it runs are console programs; with no console to inherit, Windows
    would pop up a new window for each of them.
    """
    if sys.platform != "win32":
        return
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    if kernel32.GetConsoleWindow():
        return
    if kernel32.AllocConsole():
        user32.ShowWindow(kernel32.GetConsoleWindow(), 0)       # SW_HIDE


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="wireface", description="Wireface: a talking face for your agent")
    ap.add_argument("--face", metavar="NAME", help="the face to run (default: the original single face)")
    ap.add_argument("--config", metavar="PATH", help="a profile.json to run instead of data\\faces\\NAME")
    ap.add_argument("--room", metavar="NAME", help="join this room for this run: faces in one room take turns "
                                                    "and share one microphone")
    ap.add_argument("--browser", action="store_true", help="print a URL instead of opening the desktop shell")
    ap.add_argument("--version", action="version", version=f"Wireface Desktop {VERSION}")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None):
    hidden_console()
    args = parse(sys.argv[1:] if argv is None else argv)
    try:
        profile = profiles.resolve(args.face, args.config)
        profile.acquire_lock()
    except (ValueError, profiles.AlreadyRunning) as e:
        print(f"wireface: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        profile.logs.mkdir(parents=True, exist_ok=True)
        handlers: list[logging.Handler] = [logging.FileHandler(profile.logs / "app.log", encoding="utf-8")]
        if sys.stderr is not None:
            handlers.append(logging.StreamHandler())
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            handlers=handlers)

        from .app import App

        app = App(profile, shell=not args.browser, room=args.room)
        try:
            asyncio.run(app.main())
        except KeyboardInterrupt:
            pass
        except OSError as e:
            logging.getLogger(__name__).error("could not start (is the port taken?): %s", e)
            sys.exit(1)
    finally:
        profile.release_lock()


if __name__ == "__main__":
    main()
