from __future__ import annotations

import os
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).parents[1]
FFMPEG_DIR = Path(os.environ.get("ROBOCAP_FFMPEG_DIR", ROOT / "vendor" / "ffmpeg" / "bin"))
FFMPEG = FFMPEG_DIR / "ffmpeg"
FFPROBE = FFMPEG_DIR / "ffprobe"
if not FFMPEG.is_file() or not FFPROBE.is_file():
    raise SystemExit(
        "Missing bundled ffmpeg/ffprobe. Set ROBOCAP_FFMPEG_DIR to their directory."
    )

with (ROOT / "pyproject.toml").open("rb") as handle:
    VERSION = tomllib.load(handle)["project"]["version"]

hiddenimports = (
    collect_submodules("foxglove_schemas_protobuf")
    + collect_submodules("mcap_protobuf")
    + collect_submodules("google.protobuf")
)
datas = collect_data_files("foxglove_schemas_protobuf")
binaries = [
    (str(FFMPEG), "ffmpeg/bin"),
    (str(FFPROBE), "ffmpeg/bin"),
]

a = Analysis(
    [str(ROOT / "packaging" / "macos" / "entrypoint.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "rerun", "pyarrow"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RoboCapToMCAP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="RoboCapToMCAP",
)
app = BUNDLE(
    coll,
    name="RoboCapToMCAP.app",
    bundle_identifier="ai.bitrobot.robocap-to-mcap",
    info_plist={
        "CFBundleDisplayName": "RoboCap to MCAP",
        "CFBundleName": "RoboCap to MCAP",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Copyright BitRobot",
    },
)
