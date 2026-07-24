"""Create or update the "Cedric Meeting Pilot" ElevenLabs Agent from repo config.

The agent's entire configuration lives HERE (persona from avatars/cedric/
avatar.yaml + the pilot conversation rules below) so it is code-reviewed and
reproducible — never hand-edited in the ElevenLabs dashboard. Re-running is
idempotent: an existing agent with the same name is updated in place.

Usage:
    ELEVENLABS_API_KEY=... python3 backend/scripts/create_cedric_agent.py
    ELEVENLABS_API_KEY=... python3 backend/scripts/create_cedric_agent.py --dry-run

Prints the agent id (and the config on --dry-run). NEVER prints the key.
After creation, paste the id into avatars/cedric/avatar.yaml
(elevenlabs_agent_id) — dispatch stays off until the env flag flips too
(see docs/product/CEDRIC-ELEVENLABS-PILOT.md).

Pilot decisions encoded below (from the owner's plan, 2026-07-24):
  - first_message DISABLED — exactly one system greets, and that is the
    legacy self-introduction on join.
  - turn_eagerness "patient" — a meeting has natural pauses; don't pounce.
  - private + signed-URL-only — the browser/relay never see the API key.
  - pcm_16000 in AND out — Recall's mixed stream format, zero transcoding.
  - eleven_flash_v2_5 — same fast multilingual TTS family the repo uses.
  - NO tools yet (pilot 1 is conversation-only); client tools land in PR 4.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_NAME = "Cedric Meeting Pilot"
API_BASE = "https://api.elevenlabs.io"

# Multiparty + honesty rules appended to the yaml persona. They port the
# capability-grounding discipline the legacy pipeline enforces (never claim
# "done", no bare-yes approvals, say plainly what you don't know) into the
# agent's prompt — without them live Cedric would regress.
PILOT_RULES = """
MEETING PILOT RULES — you are Cedric in a LIVE multiparty business meeting.
Audio reaches you only when the Meeting Director routed an utterance to you:
it was addressed to you by name (Cedric — sometimes mis-transcribed as
Cedrick/Sedric/Sedrik), or it is a follow-up from the person you are already
talking with. Behave accordingly:
- Never treat "yeah", "okay", "mhmm" or similar backchannels as requests.
- Never take a bare "yes"/"okay"/"va bene" as approval of any action.
- Never claim an external action (email, task, invite, message, document) has
  already been completed. In this pilot you have NO tools: when someone asks
  you to DO something, confirm out loud that you'll set it up right after the
  call (the meeting system captures it from the transcript) — e.g. "Got it,
  I'll set that up once we wrap."
- If meeting context (purpose, participants, brief) was provided at session
  start, ground your answers in it. Say plainly when something is not in your
  context instead of inventing specifics, names, numbers, or capabilities.
- Reply in the language the speaker used (English or Italian).
- Keep spoken answers SHORT and conversational — a few sentences, no lists,
  no filler. If you are interrupted, stop and yield immediately.
""".strip()


def _load_cedric() -> dict:
    cfg = REPO_ROOT / "avatars" / "cedric" / "avatar.yaml"
    return yaml.safe_load(cfg.read_text()) or {}


def build_payload() -> dict:
    cedric = _load_cedric()
    persona = (cedric.get("persona_prompt") or "").strip()
    voice_id = (cedric.get("elevenlabs_voice_id") or "").strip()
    if not persona or not voice_id:
        sys.exit("avatars/cedric/avatar.yaml is missing persona_prompt or voice id")
    return {
        "name": AGENT_NAME,
        "tags": ["laura-pilot"],
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt": f"{persona}\n\n{PILOT_RULES}",
                    "llm": "gemini-2.5-flash",
                    "temperature": 0.4,
                },
                # One greeter only: the legacy join self-introduction.
                "first_message": "",
                "language": "en",
            },
            # Italian as an additional language (the team code-switches EN/IT);
            # the platform swaps to a multilingual TTS model per-language at
            # runtime. The DEFAULT model below must be an English one — the
            # API rejects flash v2_5 for English-default agents ("English
            # Agents must use turbo or flash v2").
            "language_presets": {
                "it": {"overrides": {"agent": {"language": "it"}}},
            },
            "tts": {
                "model_id": "eleven_flash_v2",
                "voice_id": voice_id,
                "agent_output_audio_format": "pcm_16000",
            },
            "asr": {
                "user_input_audio_format": "pcm_16000",
            },
            "turn": {
                "turn_timeout": 7,
                "turn_eagerness": "patient",
            },
            "conversation": {
                # Hard stop safety net well past any normal meeting turn set;
                # the relay owns the session lifecycle, not this cap.
                "max_duration_seconds": 3600,
                "client_events": [
                    "conversation_initiation_metadata",
                    "audio",
                    "interruption",
                    "user_transcript",
                    "agent_response",
                    "agent_response_complete",
                    "client_tool_call",
                    "vad_score",
                ],
            },
        },
        # Private agent: connections require a server-minted signed URL.
        "platform_settings": {
            "auth": {"enable_auth": True},
        },
    }


def _headers() -> dict:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        sys.exit("Set ELEVENLABS_API_KEY (never hardcode it).")
    return {"xi-api-key": key, "Content-Type": "application/json"}


def find_existing(client: httpx.Client) -> str | None:
    """agent_id of an existing agent with AGENT_NAME, else None (paginated)."""
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["cursor"] = cursor
        resp = client.get(f"{API_BASE}/v1/convai/agents", params=params)
        resp.raise_for_status()
        data = resp.json()
        for agent in data.get("agents", []):
            if agent.get("name") == AGENT_NAME:
                return agent.get("agent_id")
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print config, no API call")
    args = ap.parse_args()

    payload = build_payload()
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return

    with httpx.Client(headers=_headers(), timeout=30) as client:
        agent_id = find_existing(client)
        if agent_id:
            resp = client.patch(
                f"{API_BASE}/v1/convai/agents/{agent_id}", json=payload
            )
            action = "updated"
        else:
            resp = client.post(f"{API_BASE}/v1/convai/agents/create", json=payload)
            action = "created"
        if resp.status_code >= 400:
            # Error bodies carry field-level validation details, never the key.
            sys.exit(f"ElevenLabs {resp.status_code}: {resp.text[:2000]}")
        agent_id = agent_id or resp.json().get("agent_id", "")
        print(f"{action}: {agent_id}")
        print("next: set elevenlabs_agent_id in avatars/cedric/avatar.yaml")


if __name__ == "__main__":
    main()
