# `cedric/`: SFF Studio fork glue

This package holds everything specific to the **Cedric X Laura** fork: the
integration with the Cedric Slack orchestrator that books this backend into
Google Meet calls, receives the transcript, and drives approvals. It is kept
separate so upstream `Dunic15/Laura` merges stay clean; the shared,
upstream-tracked code carries only thin, greppable hooks into this package.

## What lives here

| File | Contents |
|---|---|
| `callback.py` | Signed callbacks to the orchestrator: `send_status`, `send_ended` (retried), `send_action_requested` (single attempt; fired when the live `queue_action` tool captures an ask mid-meeting), `fetch_context`. HMAC (`X-Laura-Signature`) + Bearer, follows one redirect hop. |
| `integration.py` | Logic lifted out of `main.py`: `auth_error` (Bearer gate), `MeetingContext`, `MAX_BRIEF_BYTES`/`brief_too_large`, `build_integration`, `deliver_ended`, `notify_failed`, `notify_action_requested`, `handle_webhook_status`, `inject_brief`. |
| `__init__.py` | Re-exports the public surface used by `main.py`. |

The Cedric avatar itself (persona, voice, wake word, knowledge) is the other
fork-owned folder: `avatars/cedric/`.

## The `# CEDRIC` convention

Some seams genuinely interleave with upstream's session lifecycle and can't be
lifted wholesale (e.g. one line inside `_finalize_session`, `start_session`, and
the Recall webhook). Every such seam is a **single line tagged `# CEDRIC`**.

    grep -rn "# CEDRIC" backend/app

is the complete inventory of fork touch-points in shared files. When resolving
an upstream merge conflict, that grep tells you exactly what fork behavior must
survive. Keep the footprint to one-line hook calls into this package; never let
a multi-line block creep back into `main.py`.

## Touch-points outside this package (intentionally left in place)

These are additive and low-conflict, so they live with the code they extend:

- `config.py`: the `# ── Cedric integration ──` settings block (auth token,
  webhook secret/token, context token, default avatar, callback timeout).
- `store.py`: the `integration` field on `Session` + its `integration_json`
  column and migration (intrinsic to the persistence model).
- `recall_client.py`: `bot_name` threaded through `create_bot`, plus
  `delete_bot` (used by session cancel).
- `main.py`: `StartRequest`'s orchestrator fields and ~16 `# CEDRIC` hook lines.

## Tests

`backend/tests/test_cedric_integration.py` covers the surface here (auth gate,
callback signing/retries, cancel, integration wiring) and
`backend/tests/test_action_bridge.py` covers the action bridge (the platform
`queue_action` tool -> artifact/ledger merge + the `action.requested` webhook).
Run the whole suite with `python -m pytest` from `backend/`.
