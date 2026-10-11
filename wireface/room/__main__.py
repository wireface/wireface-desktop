"""``python -m wireface.room NAME``: run a room (started on demand by the first face that joins it)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .. import config
from . import hub


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wireface.room")
    ap.add_argument("name")
    ap.add_argument("--lead", default="first", help='"first" (whoever answers first) or an agent\'s name')
    ap.add_argument("--debug", action="store_true", help="allow POST /say?t=TOKEN to speak to the room without a microphone")
    args = ap.parse_args(argv)
    hub.rooms_dir().mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(hub.rooms_dir() / f"{args.name}.log", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    try:
        return asyncio.run(hub.run_room(args.name, args.lead, args.debug))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
