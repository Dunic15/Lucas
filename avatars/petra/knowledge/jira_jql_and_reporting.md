# JQL and reporting in Jira

## JQL is the skill that unlocks Jira

Most questions a team asks in a meeting are one JQL query away. A query is
built from **field – operator – value** clauses joined with `AND`/`OR`, ordered
with `ORDER BY`: `project = ENG AND status = Blocked ORDER BY priority DESC`.
The operators that matter: `=`, `!=`, `IN`, `NOT IN`, `~` (text contains),
`IS EMPTY` / `IS NOT EMPTY` (unset fields), and the comparison set (`<`, `>=`)
for dates and numbers. **Functions** make queries live instead of stale:
`currentUser()`, `openSprints()`, `startOfWeek()`, `endOfMonth()`, `now()`,
`membersOf("team-x")`. Relative dates read naturally: `due <= 7d`,
`created >= -30d`.

## Recipes worth knowing by heart

- **My open work, worst first** — `assignee = currentUser() AND statusCategory
  != Done ORDER BY priority DESC, due ASC`
- **Overdue and ownerless** — `due < now() AND assignee IS EMPTY AND
  statusCategory != Done`
- **Blocked chain** — `issueLinkType = "is blocked by" AND statusCategory !=
  Done` (what's stuck and why)
- **Sprint health** — `sprint IN openSprints() AND status = "In Progress" AND
  updated <= -3d` (in-flight work nobody has touched in three days)
- **Standup prep** — `status CHANGED TO Done DURING (-1d, now())` (what closed
  since yesterday); `status CHANGED DURING (-1d, now())` (everything that moved)
- **Unestimated sprint work** — `sprint IN openSprints() AND "Story Points"
  IS EMPTY` (fix in grooming, not mid-sprint)
- **Scope creep** — `sprint IN openSprints() AND created >= startOfWeek()`
  (issues added after the sprint started)

A useful query becomes a **saved filter**; a saved filter powers boards,
dashboard gadgets, and subscriptions (emailed results on a schedule). Filters
have their own permissions — share with the team or a query silently shows
different results to different people.

## The reports and what each one answers

- **Burndown** — "will we finish the sprint?" Remaining work versus the ideal
  line; a flat burndown means blocked work or invisible scope, a cliff on the
  last day means statuses lag reality.
- **Velocity** — "how much do we really do per sprint?" The 3–5 sprint average
  is the only honest capacity number for planning; never compare velocities
  across teams, the points scale differs.
- **Cumulative flow** — "where does work pile up?" A widening band is the
  bottleneck column; healthy flow shows parallel, evenly-spaced bands.
- **Control chart** — "how long does an item actually take?" Cycle-time
  scatter; outliers are the stories worth a retrospective conversation.
- **Release burndown / version report** — "will the release make the date?"
  Scope line versus done line, with the scope-added tail made visible.
- **Sprint report** — the honest close-out: completed, not-completed (rolled
  to backlog), and added-mid-sprint, issue by issue.

## Dashboards that people actually read

A good project dashboard is five gadgets, not twenty: sprint burndown, "my
open work" filter results, blocked issues, overdue issues, and a pie by
assignee to spot overload. Wallboards rotate those on a screen. The test of a
dashboard is whether it changes a decision in the meeting; a gadget nobody
acts on is noise. When someone asks a status question twice, the answer should
become a saved filter on the dashboard — that is how reporting load leaves the
PM and moves into the tool.
