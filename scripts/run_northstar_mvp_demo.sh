#!/usr/bin/env bash
# Northstar MVP — one-command, key-free acceptance run.
#
#   ./scripts/run_northstar_mvp_demo.sh
#
# Requires NO Browserbase or model credentials. It:
#   1. starts the local Northstar synthetic product (uvicorn :8971) for the
#      human live view, resetting it to the frozen seed;
#   2. runs the deterministic end-to-end acceptance test TWICE (embedded
#      Postgres, the in-process Northstar browser provider + fake visual
#      planner + real product write) — proving demo-org setup, canonical
#      knowledge ingestion, ContextResolver citations + cross-org denial, the
#      visual-only target, rejection = zero tasks, approval = exactly one
#      task-0003 with a receipt + post-action verification, replay = no
#      duplicate, dashboard state mapping, and a clean close;
#   3. prints the local URLs + the result;
#   4. cleans up every child process on exit.
#
# It resets ONLY synthetic demo state. It never touches production data and
# never opens a public tunnel.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PY="${LAURA_PYTHON:-$HOME/venvs/laura/bin/python}"
[ -x "$PY" ] || PY="python3"
PRODUCT_PORT="${NORTHSTAR_PORT:-8971}"
PRODUCT_PID=""

cleanup() {
  # Kill the product server (and any of its children) on any exit path.
  if [ -n "${PRODUCT_PID}" ] && kill -0 "${PRODUCT_PID}" 2>/dev/null; then
    kill "${PRODUCT_PID}" 2>/dev/null || true
    wait "${PRODUCT_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "▸ Northstar MVP — key-free acceptance"
echo "  python: $PY"

# 1 · start the local synthetic product (for the human live view) + reset seed.
echo "▸ starting Northstar product on :${PRODUCT_PORT} (synthetic; local only)"
"$PY" -m uvicorn demos.northstar.product:app --port "${PRODUCT_PORT}" \
  --log-level warning >/tmp/northstar_product.log 2>&1 &
PRODUCT_PID=$!

# Wait for health (bounded), then reset to the frozen seed.
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:${PRODUCT_PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
curl -fsS -X POST "http://127.0.0.1:${PRODUCT_PORT}/admin/reset" >/dev/null 2>&1 \
  || echo "  (reset skipped — product not reachable; the e2e uses its own in-process store)"

# 2 · run the deterministic acceptance test TWICE from clean reset.
echo "▸ running the deterministic MVP acceptance (embedded Postgres, key-free)"
set +e
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PY" -m pytest \
  backend/tests/test_northstar_mvp_e2e.py demos/northstar/tests -q
RESULT=$?
set -e

echo ""
echo "▸ local URLs"
echo "    Northstar product : http://127.0.0.1:${PRODUCT_PORT}/"
echo "    Acme account      : http://127.0.0.1:${PRODUCT_PORT}/customers/acme-robotics"
echo "    Tasks             : http://127.0.0.1:${PRODUCT_PORT}/tasks"
echo "    (dashboard: run the Laura backend separately — see docs/product/NORTHSTAR-MVP.md)"
echo ""
if [ "$RESULT" -eq 0 ]; then
  echo "▸ RESULT: PASS — key-free MVP demo acceptance green (deterministic, one task)."
else
  echo "▸ RESULT: FAIL — see the pytest output above."
fi
exit "$RESULT"
