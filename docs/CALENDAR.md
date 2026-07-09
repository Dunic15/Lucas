# Calendar auto-join — Laura joins meetings like a colleague

Goal: employees add Laura to a meeting (or she watches a shared calendar) and she
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
Recall connects Laura's Google calendar via OAuth and notifies you of events.
One-time setup:

1. **Create OAuth credentials** for Google Calendar in Google Cloud.
   - Enable the Google Calendar API.
   - Add scopes:
     `https://www.googleapis.com/auth/calendar.events.readonly` and
     `https://www.googleapis.com/auth/userinfo.email`.
   - Add this authorized redirect URI:
     `https://YOUR_URL/oauth/google/callback`.
2. **Set env vars** on the backend (App Runner service env / SSM secrets under
   `/laura/prod/*`, or your local `.env`):
   - `GOOGLE_CALENDAR_CLIENT_ID`
   - `GOOGLE_CALENDAR_CLIENT_SECRET`
   - `GOOGLE_CALENDAR_REDIRECT_URI=https://YOUR_URL/oauth/google/callback`
   - `CALENDAR_INVITE_EMAILS=laura.ai.122222@gmail.com`
3. **Connect Laura's calendar** by opening:
   `https://YOUR_URL/oauth/google/connect`
   while signed in as `laura.ai.122222@gmail.com`.
   The callback exchanges the Google code for a refresh token and creates the
   Recall Calendar V2 connection. The refresh token is sent directly to Recall
   and is not written to git or returned in the response.
4. **Point Recall's calendar webhook** at:
   `https://YOUR_URL/webhooks/recall-calendar`

Then this backend does the rest: for each upcoming event that has a meeting link
and includes Laura's email in the attendee list, it **schedules Laura** (deduped
by event id). For Recall Calendar V2 `calendar.sync_events` webhooks, the backend
fetches the changed events with `calendar_id` + `last_updated_ts`, then applies
the invite filter.

### Rules (who gets an avatar)
Only meetings that invite `laura.ai.122222@gmail.com` schedule the avatar. Change
`CALENDAR_INVITE_EMAILS` to point at a different mailbox, or comma-separate
multiple addresses if you create aliases later.

If the Google OAuth client is still in Testing mode, add
`laura.ai.122222@gmail.com` as a Google OAuth test user. Testing-mode refresh
tokens can expire after 7 days; publish/verify the OAuth app before relying on it
long term.
