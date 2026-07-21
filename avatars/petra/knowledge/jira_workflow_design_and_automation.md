# Workflow design and automation in Jira

## Designing a workflow that mirrors reality

A workflow is the set of **statuses** an issue can hold and the **transitions**
allowed between them. The design rule: model how work *actually* moves, then
stop. **To Do → In Progress → In Review → Done** covers most software teams;
add **Blocked** only if the team truly parks work, and **QA** only if a
separate person tests. Every status must answer "who acts next?" — a status
with no owner is where work goes to die. Keep statuses mapped to the right
**status category** (To Do / In Progress / Done), because boards, reports, and
every `statusCategory` query key off the category, not the name. Warning signs
of a bad workflow: more than seven statuses, statuses nobody can explain the
difference between, transitions that force a detour ("you can't go from Review
back to In Progress"), and a column on the board that only ever grows.

## Transition rules: guardrails, not bureaucracy

Transitions can carry **validators** (block the move unless a condition holds
— "no Done without a fix version"), **conditions** (who may move it — only QA
closes a bug), and **post functions** (what happens automatically after — set
the resolution, clear the assignee). Use them to protect the two or three
fields the team's reporting depends on, and no more; every validator is
friction at exactly the moment someone is trying to update status, and too
many teach people to stop updating at all. The **resolution** field deserves
one guardrail everywhere: an issue can read "Done" but still count as open in
reports if resolution was never set — a classic silent data bug.

## Automation rules

Jira automation is **trigger → condition → action**. The rules that pay for
themselves on almost every project:

- **Auto-assign on start** — issue moves to In Progress with no assignee →
  assign to the person who transitioned it.
- **Stale-work nudge** — in-flight issue untouched for N days → comment and
  ping the assignee; the board stops lying quietly.
- **Blocked escalation** — issue sits in Blocked more than N days → notify the
  lead; blockers age badly when they age silently.
- **Parent housekeeping** — all subtasks done → transition the parent;
  keeps epics honest without anyone remembering to.
- **Sprint hygiene** — issue added to an active sprint → comment that scope
  changed; the sprint report stops surprising people.
- **Cross-tool notifications** — high-priority bug created → message the
  team channel; triage latency drops from hours to minutes.

Two cautions: automation that edits issues can trigger other automation —
guard against loops; and a rule nobody documented becomes "the ghost that
keeps changing my tickets," so name rules by what they do and keep the list
short enough to audit.

## Schemes, permissions, and sane defaults

Projects share configuration through **schemes** — permission schemes (who
can do what), notification schemes (who gets emailed on which event), and
workflow schemes (which issue types use which workflow). The PM-level rule:
prefer the shared, default schemes until a real need diverges; every custom
scheme is a maintenance branch someone must understand later. Trim
notification schemes ruthlessly — when Jira emails everyone about everything,
everyone filters Jira to a folder and the notifications protect no one. The
overall principle is the same as for fields and statuses: **every piece of
configuration must earn its complexity**, because a simple project people
update beats a sophisticated one they avoid.
