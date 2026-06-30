# MVP readiness checklist

Goal: one working demo avatar, **Sofia**, that joins a meeting, appears with
face and voice, answers when called by name, and returns a post-meeting
checklist/email draft.

## Definition of a working first MVP

The first MVP is working when this exact path succeeds:

1. Sofia joins a Google Meet/Zoom/Teams test call through Recall.ai.
2. Recall renders `GET /avatar?anam_session_id=...` as the bot camera.
3. The avatar page starts an Anam WebRTC stream and shows Sofia.
4. A participant says, "Sofia, what are we missing?"
5. Recall sends a realtime transcript webhook to the backend.
6. The wake-word gate allows the turn, RAG retrieves Sofia's SOP context, and
   the brain returns a grounded answer with confidence above threshold.
7. The backend sends `{ "type": "speak", "text": "..." }` over the websocket.
8. The avatar page calls `anamClient.talk(text)`, Sofia speaks in the meeting,
   and the caption bar shows the same text.
9. `POST /sessions/{bot_id}/end` removes the bot and returns a summary,
   checklist, and follow-up email draft.

## Already in the repo

| Area | Status |
|---|---|
| One demo agent | Sofia exists under `avatars/sofia/` with synthetic SOP docs. |
| Meeting bot wiring | `recall_client.py` creates a Recall bot with realtime transcript webhook and webpage camera output. |
| Avatar face/voice | PR #1 switches the camera page to Anam session tokens + JS SDK `talk()`. |
| When-to-speak gate | `decision.py` only speaks when Sofia is called by wake word and confidence passes threshold. |
| RAG | `rag.py` chunks markdown by heading and retrieves cited sections from Sofia's local index. |
| Post-meeting artifact | `brain.py` returns summary, gap checklist, and follow-up email JSON. |
| Basic tests | Current no-key tests cover wake word matching and confidence gating. |

## Critical missing items before a live MVP demo

| Priority | Missing item | Why it matters | Owner / file |
|---|---|---|---|
| P0 | Finish and merge the backend provider work | Current `main` still hardcodes Anthropic/Voyage. The dirty local work in `/Users/duccioo/Desktop/Lucas` adds local/free provider seams but is not committed yet. | `backend/app/brain.py`, `backend/app/embeddings.py`, `backend/app/llm.py`, `backend/app/config.py` |
| P0 | Real service credentials | A live meeting needs Recall + Anam keys, plus either Anthropic/Voyage or completed local providers. | `.env` from `.env.example` |
| P0 | Build Sofia's index | Without `avatars/sofia/.index.json`, live questions fail before the brain can answer. | `python backend/scripts/ingest.py sofia` |
| P0 | Live Recall/Anam integration test | The only unproven part is media in a real meeting: Recall rendering the Anam page, Anam audio reaching the call, and websocket speech timing. | test call |
| P1 | Recall webhook verification | For anything beyond a private demo, reject unsigned/untrusted webhook traffic. | `backend/app/main.py` |
| P1 | Better error surfacing | If Anam token creation, missing index, or brain provider fails, return a clear session/debug event instead of only server logs. | `main.py`, `avatar.html` |
| P1 | Demo runbook | Keep one command sequence for free/offline and one for live trials so the demo is repeatable. | `docs/DEMO.md` |
| P2 | Minimal CI | Run tests and compile checks on PRs before merging. | GitHub Actions |

## What is not needed for the first MVP

| Item | Decision |
|---|---|
| pyannote.ai | Not needed for the first live demo. Recall transcript webhooks already include participant metadata in this code path, which is enough to prove "Sofia answers when called." Add pyannote later if raw audio diarization, overlapping speaker handling, or stronger owner attribution becomes a real problem. |
| Supabase pgvector / Qdrant | Not needed for one avatar and a few SOPs. The local JSON index is enough for the demo. Add a vector DB when there are many companies, many agents, or permissions per document. |
| LlamaIndex | Not needed yet. The current markdown-heading chunker is simple and inspectable. Add LlamaIndex when connectors to Drive/Notion/Slack become part of the product. |
| Next.js frontend | Not needed for the bot camera page. Static `frontend/avatar.html` is simpler and has fewer moving parts for Recall rendering. |
| Slack/Jira/Notion/Gmail actions | Not needed for MVP. Return draft JSON/email first; add real writes only after human approval flows are designed. |
| Proactive interventions | Defer. The first demo should speak only when called by name. |

## Recommended next sequence

1. Merge PR #1 or continue from `codex/content-and-ui` so Anam is the active
   avatar provider.
2. Finish the backend provider work already started in the original checkout:
   local/stub brain, local embeddings, and an offline simulator if you want a
   no-key demo path.
3. Fill `.env` with live Recall + Anam credentials:
   `RECALL_API_KEY`, `PUBLIC_BASE_URL`, `ANAM_API_KEY`, `ANAM_AVATAR_ID`,
   `ANAM_VOICE_ID`.
4. Build the index:

   ```bash
   python backend/scripts/ingest.py sofia
   ```

5. Run the backend and expose it:

   ```bash
   uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
   ngrok http 8000
   ```

6. Start a test meeting session:

   ```bash
   curl -X POST http://127.0.0.1:8000/sessions/start \
     -H 'Content-Type: application/json' \
     -d '{"meeting_url": "https://meet.google.com/your-test-call"}'
   ```

7. In the meeting, say: "Sofia, what are we missing?"
8. End the session:

   ```bash
   curl -X POST http://127.0.0.1:8000/sessions/<bot_id>/end
   ```

## Source notes

- Anam session tokens are the right browser pattern because the API key stays
  server-side and the page uses a short-lived token:
  <https://anam.ai/docs/api-reference/sessions/create-session-token>
- The Anam JS SDK supports `streamToVideoElement()` and `talk()`:
  <https://anam.ai/docs/javascript-sdk/reference/basic-usage> and
  <https://anam.ai/docs/javascript-sdk/reference/talk-commands>
- Recall's realtime transcript webhook is the current MVP input path:
  <https://docs.recall.ai/docs/real-time-transcription>
