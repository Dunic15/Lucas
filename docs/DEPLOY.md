# Deploy — get a permanent URL (kill the quick-tunnel)

The `cloudflared` quick tunnel gets a **new random URL every restart**, which
breaks `PUBLIC_BASE_URL` and any webhooks. For real use, pick ONE permanent URL.

## Option A — Deploy the backend (recommended)
A container host gives you a stable `https://…` URL and runs 24/7.

**Render (free tier, easiest):**
1. Push this repo to GitHub (done).
2. Render → New → Blueprint → pick this repo (it reads `render.yaml`).
3. Set the secret env vars (ANTHROPIC_API_KEY, RECALL_API_KEY, ANAM_API_KEY, …).
4. After first deploy, copy the `https://<name>.onrender.com` URL and set it as
   `PUBLIC_BASE_URL`, then redeploy.

**Fly.io / Railway / Google Cloud Run / a DO or AWS VM** all work with the
`Dockerfile`. Use your **DigitalOcean $10k** or **AWS $10k** startup credits.

Then in Recall's dashboard, point the **transcript + status webhooks** and (if
using it) the **calendar webhook** at:
- `https://YOUR_URL/webhooks/recall`
- `https://YOUR_URL/webhooks/recall-calendar`

## Option B — Cloudflare *named* tunnel (keep running locally, stable URL)
Uses your **Cloudflare $100k credits** + a domain. One-time:
```bash
cloudflared tunnel login                      # authorize (opens browser)
cloudflared tunnel create laura               # creates a named tunnel
cloudflared tunnel route dns laura laura.yourdomain.com
cloudflared tunnel run --url http://localhost:8000 laura
```
Set `PUBLIC_BASE_URL=https://laura.yourdomain.com`. This URL never changes.

## Local, non-permanent (what `./scripts/serve.sh` does)
Fine for quick demos only — the URL changes each run.
