#!/usr/bin/env bash
# Run the exact steps .github/workflows/ci.yml runs, in the same order and the same
# command form.
#
# The command form matters. `python -m pytest` inserts the current directory into
# sys.path; the bare `pytest` console script does not. Verifying with the former while
# CI uses the latter hides import errors that CI then finds -- which is exactly what
# happened, with CI red for 15 consecutive runs while local runs reported green.
#
# Always verify with this script before claiming CI will pass.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${VENV_BIN:-.venv/bin}"

echo "--- ruff check ---"
"$BIN/ruff" check src tests

echo "--- ruff format --check ---"
"$BIN/ruff" format --check src tests

echo "--- mypy ---"
"$BIN/mypy"

echo "--- pytest (bare console script, as CI invokes it) ---"
"$BIN/pytest" --cov=atlas --cov-report=term-missing

echo
echo "All CI steps passed."
