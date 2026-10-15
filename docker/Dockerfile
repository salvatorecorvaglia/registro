# syntax=docker/dockerfile:1.7

# Debian slim rather than Alpine: the build needs apt/useradd, and the wheels
# for the database drivers are manylinux, so Alpine (musl) would force a source
# build of each one. The Python version tracks `requires-python` and mypy.ini.
ARG PYTHON_VERSION=3.13

# ---- Builder ---------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# A pinned uv from its official image, rather than ``curl | sh`` of whatever
# the install script serves today: reproducible, and no script executed from
# the network at build time. Bump deliberately, alongside the CI's uv.
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

# Fail loud if uv.lock is stale rather than silently regenerating it — the
# lock file is committed and the image must build from exactly those pins.
RUN uv sync --frozen --no-dev --all-extras --no-editable

# ---- Runtime --------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REGISTRO_LOG_LEVEL=INFO

RUN useradd -ms /bin/false -u 10001 registro

# Only the environment: the package is installed into it (``--no-editable``),
# so the source tree the builder worked from has no business in the image.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /home/registro
USER registro

# `registro tui` needs a terminal: run it with `docker run -it`.
ENTRYPOINT ["registro"]
CMD ["--help"]
