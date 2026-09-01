# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build
COPY requirements.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install --require-hashes -r requirements.lock

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    TZ=Europe/Madrid \
    CONTAINERIZED=1

RUN groupadd --system --gid 10001 albaran \
    && useradd --system --uid 10001 --gid albaran --home-dir /app --shell /usr/sbin/nologin albaran

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=albaran:albaran src ./src
COPY --chown=albaran:albaran ops/entrypoint.sh ops/healthcheck.py ./ops/

RUN chmod 0555 /app/ops/entrypoint.sh /app/ops/healthcheck.py \
    && mkdir -p /app/runtime \
    && chown albaran:albaran /app/runtime

USER 10001:10001

ENTRYPOINT ["/app/ops/entrypoint.sh"]
CMD ["python", "-m", "src.bot"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "/app/ops/healthcheck.py"]
