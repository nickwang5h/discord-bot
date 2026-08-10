FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN python -m pip install --require-hashes -r requirements.lock

RUN groupadd --gid 1000 botuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin botuser \
    && chown 1000:1000 /app

COPY --chown=1000:1000 bot.py config.py settings.json ./
COPY --chown=1000:1000 cogs ./cogs
COPY --chown=1000:1000 core ./core
COPY --chown=1000:1000 scripts ./scripts

USER 1000:1000

CMD ["python", "bot.py"]
