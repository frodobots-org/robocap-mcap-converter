$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to build. Install it from https://docs.astral.sh/uv/."
}

& uv python install 3.12

$FfmpegDir = if ($env:ROBOCAP_FFMPEG_DIR) {
    $env:ROBOCAP_FFMPEG_DIR
} else {
    Join-Path $Root "vendor\ffmpeg\bin"
}
foreach ($Tool in @("ffmpeg.exe", "ffprobe.exe")) {
    if (-not (Test-Path (Join-Path $FfmpegDir $Tool))) {
        throw "Missing $Tool in $FfmpegDir. Supply the approved Windows x64 FFmpeg bundle."
    }
}
$env:ROBOCAP_FFMPEG_DIR = $FfmpegDir
$EncoderList = & (Join-Path $FfmpegDir "ffmpeg.exe") -hide_banner -loglevel quiet -encoders
if ($LASTEXITCODE -ne 0 -or ($EncoderList -join "`n") -notmatch "libx264") {
    throw "The approved FFmpeg bundle must include the libx264 encoder used by video repair."
}

& uv sync --python 3.12 --extra desktop --extra build
& uv run --python 3.12 pytest -q `
    tests/test_robocap_cloud_cli.py `
    tests/test_robocap_container_cli.py `
    tests/test_robocap_desktop_gui.py `
    tests/test_robocap_desktop_scanner.py `
    tests/test_robocap_desktop_validator.py `
    tests/test_robocap_desktop_conversion.py
& uv run --python 3.12 pyinstaller --noconfirm --clean packaging/windows/robocap_to_mcap.spec

$Iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    throw "Inno Setup 6 is required to build the installer."
}
& $Iscc packaging/windows/installer.iss

$Installer = Get-ChildItem dist/installer/*.exe | Select-Object -First 1
Get-FileHash $Installer.FullName -Algorithm SHA256 | Format-List
Write-Host "Built $($Installer.FullName)"
