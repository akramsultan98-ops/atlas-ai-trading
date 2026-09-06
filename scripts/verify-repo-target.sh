#!/usr/bin/env bash
# Repository target guard.
#
# This session's shell resets its working directory to the egx-sentinel checkout after
# every command, so a command that forgets to cd would operate on the wrong repository.
# ATLAS and EGX Sentinel are separate projects and must never cross.
#
# Run before any commit or push:  ./scripts/verify-repo-target.sh
set -euo pipefail

EXPECTED_REPO="akramsultan98-ops/atlas-ai-trading"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

origin="$(git remote get-url origin 2>/dev/null || true)"
if [[ -z "$origin" ]]; then
    echo "FAIL: no origin remote configured in $REPO_ROOT" >&2
    exit 1
fi

if [[ "$origin" != *"$EXPECTED_REPO"* ]]; then
    echo "FAIL: origin points at '$origin'" >&2
    echo "      expected '$EXPECTED_REPO'" >&2
    echo "      REFUSING to proceed - this is the wrong repository." >&2
    exit 1
fi

if [[ "$origin" == *"egx-sentinel"* ]]; then
    echo "FAIL: origin points at egx-sentinel, a separate project. REFUSING." >&2
    exit 1
fi

echo "OK: $REPO_ROOT -> $origin (branch $(git branch --show-current))"
