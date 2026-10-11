"""Finding an agent's real executable.

On Windows the npm installs of these agents leave a ``.cmd`` / ``.ps1`` shim on PATH (which cannot be launched
safely with arguments) and keep the actual ``.exe`` inside their ``node_modules``. This finds that ``.exe``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AgentNotFound(RuntimeError):
    pass


def find_executable(name: str, override: str | None = None, shim_globs: Sequence[str] = (),
                    fallbacks: Sequence[Path] = ()) -> str:
    """The executable for ``name`` (e.g. "codex"): ``override`` if given, a real ``name.exe`` on PATH, the
    ``shim_globs`` (relative to the folder of the npm shim on PATH), then ``fallbacks``. Raises AgentNotFound."""
    if override:
        if not Path(override).is_file():
            raise AgentNotFound(f"{override} does not exist (backend.options.cli_path)")
        return override
    on_path = shutil.which(name + ".exe") if sys.platform == "win32" else shutil.which(name)
    if on_path:
        return on_path
    shim = shutil.which(name)
    if shim:
        base = Path(shim).parent
        for pattern in shim_globs:
            for match in sorted(base.glob(pattern)):
                if match.is_file():
                    return str(match)
        if sys.platform != "win32":
            return shim
    for candidate in fallbacks:
        if candidate.is_file():
            return str(candidate)
    raise AgentNotFound(f"{name} is not installed (or not on PATH). Install it, or set backend.options.cli_path "
                        f"to its executable.")


def kill_tree(pid: int) -> None:
    """End a process and everything it started (agents run commands in child processes)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
