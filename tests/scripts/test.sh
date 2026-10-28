#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$HERE"

MARKER_EXPR="${REGISTRO_TEST_MARKERS:-not integration}"
uv run --frozen pytest --durations=25 --junitxml=junit.xml -m "$MARKER_EXPR" --cov=src --cov-report=term-missing "$@"
