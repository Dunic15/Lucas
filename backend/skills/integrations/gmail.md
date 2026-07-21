# Gmail — integration skill

Guidance for turning meeting agreements into GOOD emails. Loaded at typing
time whenever the org can send mail (native Google or Pipedream Gmail).
Execution is approval-gated on the dashboard and sends AS THE OWNER's
account — every rule below follows from that.

## Typing an email well (`email.send` / `gmail.create_draft`)

- **to**: only addresses that literally appear in the item text, the meeting
  brief, or the attendee list. NEVER guess a domain ("marco@" + company is an
  invention). If the room said only a first name and no address is derivable,
  prefer `gmail.create_draft` over `email.send` — a draft can't misfire.
- **subject**: plain and specific, max ~8 words ("Recap — pricing decision,
  next steps"), no "Meeting follow-up" filler.
- **body**: short, first person as the owner, 3-6 sentences or a tight
  bullet list. State what was agreed, who does what, by when. No transcript
  paste, no invented commitments.
- **cc/bcc**: only when explicitly asked ("put Dana in copy").
- Reply threading: when the ask is "reply to X's email", set `thread_id` +
  `in_reply_to` only if those identifiers are actually known (from context);
  otherwise send a fresh mail referencing the topic in the subject.

## What is (and is not) an email action

- An email action = someone agreed a MESSAGE should go out (recap, intro,
  answer, reminder).
- Not email actions: "loop in legal eventually", vague "let's tell the team"
  with no recipient, anything already sent during the call.

## Send vs draft

- Recipient explicit + content agreed → `email.send`.
- Recipient fuzzy, content sensitive, or the owner said "prepare/draft" →
  `gmail.create_draft` (lands in the owner's Drafts; they hit send).

## Execution layer quirks

- The adapter builds real MIME (plain or multipart when `html_body` is set);
  Gmail strips Bcc headers on delivery — that's normal, not a bug.
- Receipts: the Gmail message id comes back as the receipt — surface it.
- One send per approval: a failed send is re-approvable from the card; never
  type the same email twice into two actions.

## Language

Write the email in the language the room used for that agreement (an Italian
meeting gets an Italian recap), and keep names/product terms exactly as
spoken — never translate a project or company name.
