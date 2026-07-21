"""Test a Recall API key instantly; no server restart needed.

Usage:
    python backend/scripts/recall_check.py                 # tests RECALL_API_KEY from .env
    python backend/scripts/recall_check.py <paste-key-here> # tests a key you paste

It tries every Recall region and tells you which one the key belongs to (and the
exact RECALL_API_BASE to use), or explains why the value is not an API key.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402

REGIONS = ["us-east-1", "us-west-2", "eu-central-1", "ap-northeast-1"]


def main() -> None:
    key = (sys.argv[1] if len(sys.argv) > 1 else settings.recall_api_key).strip()

    if not key:
        print("No key. Paste one:  python backend/scripts/recall_check.py <key>")
        return
    if key.startswith("whsec_"):
        print("✗ This is a WEBHOOK/Workspace secret (whsec_…), NOT an API key.")
        print("  The API key is a different value on the dashboard; it does not")
        print("  start with whsec_. Create one at Developers → API Keys.")
        return

    print(f"Testing key '{key[:6]}…{key[-4:]}' across all regions…\n")
    found = False
    for region in REGIONS:
        base = f"https://{region}.recall.ai"
        try:
            r = httpx.get(
                f"{base}/api/v1/bot/",
                headers={"Authorization": key},  # Recall accepts the raw key
                timeout=15.0,
            )
        except Exception as e:
            print(f"  {region:15} error: {type(e).__name__}")
            continue
        if 200 <= r.status_code < 300:
            print(f"  {region:15} ✓ VALID")
            print(f"\n✅ Use this in .env:  RECALL_API_BASE={base}")
            found = True
            break
        print(f"  {region:15} HTTP {r.status_code}")

    if not found:
        print("\n✗ The key was rejected in every region. Double-check you copied the")
        print("  API Key (Developers → API Keys), not the Workspace/webhook secret.")


if __name__ == "__main__":
    main()
