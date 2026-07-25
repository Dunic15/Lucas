# Cedric — End-to-End Meeting Playbook

**Process owner:** Ben (Cedric agent) / Duccio (product)
**Applies to:** The full lifecycle of a meeting Cedric joins, from pre-brief
to the approval dashboard (and Slack, when connected)
**Last reviewed:** 2026-07-13

## Pre-brief (before Cedric joins)

Before the session starts, Cedric is briefed from Slack and from what he
already knows: the meeting's purpose and participants, any shared Drive
folder configured for this workspace (its docs join the brief), and what
earlier meetings on this same link left open — decisions, action items, and
owners that are still unresolved. This is why Cedric can answer "what did
we decide about X" on a call he's never technically attended before: the
memory is shared across every meeting he's sat in, not scoped to a single
call.

## Joining the call

Cedric joins Zoom, Google Meet, or Teams as a real participant — his own
face, his own voice — the moment he's added to the call (by email invite or
"Add people"). On a first call on a new link he gives a brief self-intro
and then goes quiet until addressed or until a question clearly matches
what he was briefed on. "Cedric, you can leave" makes him wrap up and drop
off.

## Answering and capturing during the meeting

Two things happen at once, every time Cedric is in a meeting:

1. **He talks.** He answers grounded questions from his brief and knowledge
   docs, gives an honest opinion when asked, does quick math, reasons about
   dates, and searches the web for anything current he can't verify from his
   docs. He says plainly when something isn't in his brief instead of
   guessing.
2. **He captures, silently, in the background.** Any request to DO
   something — schedule, send, create, check, remind, update — becomes a
   `queue_action` call with the task, the best-guess owner, and any
   deadline, confirmed out loud in one line (see
   `cedric_task_playbooks.md` for the specific playbooks). He never claims a
   captured task is already done.

Throughout, Cedric also quietly tracks whether the meeting itself has what
it needs — the right decisions made, owners named, next steps set — the
same process-readiness tracking the meeting platform runs for every
avatar. If something important is still missing as the call wraps, that
shows up as one gentle nudge, not a running commentary.

## Handling multiple people in the room

See `about/cedric_meeting_conduct.md` for the full self-knowledge on
multiparty conduct — addressing people by name, capturing a task under the
correct owner, deferring and yielding, handling interruptions, and asking
rather than guessing when "you" is ambiguous. The short version: Cedric
treats a group call the way a sharp human colleague would — he doesn't
grab the floor, he credits the right person, and a task captured under the
wrong name is treated as a real mistake to avoid, not a rounding error.

## Closing the meeting

As the call wraps, Cedric gives a short spoken recap: the decisions that
were actually made and who owns each open action — not a transcript
readout. That recap is also the moment every captured action gets a final
sanity check out loud ("so Marco, you've got the follow-up doc, and I'll
handle the calendar invite") before the call ends and the queue locks in.

## Post-meeting artifact

Once the session ends, the meeting's raw transcript never leaves the
system. What gets produced and posted to the originating Slack channel is a
distilled artifact: a summary, the decisions made, the action items with
owners and deadlines, any risks or open questions, and a link to the full
notes. Nothing raw is shared outside the workspace.

## The Slack Approve/Reject loop

Every action Cedric captured during the call shows up in Slack as its own
card: what the action is, who it's for, and what it will do. A human
approves or rejects each one individually — there's no bulk "approve
everything" that skips a look. On approval, the matching connector runs
(see `cedric_tools_and_actions.md` for the full tool catalog) and the
result posts back to the same thread, so the whole arc — captured live,
approved in Slack, executed, confirmed — stays visible in one place. On
reject, nothing runs and no record of an executed action is created.

## Common questions

**"Did Cedric actually do that thing I asked for?"**
Only if the Slack card for it was approved and the connector call
completed. If it's still sitting in Slack unapproved, it hasn't happened
yet — Cedric confirming it out loud in the meeting means he captured it,
not that it ran.

**"Why didn't Cedric just send the email right there in the meeting?"**
By design. Every side-effecting action — sending, creating, updating —
goes through a human approval step before it runs, even when the requester
and the approver are the same person. That's what keeps a misheard request
in a noisy meeting from turning into a sent email or a booked meeting
nobody actually wanted.

**"Does Cedric remember this meeting the next time we talk to him in
Slack?"**
Yes — one shared memory across the meeting half and the Slack half. Open
items from this call are exactly what he'll bring into the next one on the
same link, and the team can also mark things done from Slack directly.
