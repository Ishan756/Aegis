# syntax=docker/dockerfile:1

# ---- build ------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# uv resolves and installs far faster than pip.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# NOTE: app/ is copied before installing because the build backend (hatchling)
# needs the package to resolve. Once a lock file is committed this can split into
# a dependency-only layer so code edits do not reinstall dependencies.
COPY pyproject.toml README.md ./
COPY app ./app
RUN uv pip install --system --no-cache .

# ---- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Run unprivileged; the app never needs to write to its own source tree.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin aegis

COPY --from=builder /usr/local /usr/local
COPY app ./app

USER aegis

EXPOSE 8000

# Uses the unversioned /health alias so the probe does not depend on the API
# version prefix.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]