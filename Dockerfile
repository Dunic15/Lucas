# Production image for the Laura backend (App Runner image mode / any container host).
#
# Parity notes vs the source-mode App Runner build this replaces:
# - Python 3.11 (the managed runtime prod ran on — keeps wheels/behaviour identical).
# - Boots through scripts/start-with-litestream.sh (org-memory durability +
#   the restore integrity guard), NOT bare uvicorn. The litestream binary is
#   BAKED IN so boot never depends on a GitHub download.
# - Copies scripts/ + etc/ (boot wrapper + litestream config) and gpu/assets
#   (avatar portraits served by /laura-reference.jpg).
# Config still comes entirely from environment variables.

FROM python:3.11-slim

# litestream: pin the same version the boot wrapper pins; bake the binary in.
ADD https://github.com/benbjohnson/litestream/releases/download/v0.5.14/litestream-0.5.14-linux-x86_64.tar.gz /tmp/litestream.tar.gz
RUN tar -xzf /tmp/litestream.tar.gz -C /usr/local/bin litestream \
    && rm /tmp/litestream.tar.gz && chmod +x /usr/local/bin/litestream

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY avatars/ avatars/
COPY scripts/ scripts/
COPY etc/ etc/
COPY gpu/assets/ gpu/assets/

ENV HOST=0.0.0.0 PORT=8000 PYTHON=python3
EXPOSE 8000

CMD ["bash", "scripts/start-with-litestream.sh"]
