from __future__ import annotations

import os
import sys
from pathlib import Path


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def configure_bundled_tools() -> None:
    """Put packaged ffmpeg/ffprobe ahead of the machine PATH."""
    roots = [
        application_root() / "ffmpeg" / "bin",
        application_root() / "_internal" / "ffmpeg" / "bin",
    ]
    for root in roots:
        if (root / "ffmpeg.exe").exists() or (root / "ffmpeg").exists():
            os.environ["PATH"] = str(root) + os.pathsep + os.environ.get("PATH", "")
            return
