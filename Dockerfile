# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG DENO_VERSION=v2.8.1

FROM ${PYTHON_IMAGE} AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project --frozen

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen

FROM ${PYTHON_IMAGE} AS runtime

ARG DENO_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libjemalloc2 \
        ffmpeg \
        media-types \
        curl unzip ca-certificates \
    && export DENO_INSTALL=/opt/deno \
    && curl -fsSL https://deno.land/install.sh | sh -s "${DENO_VERSION}" \
    && test -x /opt/deno/bin/deno \
    && rm -rf /var/lib/apt/lists/*

ENV DENO_INSTALL=/opt/deno \
    PATH="/app/.venv/bin:/opt/deno/bin:$PATH" \
    LD_PRELOAD=libjemalloc.so.2 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_PATH=/app/data \
    DOWNLOAD_DIR=/app/downloads

WORKDIR /app
COPY --from=build /app /app

RUN mkdir -p /app/data /app/downloads /app/logs \
    && python -c "import parsehub, tgcrypto; print('ParseHub/TgCrypto import OK')" \
    && ffmpeg -version >/dev/null \
    && deno --version >/dev/null

STOPSIGNAL SIGTERM

CMD ["python", "-u", "bot.py"]
