# Calendar auto-join — Laura joins meetings like a colleague

Goal: employees add Laura to a meeting (or he watches a shared calendar) and he
**auto-joins on time** — no one has to send an API call.

There are two ways, from simplest to fullest.

## 1. Manual / scheduled (works today, no OAuth)
Send Laura in now, or schedule him for later with `join_at` (ISO 8601, ≥10 min out):
```bash
curl -X POST https://YOUR_URL/sessions/start -H 'Content-Type: application/json' -d '{
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "join_at": "2026-07-02T15:00:00Z"
}'
```
A simple internal web button or Slack command can call this. Recall schedules the
bot; the avatar page mints a fresh Anam token at join time (so future scheduling
works).

## 2. Full calendar auto-join (Recall Calendar integration)
Recall connects a Google/Outlook calendar via OAuth and notifies you of events.
One-time setup:

1. **Create OAuth credentials** for Google Calendar (Google Cloud project) and/or
   Microsoft, and add them in the Recall dashboard's Calendar settings.
2. **Connect the user's calendar** through Recall's OAuth flow (Recall stores the
   connection and syncs events).
3. **Point Recall's calendar webhook** at:
   `https://YOUR_URL/webhooks/recall-calendar`

Then this backend does the rest: for each upcoming event that has a meeting link,
it **schedules Laura** (deduped by event id). See `/webhooks/recall-calendar` in
`backend/app/main.py` — the payload parser (`_extract_events`) is defensive;
adjust the field names to match your Recall calendar payload on the first live run.

### Rules (who gets an avatar)
Right now every event with a meeting link gets Laura. To scope it, filter in the
webhook by: attendee list (only if `laura@yourco` is invited), a title keyword
(e.g. "[laura]"), or a per-calendar `avatar_id`.

> Needs your Google/Microsoft OAuth app — that's the only part I can't set up for
> you. The backend side is ready.
