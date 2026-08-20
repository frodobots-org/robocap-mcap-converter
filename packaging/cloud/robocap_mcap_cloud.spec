from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).parents[1]
hiddenimports = (
    collect_submodules("boto3")
    + collect_submodules("botocore")
    + collect_submodules("foxglove_schemas_protobuf")
    + collect_submodules("mcap_protobuf")
    + collect_submodules("google.protobuf")
)
datas = collect_data_files("botocore") + collect_data_files("foxglove_schemas_protobuf")

a = Analysis(
    [str(ROOT / "packaging" / "cloud" / "entrypoint.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6",
        "tkinter",
        "matplotlib",
        "IPython",
        "rerun",
        "pyarrow",
        "uvicorn",
        "fastapi",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="robocap-mcap-cloud",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="robocap-mcap-cloud",
)
