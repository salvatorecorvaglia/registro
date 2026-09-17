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
 && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -s /root/.local/bin/uv /usr/local/bin/uv

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

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

WORKDIR /app
USER registro

# `registro tui` needs a terminal: run it with `docker run -it`.
ENTRYPOINT ["registro"]
CMD ["--help"]
