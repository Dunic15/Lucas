#!/usr/bin/env bash
# Launch the backend + a public tunnel under pm2 so they survive terminal/session
# exit (until reboot). Re-run any time to get a fresh tunnel URL wired into .env.
#
#   ./scripts/serve.sh          # start / restart everything
#   pm2 status                  # see the processes
#   pm2 logs laura-api          # tail server logs
#   pm2 stop laura-api laura-tunnel   # stop
#
# NOTE: free cloudflared "quick tunnels" get a NEW random URL each restart, so
# this script rewrites PUBLIC_BASE_URL in .env every run. For a permanent URL,
# use a Cloudflare *named* tunnel (needs a domain) or deploy the backend.
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PORT="${PORT:-8000}"

command -v cloudflared >/dev/null || { echo "cloudflared not installed (brew install cloudflared)"; exit 1; }
[ -x ".venv/bin/uvicorn" ] || { echo "no .venv; run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }

echo "→ (re)starting tunnel…"
pm2 delete laura-tunnel laura-api >/dev/null 2>&1 || true
: > "$HOME/.pm2/logs/laura-tunnel-out.log" 2>/dev/null || true
: > "$HOME/.pm2/logs/laura-tunnel-error.log" 2>/dev/null || true
pm2 start cloudflared --name laura-tunnel -- tunnel --url "http://localhost:$PORT" >/dev/null

echo "→ waiting for the tunnel URL…"
URL=""
for i in $(seq 1 30); do
  # cloudflared prints the quick-tunnel URL to stderr → pm2 error log
  URL=$(grep -hoE 'https://[a-z0-9-]+\.trycloudflare\.com' \
        "$HOME/.pm2/logs/laura-tunnel-error.log" \
        "$HOME/.pm2/logs/laura-tunnel-out.log" 2>/dev/null | head -1)
  [ -n "$URL" ] && break; sleep 1
done
[ -n "$URL" ] || { echo "could not get tunnel URL; check: pm2 logs laura-tunnel"; exit 1; }

echo "→ writing PUBLIC_BASE_URL=$URL into .env"
python3 - "$URL" <<'PY'
import sys, re, pathlib
url = sys.argv[1]; p = pathlib.Path(".env")
t = p.read_text() if p.exists() else ""
if re.search(r'^PUBLIC_BASE_URL=', t, re.M):
    t = re.sub(r'^PUBLIC_BASE_URL=.*$', f'PUBLIC_BASE_URL={url}', t, flags=re.M)
else:
    t += f'\nPUBLIC_BASE_URL={url}\n'
p.write_text(t)
PY

echo "→ starting API…"
pm2 start "$ROOT/.venv/bin/uvicorn" --name laura-api --interpreter none -- \
  backend.app.main:app --host 127.0.0.1 --port "$PORT" >/dev/null
pm2 save >/dev/null 2>&1 || true

sleep 2
echo
echo "✅ up. Public URL: $URL"
echo "   Local:  http://127.0.0.1:$PORT/       (demo)   /live  (avatar)"
echo "   Status: curl -s http://127.0.0.1:$PORT/recall/status?check_auth=true"
