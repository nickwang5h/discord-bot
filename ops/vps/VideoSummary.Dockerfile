# syntax=docker/dockerfile:1.7
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/info-curator/src:/opt/media-transcriber/src \
    INFO_CURATOR_CLI=/usr/local/bin/info-curator \
    MEDIA_TRANSCRIBER_CLI=/usr/local/bin/media-transcriber \
    VIDEO_SUMMARY_WORKER_HOST=0.0.0.0 \
    VIDEO_SUMMARY_WORKER_PORT=8080 \
    VIDEO_SUMMARY_JOB_TIMEOUT_SECONDS=190 \
    NODE_PATH=/opt/media-transcriber/node_modules

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=info_curator /src /opt/info-curator/src
COPY --from=media_transcriber /src /opt/media-transcriber/src
COPY --from=media_transcriber /package.json /package-lock.json /opt/media-transcriber/
COPY core/video_summary_worker.py /app/video_summary_worker.py

RUN cd /opt/media-transcriber && npm ci --omit=dev \
    && groupadd --gid 1000 worker \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin worker \
    && mkdir -p /var/lib/info-curator \
    && chown -R 1000:1000 /var/lib/info-curator /app /opt/media-transcriber \
    && printf '%s\n' '#!/bin/sh' 'exec python -m content_enrichment "$@"' \
       > /usr/local/bin/info-curator \
    && printf '%s\n' '#!/bin/sh' 'exec python -m media_transcriber "$@"' \
       > /usr/local/bin/media-transcriber \
    && chmod 755 /usr/local/bin/info-curator /usr/local/bin/media-transcriber

USER 1000:1000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=5s \
    CMD ["python", "/app/video_summary_worker.py", "--healthcheck"]

CMD ["python", "/app/video_summary_worker.py"]
