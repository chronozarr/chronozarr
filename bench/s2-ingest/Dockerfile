# Linux image for bench.py runs under CPU, memory and file-descriptor limits.
# Build from the repo root (Dockerfile.dockerignore keeps the context to the package files):
#   docker build -f bench/s2-ingest/Dockerfile -t chronozarr-s2-bench .
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /usr/local/bin/uv

# git: bench.py reads the baseline pipeline from a git ref in the mounted repo.
# build-essential: uv.lock pins numcodecs 0.14.1, which has no linux/aarch64 wheel, so it is
# built from the locked sdist.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/project
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --extra ingest --extra dev
