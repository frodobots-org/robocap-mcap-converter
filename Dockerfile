FROM ghcr.io/astral-sh/uv:0.8.17 AS uv

FROM python:3.12-slim-bookworm

ARG VERSION=0.2.1
ARG REVISION=unknown

LABEL org.opencontainers.image.title="RoboCap MCAP Converter" \
      org.opencontainers.image.description="Open-source raw RoboCap session to MCAP converter" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.vendor="FrodoBots" \
      org.opencontainers.image.source="https://github.com/frodobots-org/robocap-mcap-converter"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 converter \
    && useradd --uid 10001 --gid converter --create-home --shell /usr/sbin/nologin converter \
    && mkdir -p /app /work/input /work/output /work/tmp \
    && chown -R converter:converter /app /work

COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:${PATH}" \
    TMPDIR=/work/tmp \
    PYTHONUNBUFFERED=1

USER converter
WORKDIR /work
ENTRYPOINT ["robocap-mcap-cloud"]
CMD ["--help"]
