# Working in Asana

## How Asana organizes work

The hierarchy, top to bottom: **Organization/Workspace** (the whole company)
→ **Teams** (groups of people with their own project lists) → **Projects**
(a body of work with a view: list, board, timeline, or calendar) →
**Sections** (groupings inside a project — stages, categories, or sprint
buckets) → **Tasks** (the atomic unit: one owner, one due date) →
**Subtasks** (steps inside a task — use sparingly; deeply nested subtasks
hide work from project views). A task can live in **multiple projects at
once** (multi-homing) — the same task can sit on the team's delivery board
and in a leadership tracking project without duplication. That is Asana's
most underused superpower.

## Tasks done right

A well-formed task has: a verb-first name ("Draft the Q3 pricing page", not
"Pricing"), exactly one **assignee** (Asana enforces this — shared
responsibility goes in collaborators), a **due date** (date, not vibes), the
context in the **description**, and any acceptance criteria as a checklist
of subtasks. **Dependencies** ("blocked by / blocking") make sequencing
explicit and let Asana warn owners when an upstream slip lands on them.
**Milestones** are special tasks marking a binary event — use them for the
dates leadership actually asks about.

## Custom fields, rules, and forms

**Custom fields** add structured columns to a project: priority, status,
effort, cost — sortable and reportable. Keep the set small; ten fields
nobody fills is worse than three everyone does. **Rules** automate the
boring transitions: "when moved to section Done, mark complete", "when
priority set to High, add the lead as collaborator". **Forms** turn a
project into an intake funnel — requests arrive as structured tasks instead
of Slack messages. If a team does the same triage moves every day, a rule
should be doing it.

## Portfolios, goals, and status

**Portfolios** roll multiple projects into one view with status, progress,
and owners — the layer program managers live in. **Goals** link
company/team objectives to the projects that serve them. **Project status
updates** (on-track / at-risk / off-track with a short narrative) are
Asana's native RAG reporting — a weekly status update written where the
work lives beats a slide assembled far from it.

## Views and reporting

Every project renders as List (spreadsheet-style triage), Board (kanban
flow), Timeline (Gantt-style with dependencies — where date conflicts
become visible), or Calendar. **Search and saved searches** answer "all my
overdue tasks across projects"; **Dashboards** chart custom fields
(burnup, workload by assignee, tasks by status). **Workload** shows
per-person capacity across projects — the honest view of who is
overcommitted before someone burns out.

## Conventions that keep a workspace healthy

- One task = one owner = one next physical action. Discussions live in
  comments on the task, not in email threads about the task.
- Sections mirror the team's real workflow stages, and a task's section is
  updated the moment reality changes — a stale board is worse than none.
- Completed tasks are COMPLETED, not deleted — history is the audit trail.
- A weekly sweep: overdue tasks get a new date or an explicit escalation,
  never silent decay. Unassigned tasks get an owner or get deleted.
- Project naming that sorts: "2026-Q3 · Website relaunch", not "misc stuff".

## How I (Petra) connect to Asana

At the start of each meeting I receive a live snapshot of the connected
workspace: projects, open tasks, owners, due dates, and overdue flags —
that is what I answer from when you ask "what's open / what's overdue /
who owns X". When the meeting produces action items, they are captured and
— once approved on the dashboard — filed into Asana as real tasks with the
receipt linked back. I never write to the board without an approval, and I
say "captured for the board", never "done", until the receipt exists. If
my snapshot doesn't show something (a project beyond the snapshot cap, a
task created seconds ago), I say so rather than guess.
