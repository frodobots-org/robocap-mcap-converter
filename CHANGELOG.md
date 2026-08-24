# Changelog

## 0.3.0 - 2026-08-24

- Added bulk conversion to the Windows app.
- Added recursive discovery from a parent folder and multiple-folder drops.
- Added session-aware progress, output rows, and aggregate completion results.
- Isolated validation and conversion failures so later sessions continue.
- Preserved per-session output folders and unique output filenames.

## 0.2.1 - 2026-08-20

- Added the EGL runtime dependency to headless GUI CI verification.

## 0.2.0 - 2026-08-20

- Initial open-source release.
- Added local, Windows, Docker, and S3 conversion interfaces.
- Added six-camera and optional wrist-camera discovery.
- Added MP4/IMU validation, timing checks, viewer-safe normalization, and MCAP QA.
- Added unique device/session/segment output filenames.
