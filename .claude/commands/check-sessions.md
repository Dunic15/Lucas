---
description: Check for orphaned live sessions still running the per-minute meter
---

Golden rule: sessions left running keep the Recall (+Anam) per-minute meter
burning. Check for orphans:

1. `curl -s https://dhfgfe6yw6.eu-central-1.awsapprunner.com/health`: 
   `active_sessions` should be 0 when no meeting is actually happening.
2. If it isn't, list what's live: query Recall's bot list via the API
   (`RECALL_API_BASE` + `RECALL_API_KEY` from the backend env; ask the user to
   run it if the key isn't available locally) and show bot ids + meeting URLs.
3. For each bot that should be dead, end it properly:
   `POST /sessions/{bot_id}/end` on the backend (idempotent; stops both vendors
   and stores the artifact). Never leave this step for later.
4. Report what was found and what was ended.
