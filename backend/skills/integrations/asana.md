# Asana — integration skill

Guidance for turning meeting agreements into GOOD Asana tasks. Loaded only
when the acting avatar may use Asana (Petra). Execution runs through the
org's connected Pipedream Asana account, behind dashboard approval.

## Typing a task well

- **name**: short imperative title, drawn from the item — "Send Q3 deck to
  Marco", never a whole sentence of meeting prose. Max ~8 words.
- **notes**: ONE sentence of context (who asked, why). Don't paste transcript.
- **assignee**: only an email that literally appears in the item text.
  Otherwise OMIT — the task then defaults to the connected account's
  "My Tasks", which is visible; an unassigned+unprojected task is invisible
  to everyone in Asana.
- **due_on**: only when a concrete date was said ("by Friday" in a meeting on
  2026-07-21 → 2026-07-24). Never invent urgency.
- **project**: only a project name that literally appears in the item or the
  meeting summary. Wrong-project filing is worse than no project.

## What is (and is not) a task

- A task = a discrete piece of work someone AGREED to do.
- Not tasks: vague remarks, questions, things already done, decisions
  (decisions go in the summary, not the board).
- Updating an existing task needs its gid (from the workspace snapshot or a
  lookup) — if the gid isn't known, prefer creating a comment-style follow-up
  or asking, never guessing a gid.

## API quirks (execution layer)

- Create without a project REQUIRES the workspace gid (resolved automatically
  from the connected account's first workspace).
- With a project gid, workspace must be omitted (Asana 400s on both).
- Receipts: the permalink_url that comes back is the human-facing proof —
  surface it in the receipt so the row links to the real task.
- API base: https://app.asana.com/api/1.0, request body wrapped in {"data": …},
  opt_fields=gid,name,permalink_url for a useful response.

## ASR repair (live-meeting transcripts)

Task names arrive through speech recognition — repair OBVIOUS mishearings
using the meeting summary as context, never beyond it: "the key three plan"
in a meeting about Q3 planning is "the Q3 plan"; "as an a task" is "an Asana
task". If the summary doesn't disambiguate, keep the words as heard — a
literal name is recoverable, a wrong guess is not.

## "Assign it to me"

"Me" is the SPEAKER of the ask. Resolve it to that person's email only when
the attendee list (or the item text) provides one; otherwise omit assignee —
the card's edit affordance fills it in one click. Never map "me" to the
owner or the avatar by default.

## Language

When the room speaks Italian, keep the task NAME in the meeting's language,
but never translate project names — match them literally.
