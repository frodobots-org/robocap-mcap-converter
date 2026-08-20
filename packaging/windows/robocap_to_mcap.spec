from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).parents[1]
FFMPEG_DIR = Path(os.environ.get("ROBOCAP_FFMPEG_DIR", ROOT / "vendor" / "ffmpeg" / "bin"))
FFMPEG = FFMPEG_DIR / "ffmpeg.exe"
FFPROBE = FFMPEG_DIR / "ffprobe.exe"
FFMPEG_LICENSE = FFMPEG_DIR.parent / "LICENSE.txt"
if not FFMPEG.is_file() or not FFPROBE.is_file():
    raise SystemExit(
        "Missing bundled ffmpeg.exe/ffprobe.exe. Set ROBOCAP_FFMPEG_DIR to their directory."
    )

hiddenimports = (
    collect_submodules("foxglove_schemas_protobuf")
    + collect_submodules("mcap_protobuf")
    + collect_submodules("google.protobuf")
)
datas = collect_data_files("foxglove_schemas_protobuf")
if FFMPEG_LICENSE.is_file():
    datas.append((str(FFMPEG_LICENSE), "licenses/ffmpeg"))
binaries = [
    (str(FFMPEG), "ffmpeg/bin"),
    (str(FFPROBE), "ffmpeg/bin"),
]

a = Analysis(
    [str(ROOT / "packaging" / "windows" / "entrypoint.py")],
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
    manifest=str(ROOT / "packaging" / "windows" / "app.manifest"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="RoboCapToMCAP",
)
