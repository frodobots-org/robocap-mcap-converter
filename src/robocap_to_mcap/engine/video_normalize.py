"""Normalize H.264 video for Foxglove (Chromium WebCodecs) playback.

Two pathologies are corrected:

1. **B-frames** — WebCodecs requires monotonically-increasing chunk DTS;
   B-frame PTS is out-of-order, causing stutter/error.  Fix: full
   re-encode with `-bf 0`.

2. **Non-standard SPS VUI timing** — Some blur encoders write
   `num_units_in_tick`/`time_scale` as odd clock-divider-derived
   rationals (e.g. 34592/1971194 ≈ 28.49 Hz, or 27144/1627570 ≈
   29.98 Hz).  These are spec-legal but outside the range Chromium
   WebCodecs has tested; WebCodecs configures itself off the bad SPS,
   then fails mid-frame in the CABAC bin parser with the misleading
   "bitstream exhausted" message.  Fix: stream-copy with ffmpeg's
   `h264_metadata` BSF to remove the VUI timing fields; WebCodecs then
   falls back to the container timestamps, which are always correct.

Both produce a `.normalized.mp4` sibling file that is cached on disk.
See `mcap_writer.build_video_message` for the full WebCodecs DTS
constraint."""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

import av

log = logging.getLogger(__name__)

_NORMALIZED_SUFFIX = ".normalized.mp4"

# h264_metadata BSF that rewrites the SPS VUI timing to 30 fps
# (time_scale=60, num_units_in_tick=1).  `tick_rate` is the FFmpeg
# option name for time_scale/num_units_in_tick; setting it to 60/1
# gives fps = tick_rate/2 = 30, which is in WebCodecs' tested range.
# We do NOT set fixed_frame_rate_flag here — doing so triggers an
# HRD consistency check in FFmpeg 8.x that fails on some profiles.
# Foxglove uses container timestamps for actual playback pacing, so
# the declared fps in the SPS only needs to be a sane value.
_SPS_FIX_BSF = "h264_metadata=tick_rate=60/1"

# WebCodecs-safe frame rates. Any SPS-declared fps outside this set
# triggers the stream-copy SPS fix.
_STANDARD_FPS: frozenset[Fraction] = frozenset({
    Fraction(24000, 1001),
    Fraction(24),
    Fraction(25),
    Fraction(30000, 1001),
    Fraction(30),
    Fraction(48000, 1001),
    Fraction(48),
    Fraction(50),
    Fraction(60000, 1001),
    Fraction(60),
})

# `ultrafast` + `crf 18` is the right operating point: the input is
# already compressed, so spending CPU on encoder analysis gains little;
# crf 18 stays visually close to the input. `-loglevel error`
# bounds stderr so a long input doesn't balloon the captured buffer.
# `-bsf:v` applies after encoding to strip any residual non-standard
# VUI timing the re-encoder might inherit from the input.
_FFMPEG_TRANSCODE_FLAGS = (
    "-loglevel", "error",
    "-bf", "0",
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-crf", "18",
    "-g", "30",
    "-keyint_min", "30",
    "-force_key_frames", "expr:gte(t,n_forced*1.0)",
    "-pix_fmt", "yuv420p",
    "-bsf:v", _SPS_FIX_BSF,
)


class NormalizeError(RuntimeError):
    """Raised when ffmpeg is unavailable or the transcode/patch fails."""


def has_b_frames(path: Path) -> bool:
    """True if the stream contains B-frames (PyAV `codec_context.has_b_frames`
    reports the decoder's max reorder depth — 0 for `-bf 0` encodes)."""
    with av.open(str(path)) as container:
        return container.streams.video[0].codec_context.has_b_frames > 0


def has_bad_sps_timing(path: Path) -> bool:
    """True if the H.264 SPS VUI declares a frame rate not in
    `_STANDARD_FPS` (spec-legal but rejected by Chromium WebCodecs).

    Returns False when no VUI timing info is present (the decoder
    falls back to container timestamps, which is always fine)."""
    with av.open(str(path)) as container:
        fps = container.streams.video[0].codec_context.framerate
    if fps is None or fps == 0:
        return False
    return Fraction(fps.numerator, fps.denominator) not in _STANDARD_FPS


def normalize_for_mcap(path: Path, *, cache_dir: Path | None = None) -> Path:
    """Return a WebCodecs-compatible version of `path`.

    Passthrough when no pathology is detected.  Otherwise produces a
    cached `.normalized.mp4` sibling via ffmpeg.  Concurrency-safe:
    writes to a unique `.part` tempfile then atomically renames it."""
    out = (
        cache_dir / f"{path.name}{_NORMALIZED_SUFFIX}"
        if cache_dir is not None
        else path.with_suffix(path.suffix + _NORMALIZED_SUFFIX)
    )
    # Check cache before probing — at full batch scale (~21.6M segments)
    # skipping a 10ms `av.open` per cache hit saves ~60 CPU-hours.
    # The atomic-rename below ensures `out` is never zero-byte, so
    # `is_file()` alone is sufficient.
    if out.is_file():
        return out

    b_frames = has_b_frames(path)
    bad_sps = has_bad_sps_timing(path)

    if not b_frames and not bad_sps:
        return path

    if shutil.which("ffmpeg") is None:
        raise NormalizeError("ffmpeg not on PATH; required to normalize video for MCAP")

    log.info(
        "normalizing for MCAP: %s (b_frames=%s bad_sps=%s) -> %s",
        path.name, b_frames, bad_sps, out.name,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=out.parent, prefix=out.name + ".", suffix=".part", delete=False,
    ) as tmp_handle:
        tmp = Path(tmp_handle.name)

    if b_frames:
        # Full re-encode strips B-frames; -bsf:v also clears VUI timing.
        cmd = ["ffmpeg", "-y", "-i", str(path),
               *_FFMPEG_TRANSCODE_FLAGS, "-f", "mp4", str(tmp)]
        action = "transcoding"
    else:
        # SPS-only: stream-copy (no decode/encode) + h264_metadata BSF.
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
               "-c:v", "copy", "-bsf:v", _SPS_FIX_BSF, "-f", "mp4", str(tmp)]
        action = "patching SPS timing in"

    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise NormalizeError(
            f"ffmpeg exit {proc.returncode} {action} {path.name}: "
            f"{proc.stderr.decode('utf-8', errors='replace')[-500:]}"
        )
    tmp.replace(out)
    return out
