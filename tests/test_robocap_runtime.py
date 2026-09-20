from __future__ import annotations

import os
import sys
from pathlib import Path

from robocap_to_mcap.runtime import configure_bundled_tools


def test_configure_bundled_tools_finds_macos_app_frameworks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "RoboCapToMCAP.app" / "Contents" / "MacOS" / "RoboCapToMCAP"
    executable.parent.mkdir(parents=True)
    executable.touch()
    tool_dir = executable.parent.parent / "Frameworks" / "ffmpeg" / "bin"
    tool_dir.mkdir(parents=True)
    (tool_dir / "ffmpeg").touch()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setenv("PATH", "/usr/bin")

    configure_bundled_tools()

    assert os.environ["PATH"].split(os.pathsep)[0] == str(tool_dir)
