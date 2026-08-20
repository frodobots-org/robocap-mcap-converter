"""Single source of truth for RoboCap camera to MCAP topic-prefix naming.

Topics follow ``/{position}-{side}-camera``. Head: eye stereo → ``top-{side}``,
front/side cameras get their own prefixes. Wrist (RoboWrist ``left_down`` /
``right_down``) → ``/wrist-{side}-camera``, consistent with the head naming.
Unknown cameras fall through to the generic ``/camera-{name}`` — CAMERAS is a
closed set in robocap_reviewer, so that path is only a defensive fallback.
"""
from __future__ import annotations

# Stable topic names consumed by Foxglove and downstream robotics tooling.
CAMERA_TOPIC_PREFIX = {
    "left-eye":    "/top-left-camera",
    "right-eye":   "/top-right-camera",
    "left-front":  "/front-left-camera",
    "right-front": "/front-right-camera",
    "left":        "/side-left-camera",
    "right":       "/side-right-camera",
    "left_down":   "/wrist-left-camera",
    "right_down":  "/wrist-right-camera",
}


def topic_prefix_for(camera: str) -> str:
    """Topic prefix for a camera, e.g. ``/wrist-left-camera``.

    Unknown cameras degrade to ``/camera-{name}`` (never raises)."""
    return CAMERA_TOPIC_PREFIX.get(camera, f"/camera-{camera}")
