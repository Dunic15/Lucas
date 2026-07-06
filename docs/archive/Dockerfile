# Archived: this document may be stale and is kept only for reference.
# Deploy the backend anywhere that runs containers (Render, Fly, Railway, Cloud Run,
# a DigitalOcean/AWS VM). Gives you a permanent HTTPS URL — no more cloudflared.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY avatars/ avatars/

# Config comes from environment variables (set them in your host's dashboard).
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
