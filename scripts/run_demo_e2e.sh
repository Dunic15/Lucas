#!/usr/bin/env bash
# Scripted key-free demo — one-command, CI-guarded end-to-end acceptance.
#
#   ./scripts/run_demo_e2e.sh
#
# Requires NO model or embedding credentials. It runs the deterministic e2e test
# that walks the whole value loop through the REAL demo endpoints, key-free
# (BRAIN_PROVIDER=stub → offline extractive brain; EMBEDDING_PROVIDER=hash):
#
#     GET  /demo/sample        → the shipped meeting transcript
#     POST /demo/ask           → a grounded answer that quotes a company doc + cites it
#     POST /demo/post_meeting  → summary + actions + decisions + risks + readiness
#
# Every step that would break the live YC demo breaks a test here instead.
# The scripted presenter flow this guards is docs/demo/RUNBOOK.md.
#
# It touches no production data, opens no network connection, and needs no keys.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${LAURA_PYTHON:-$HOME/venvs/laura/bin/python}"
[ -x "$PY" ] || PY="python3"

# Belt-and-suspenders: the test's conftest already pins these code defaults, but
# make the key-free contract explicit for anyone reading the command.
export BRAIN_PROVIDER="${BRAIN_PROVIDER:-stub}"
export EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-hash}"

echo "▸ Scripted demo — key-free end-to-end acceptance"
echo "  python: $PY"
echo "  BRAIN_PROVIDER=$BRAIN_PROVIDER  EMBEDDING_PROVIDER=$EMBEDDING_PROVIDER"
echo "▸ running the deterministic demo e2e (real endpoints, no keys, no network)"

set +e
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PY" -m pytest \
  backend/tests/test_demo_e2e.py -q
RESULT=$?
set -e

echo ""
if [ "$RESULT" -eq 0 ]; then
  echo "▸ RESULT: PASS — scripted key-free demo loop is green (deterministic)."
  echo "  Presenter script: docs/demo/RUNBOOK.md"
else
  echo "▸ RESULT: FAIL — see the pytest output above."
fi
exit "$RESULT"
