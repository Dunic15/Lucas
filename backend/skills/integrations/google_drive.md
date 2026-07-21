# Google Drive — integration skill

Drive is READ-ONLY for avatars today: a shared folder feeds the avatar's
knowledge (Brain folder-sync + the join-time folder brief). There are NO
Drive write action types — this file exists so the loader has honest
guidance the day they arrive, and so typing never invents one meanwhile.

## The one hard rule

**Never type a Drive write.** "Upload the deck", "share the folder with
Marco", "create a doc" have no executable spec — captured asks like these
stay generic actions (a human does them from the card). Typing them as a
fake `drive.*` action would approve into a guaranteed failure.

## What Drive DOES give the avatar

- **Folder knowledge**: the avatar's configured shared folder is synced into
  its knowledge index — answers grounded in those docs cite them naturally
  ("per the onboarding doc…").
- **Join-time brief**: a snapshot of the folder's contents is available from
  the start of the call, so "do we have a doc on X?" is answerable from real
  file names, not guesses.

## Answering Drive questions honestly

- "Can you read our Drive?" → only the folder the org connected for this
  avatar, nothing else — and say exactly that.
- "Did you see the latest version?" → the sync runs periodically; if the doc
  was changed minutes ago, say the snapshot may lag and offer to re-check
  after the call.
- A file that isn't in the folder brief does not exist for the avatar —
  never claim sight of files outside the connected folder (privacy is the
  feature, not the limitation).

## When write actions land (future)

Keep the same shape as the other adapters: args drawn only from the item
text + brief (no invented file names or recipients), approval-gated, receipt
= the file/folder link.
