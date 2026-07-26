# Working in Jira

## How Jira organizes work

The hierarchy, top to bottom: **Site** (`yourco.atlassian.net`, the whole
company's Jira) → **Projects** (a body of work with its own issues, board, and
workflow; each has a **key** like `ENG` so issues read `ENG-142`) → **Epics**
(large bodies of work spanning many sprints — a feature or initiative) →
**Stories / Tasks / Bugs** (the working unit: one assignee, a status, usually
story points) → **Subtasks** (steps inside an issue — use sparingly; they
don't show on the board as first-class cards). Issues are typed —
**Story** (user-facing value), **Task** (work with no user story), **Bug**
(a defect), **Epic** (the parent container) — and the type drives which fields
and workflow apply. Unlike Asana's multi-homing, a Jira issue lives in **one
project**; cross-project rollups happen through **Advanced Roadmaps / Plans**
or shared epics, not by putting one issue in two places.

## Issues done right

A well-formed issue has: a clear **summary** (verb-first, "Add SSO to the
login page", not "SSO"), exactly one **assignee**, a **status** that reflects
reality *right now*, and — on a Scrum team — **story points** (relative effort,
not hours). The **description** carries context and **acceptance criteria**
(often a checklist). **Priority** and **labels/components** make issues
filterable. **Links** express relationships ("blocks / is blocked by /
relates to / duplicates") — the blocked-by link is how a team sees a chain of
work before a slip cascades. A story that can't be finished in one sprint is
too big — split it, or make it an **epic** with child stories.

## Workflows and statuses

Every project runs a **workflow**: the set of **statuses** an issue moves
through (classic: **To Do → In Progress → Done**, but teams add **In Review**,
**Blocked**, **QA**, etc.) and the **transitions** allowed between them.
Statuses roll up to three **status categories** — To Do (grey), In Progress
(blue), Done (green) — which is what "% done" and reports key off. A healthy
workflow is short and mirrors how the team actually works; a status nobody
moves issues *out* of is a bottleneck the workflow is hiding. Transitions can
trigger **automation** (assign on move to In Progress, notify on Blocked).

## Boards, backlog, sprints, and releases

A **board** visualizes a project's issues. **Scrum boards** have a **backlog**
(the prioritized, groomed list) and run **sprints** (a fixed timebox — commit
a set of issues, burn them down, review, repeat); **velocity** (points
completed per sprint) is how you forecast. **Kanban boards** have no sprints —
work flows continuously, and **WIP limits** per column keep it from piling up.
**Versions / releases** ("fix version") group issues shipping together, giving
a release burndown and honest scope-vs-date visibility. **Backlog grooming**
(refining, estimating, ordering the backlog before a sprint) is where most PM
value in Jira actually happens.

## JQL — the query language

Jira's real power is **JQL** (Jira Query Language): `assignee = currentUser()
AND statusCategory != Done ORDER BY priority DESC` answers "my open work,
worst first". Building blocks: `project = ENG`, `sprint in openSprints()`,
`due <= endOfWeek()`, `status changed to Done during (-7d, now())`,
`labels = security`. Saved as a **filter**, a JQL query powers dashboards,
gadgets, and boards. If a question about the project can be asked in words, it
can almost always be a JQL filter — "everything overdue and unassigned",
"bugs opened this sprint", "stories with no story points".

## Conventions that keep a Jira project healthy

- One issue = one owner = one next action. Discussion lives in the issue's
  comments, not in chat about the issue.
- Status is updated the moment work moves, not at stand-up — a board that
  lags reality is worse than no board.
- Estimate in **points**, not hours, on a Scrum team; keep the scale small and
  consistent so velocity means something.
- Every sprint issue has an owner and points before the sprint starts;
  unpointed or unassigned issues get fixed in grooming, not mid-sprint.
- Close issues as **Done**, don't delete them — the history is the audit trail.
- Project/issue naming and components that sort and filter beat free-text soup.

## How I (Laura) connect to Jira

At the start of each meeting I receive a live snapshot of the connected Jira
site: open issues, their owners, due dates, and status — that is what I answer
from when you ask "what's open / what's blocked / who owns ENG-142 / what's
slipping this sprint". I read your issues; I don't silently change them. If my
snapshot doesn't cover something (an issue created seconds ago, or beyond the
snapshot cap), I say so rather than guess. My general project-management
knowledge — sprints, backlogs, estimation, workflows, risk — applies whether
your team runs Jira, Asana, or a whiteboard; Jira is one place those practices
live.
