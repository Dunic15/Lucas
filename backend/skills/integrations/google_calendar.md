# Google Calendar — integration skill

Guidance for turning scheduling agreements into GOOD calendar events. Loaded
at typing time whenever the org has Calendar (native Google or Pipedream).
Events are created on THE OWNER's calendar after dashboard approval.

## Typing an event well (`calendar.create_event` / `calendar.update_event`)

- **title**: what the meeting IS, not the sentence that asked for it —
  "Follow-up: Q3 plan review", max ~8 words.
- **start/end**: ONLY when a concrete moment was said. "Tuesday at 3" in a
  meeting on 2026-07-21 → 2026-07-28T15:00 local; "sometime next week" →
  leave the time OUT and let the approval card's edit fill it (a wrong slot
  on someone's calendar is worse than a missing one). Default duration
  30 minutes when only a start was given; never invent multi-hour blocks.
- **attendees**: only emails that literally appear in the item, brief, or
  attendee list. The owner is implicit (it's their calendar). No guessed
  addresses — an invite to a stranger is unrecoverable.
- **description**: one sentence of context ("Agreed on the 2026-07-21 call:
  review the revised Q3 plan"). No transcript paste.
- Updating an event needs its id from a receipt or the calendar snapshot —
  if unknown, create a NEW event or leave a needs-details gap; never guess.

## What is (and is not) a calendar action

- Calendar action = the room AGREED to meet/hold time, with at least a rough
  when. ("Let's talk after the launch" with no date = a note for the summary,
  not an event.)
- Someone asking "what's on the calendar?" is a READ — answered live from
  the snapshot, never typed as an action.

## Execution layer quirks

- Created events carry a real Meet link automatically (the adapter requests
  one) — don't add placeholder links in the description.
- Timezone: times are interpreted in the owner's calendar timezone; type
  ISO local times, never bare "3pm" strings.
- Receipts: the event's htmlLink is the human-facing proof — surface it.

## Language

Event titles in the meeting's language; attendee names and project names
exactly as spoken.
