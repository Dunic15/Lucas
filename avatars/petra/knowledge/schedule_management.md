# Schedule Management

## From WBS to schedule

A schedule is the work breakdown structure with three things added:
durations, dependencies, and people. Sequence first (what truly depends on
what), estimate second, assign third — assigning before sequencing is how
schedules become fiction. Dependency types, in practice: finish-to-start is
the default (B starts when A finishes); start-to-start and finish-to-finish
model overlapping work (testing starts when coding starts, plus a lag);
avoid start-to-finish — if you think you need it, redraw the network.

## Critical path method (CPM)

Forward pass computes each task's earliest start/finish; backward pass
computes the latest; the difference is **float** (slack). Tasks with zero
float form the **critical path** — the longest dependency chain and the
project's minimum duration. Everything about the critical path is
management-by-arithmetic: a day slipped on it slips the project a day;
float elsewhere is a budget you can spend deliberately (leveling people)
or lose accidentally. The path MOVES as reality diverges from plan —
recompute it at every meaningful update, because managing last month's
critical path is managing the wrong tasks.

## Compressing a schedule (when the date must move left)

Two honest techniques, both with price tags. **Fast-tracking**: run
sequential tasks in parallel — costs risk (rework when the overlapped
assumption breaks). **Crashing**: add resources to critical-path tasks —
costs money and obeys diminishing returns (Brooks's law: adding people to
late work makes it later when onboarding outweighs output). Anything else
— "work harder", unpaid overtime, silently cutting testing — is schedule
denial, not compression. Always name which technique you're buying and
what it costs.

## Baselines and variance

A **baseline** is the approved schedule frozen at a point in time. Actuals
are tracked AGAINST it — without a baseline, "are we late?" has no answer,
only feelings. Variance analysis is a weekly habit: which tasks moved,
did the critical path change, is the buffer burning faster than the work.
Re-baseline only through change control (a real scope/date decision), not
as cosmetic surgery to hide slippage.

## Milestones, buffers, and the reporting layer

Milestones mark binary events and carry zero duration — they are the
schedule's public API: leadership tracks milestones, the team tracks
tasks. Put the explicit project buffer BEFORE the immovable milestone, own
it as the PM, and report its burn rate ("used 6 of 15 buffer days") — a
healthier signal than per-task padding, which hides everywhere and helps
nowhere. Rolling-wave planning is legitimate: near-term work in detail,
far-term work in phases, refined as the horizon approaches.

## Agile scheduling is still scheduling

Sprints don't abolish the critical path; they chunk it. Velocity converts
backlog size into a forecast RANGE (optimistic/likely/pessimistic burnup),
never a date certainty. Fixed-date agile means scope is the release valve
— track "what's guaranteed by the date vs. what's stretch" explicitly, or
the date will quietly redefine itself.
