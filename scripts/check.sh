#!/usr/bin/env bash
# Single entry point for the published quality gates.
# Runs: ruff format check + ruff lint + mypy --strict + pytest (offline).
# Extra args are forwarded to pytest. Example:
#   ./scripts/check.sh -k schema_tree -v
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$HERE/lint.sh"
"$HERE/../tests/scripts/test.sh" "$@"
