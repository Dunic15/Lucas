# Cedric: Task Playbooks (common in-meeting asks)

**Process owner:** Ben (Cedric agent + integrations)
**Applies to:** The most common "Cedric, can you..." requests in a meeting
**Last reviewed:** 2026-07-13

Every playbook below follows the same shape: a trigger phrase, what Cedric
captures with `queue_action` right then, what happens once the card is
approved in Slack, and what he says out loud in the meeting. Cedric never
runs the connector action live; see `cedric_tools_and_actions.md` for the
full capture-approve-execute contract.

## Send a follow-up email

**Trigger phrases:** "Cedric, send them a follow-up", "can you email
[person] about this", "shoot them a recap after this."

**Captured with queue_action:** action type `gmail_send`; recipient (named
or inferred from context); a one-line draft of what the email should cover;
requester; deadline if one was given ("before end of day").

**On approval:** the Slack side drafts and sends the email via the `gmail`
connector, and the sent message (or a link to it) posts back to the
approval thread.

**What I say:** "Got it. I'll draft that and get it out once we wrap.
You'll see it as a card in Slack before it actually sends."

## Schedule or reschedule a meeting

**Trigger phrases:** "Cedric, can you set up a follow-up on Friday", "find
us 30 minutes next week", "move Thursday's call to the afternoon."

**Captured with queue_action:** action type `calendar_create_event` (or
`calendar_update_event` for a reschedule); attendees; the requested
timeframe; duration; whether a Meet link is needed. Cedric may use
`calendar_list_events` live to check the calendar isn't already booked
before confirming a time out loud, but the event itself is never created
live.

**On approval:** the `google_calendar` connector creates or updates the
event and invites the attendees; the confirmed time posts back to the
thread.

**What I say:** "Friday works on the calendar I can see. I'll lock that in
as soon as we're done here."

## Create a Linear issue

**Trigger phrases:** "Cedric, file a ticket for that", "can you open an
issue so we don't lose this", "add that as a bug/task."

**Captured with queue_action:** action type `linear_create_issue`; a title
and short description from what was just discussed; the team/project if
named; the person named as owner (assignee).

**On approval:** the `linear` connector creates the issue with that title,
description, and assignee, and the issue link posts back to the thread.

**What I say:** "Noted: I'll get that filed in Linear with [name] as the
assignee once we wrap."

## Pull a Stripe revenue report

**Trigger phrases:** "Cedric, what's our revenue this month", "can you pull
a Stripe report for the last quarter", "how many paying customers do we
have."

**Captured with queue_action:** action type `stripe_revenue_report`; the
time range asked for; who requested it and where the summary should land
(reply in Slack vs. a document). This one is capture-only even though it's
a read, not a write. Cedric doesn't have live access to Stripe from inside
the meeting, so he's honest that the number isn't something he can state on
the spot.

**On approval:** the `stripe` connector pulls the report and posts the
summary (revenue, customer count, trend) to the thread.

**What I say:** "I don't have that number live in the room. I'll pull the
actual Stripe report right after this and drop it in Slack."

## Update HubSpot

**Trigger phrases:** "Cedric, log this in the CRM", "update the deal stage",
"add a note to their record."

**Captured with queue_action:** action type `hubspot_update`; the record
(company/contact/deal) named; the field or note to add; requester.

**On approval:** the `hubspot` connector applies the update and confirms
the record was changed in the thread.

**What I say:** "Got it. I'll log that against their record once we're
done here."

## Share or create a document (Notion / Drive)

**Trigger phrases:** "Cedric, can you write this up", "put this in a doc",
"share the rollout doc with them."

**Captured with queue_action:** action type `notion_draft` (or a Drive
share, depending on the connector); a one-line brief of the doc's content or
which existing doc to share; who it should be shared with.

**On approval:** the `notion` connector drafts or updates the page (or the
Drive share is applied) and the doc link posts back to the thread. Note:
Cedric can already READ a shared Drive folder live if his brief includes
one (see `about/cedric_capabilities.md`); that's separate from creating or
sharing a NEW doc, which always goes through this queued flow.

**What I say:** "I'll draft that up and share it with them right after the
call."

## Set a reminder

**Trigger phrases:** "Cedric, remind me to follow up next week", "ping me
before the deadline", "don't let this slip."

**Captured with queue_action:** action type `reminder`; what to be reminded
of; who for; when.

**On approval:** the reminder is scheduled (typically as a Slack message at
the target time) and confirmed in the thread.

**What I say:** "I've got that. I'll remind you before it's due."

## Summarize a Slack channel

**Trigger phrases:** "Cedric, what's been happening in the [X] channel",
"catch me up on Slack before this call", "did the team already discuss
this."

**Captured with queue_action:** action type `slack_summarize`; the channel
named; the time window (since when).

**On approval; or live if already connected and low-risk:** the `slack`
connector pulls recent messages and returns a short summary. Because this
is a read of a channel Cedric already has access to (not a new side effect),
it's the one playbook most likely to come back quickly after the call
rather than sit as a long-pending card; but it still surfaces as a normal
approval card, since Cedric doesn't pull channel history live inside the
meeting.

**What I say:** "I'll pull a summary of that channel and send it over once
we wrap."

## Owners

- Ben owns the connector wiring behind each playbook and which ones are
  live for a given workspace.
- The requester in the meeting owns the accuracy of what got captured
  (recipient, timeframe, content); the named owner on the card owns
  approving or rejecting it in Slack.

## Definition of done

A playbook is complete only when the Slack card was approved AND the
connector's result (sent email, created event, filed issue, posted report,
updated record, shared doc, scheduled reminder, or channel summary) posted
back to the thread; not when Cedric merely confirmed it out loud in the
meeting.

## Common gaps

- Capturing WHAT to do but not WHO it's for in a multi-person room; always
  anchor the action to a named owner before queuing it (see
  `about/cedric_meeting_conduct.md`).
- Assuming a report or lookup (Stripe, HubSpot, Slack summary) happened live
  just because Cedric answered fluently; reads still route through the
  queued flow unless explicitly marked live-safe above.
- Letting an approval card sit unapproved and assuming the task is handled
  because Cedric "said he'd do it."
- Requesting an action on a tool that isn't connected yet: the card will
  come back as "not connected," not silently fail.
