"""MP4 → H.264/H.265 packet stream in Annex B byte format.

CRITICAL: foxglove.CompressedVideo requires Annex B bitstream (NAL units
separated by 0x00000001 start codes, with SPS/PPS prepended to every IDR).
PyAV's container.demux() returns AVCC-format packets (length-prefixed,
SPS/PPS in extradata), so we MUST pipe through the h264_mp4toannexb (or
hevc_mp4toannexb) bitstream filter.

Verified by Foxglove Studio decode error if the filter is omitted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import av
import av.bitstream

log = logging.getLogger(__name__)

VALID_CODECS = ("h264", "hevc")


@dataclass(frozen=True)
class VideoPacket:
    pts_seconds: float
    codec: str            # "h264" or "hevc"
    data: bytes           # Annex B byte stream
    is_keyframe: bool = False
    dts_seconds: float | None = None

    @property
    def effective_dts_seconds(self) -> float:
        """DTS when known, otherwise PTS — always defined, no None-check
        at call sites. Consumers route this into MCAP log_time; see
        `mcap_writer.build_video_message` for why PTS isn't usable."""
        return self.pts_seconds if self.dts_seconds is None else self.dts_seconds


@dataclass(frozen=True)
class VideoMeta:
    codec: str
    width: int
    height: int


@dataclass(frozen=True)
class TrimResult:
    packets: list[VideoPacket]
    episode_start_seconds: float        # the episode boundary the caller asked for
    actual_first_packet_seconds: float  # the IDR we actually started from
    preroll_seconds: float              # actual_first - episode_start; >= 0


def camera_trim_window(
    *,
    episode_start_seconds: float,
    episode_end_seconds: float,
    primary_clock_us: int,
    cam_clock_us: int,
) -> tuple[float, float]:
    """Per-camera trim window in that camera's own mp4-PTS coordinates.

    The 6 robocap cameras split across two SoC clock domains, so a single
    episode boundary lands at a different mp4-PTS on each camera. We shift the
    primary camera's PTS bounds by this camera's device-clock offset so all
    cameras trim to the SAME wall-clock interval; without it, non-genlocked
    cameras land on different IDR keyframes for the same episode.

    Shared by both exports (MCAP packet trim and standalone-mp4 trim) so the
    two can never disagree on where an episode starts for a given camera.

    Returns ``(start_seconds, end_seconds)``. ``start`` may be negative when a
    camera's clock leads the primary's by more than the episode start offset;
    callers that seek a container (ffmpeg ``-ss``) should clamp to >= 0.
    """
    offset_seconds = (primary_clock_us - cam_clock_us) / 1_000_000.0
    return episode_start_seconds + offset_seconds, episode_end_seconds + offset_seconds


def probe(path: Path) -> VideoMeta:
    container = av.open(str(path))
    try:
        s = container.streams.video[0]
        return VideoMeta(codec=s.codec_context.name, width=s.width, height=s.height)
    finally:
        container.close()


def iter_packets(path: Path) -> Iterator[VideoPacket]:
    """Yield Annex-B-formatted packets in DECODE order. Sanity-checks the
    first packet.

    PyAV's `container.demux()` returns packets in file (decode) order. For
    H.264/H.265 streams without B-frames decode order == PTS order, so the
    distinction is invisible. With B-frames they diverge — DTS is exposed
    on `VideoPacket.dts_seconds` so downstream consumers can route it into
    MCAP log_time (see `VideoPacket` docstring)."""
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        codec = stream.codec_context.name
        if codec not in VALID_CODECS:
            raise RuntimeError(f"unexpected codec: {codec}")

        bsf_name = "h264_mp4toannexb" if codec == "h264" else "hevc_mp4toannexb"
        bsf = av.bitstream.BitStreamFilterContext(bsf_name, stream)
        time_base = stream.time_base

        first_emitted = False
        def emit(packet) -> Iterator[VideoPacket]:
            nonlocal first_emitted
            if packet.pts is None:
                return
            data = bytes(packet)
            if not first_emitted:
                # Annex B start codes must be 0x000001 or 0x00000001.
                if not (data.startswith(b"\x00\x00\x00\x01") or data.startswith(b"\x00\x00\x01")):
                    raise RuntimeError(
                        "First video packet is not Annex B (no start code prefix). "
                        "Bitstream filter mis-applied?"
                    )
                first_emitted = True
            yield VideoPacket(
                pts_seconds=float(packet.pts * time_base),
                codec=codec,
                data=data,
                is_keyframe=bool(packet.is_keyframe),
                dts_seconds=(
                    float(packet.dts * time_base) if packet.dts is not None
                    else None
                ),
            )

        for raw in container.demux(stream):
            if raw.pts is None:
                continue
            for filtered in bsf.filter(raw):
                yield from emit(filtered)
        for filtered in bsf.filter(None):  # EOF flush
            yield from emit(filtered)
    finally:
        container.close()


def trim_to_range(path: Path, start_seconds: float, end_seconds: float) -> TrimResult:
    """
    Return only the H.264/H.265 packets needed to play the [start, end] window
    of the source MP4. Includes pre-roll back to the most recent IDR before
    `start_seconds`, since H.264 cannot decode mid-GOP.

    Streams the demux once, buffering packets from the most recent IDR until we
    cross `start_seconds`. From that point on, packets pass through directly.
    Stops emitting once a packet's PTS exceeds `end_seconds`.
    """
    if end_seconds <= start_seconds:
        raise ValueError(f"end_seconds ({end_seconds}) must be > start_seconds ({start_seconds})")

    gop_buffer: list[VideoPacket] = []   # packets since the latest IDR, while we're still pre-start
    out: list[VideoPacket] = []
    started = False
    actual_first: float | None = None

    for pkt in iter_packets(path):
        if not started:
            if pkt.is_keyframe:
                gop_buffer = [pkt]
            else:
                gop_buffer.append(pkt)
            if pkt.pts_seconds >= start_seconds:
                # Flush the GOP buffer (which begins at the most recent IDR) and switch to passthrough.
                if not gop_buffer[0].is_keyframe:
                    # Should never happen: a valid MP4's first packet is always an IDR,
                    # and gop_buffer is reset on every keyframe we see. Fail loud rather
                    # than emit undecodable Annex B bytes that would silently fail at
                    # the consumer.
                    raise RuntimeError(
                        f"trim_to_range: first GOP packet at t={gop_buffer[0].pts_seconds:.3f}s "
                        f"is not a keyframe; output would be undecodable. "
                        f"Source: {path.name}"
                    )
                out.extend(gop_buffer)
                actual_first = gop_buffer[0].pts_seconds
                started = True
        else:
            if pkt.pts_seconds > end_seconds:
                break
            out.append(pkt)

    if not started:
        # Episode is entirely past the video (or no IDR found before start). Caller
        # should treat this as a soft failure.
        raise RuntimeError(
            f"no IDR + content found for range [{start_seconds:.3f}, {end_seconds:.3f}] "
            f"in {path.name}"
        )

    preroll = max(0.0, start_seconds - (actual_first or start_seconds))
    log.info(
        "trim %s: requested [%.3f..%.3f]s, kept %d packets from t=%.3fs (preroll=%.3fs)",
        path.name, start_seconds, end_seconds, len(out), actual_first, preroll,
    )
    return TrimResult(
        packets=out,
        episode_start_seconds=start_seconds,
        actual_first_packet_seconds=actual_first,
        preroll_seconds=preroll,
    )
