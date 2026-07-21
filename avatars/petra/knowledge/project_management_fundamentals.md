# Project Management Fundamentals

## The iron triangle (scope, time, cost)

Every project balances three constraints: scope (what gets built), time (when
it ships), and cost (people and money). Quality is the fourth, implicit
dimension. You can fix at most two; the third must flex. When a stakeholder
asks for more scope on the same deadline with the same team, the honest
answers are: cut other scope, move the date, add capacity (which helps less
than people expect; see Brooks's law), or knowingly accept lower quality.
A project manager's core job is making that trade-off explicit *before* it
gets made silently by exhausted engineers.

## Project lifecycle

Initiation → planning → execution → monitoring & control → closure.
- **Initiation** answers "why": the problem, the sponsor, rough size, and a
  go/no-go. Its artifact is the project charter.
- **Planning** answers "what and how": scope statement, work breakdown,
  schedule, budget, risk register, communication plan.
- **Execution** is doing the work; the PM's job shifts to unblocking,
  coordinating, and protecting focus.
- **Monitoring & control** runs in parallel: compare actuals against the
  plan, surface variance early, and re-plan deliberately instead of drifting.
- **Closure** is deliberate: acceptance confirmed, handover done, lessons
  captured, the team thanked and released.

## The project charter

A one-to-two page agreement that prevents most later fights. It names: the
problem and business case, measurable objectives, in/out of scope, the
sponsor, the PM's authority, key stakeholders, headline milestones, budget
envelope, and top known risks. If you cannot get a sponsor to sign a charter,
that is itself the project's biggest risk.

## Work breakdown structure (WBS)

Decompose the deliverable, not the activity: break the outcome into smaller
outcomes until each piece is estimable and ownable (rule of thumb: no work
package larger than ~two weeks for one owner). The WBS is the backbone for
estimates, assignments, and progress tracking; anything not in it is, by
definition, scope creep when it appears.

## Milestones and the definition of done

A milestone is a binary, verifiable event ("contract signed", "beta live for
10 customers"), never a percentage ("80% done" is the most dangerous phrase
in project reporting; the last 20% routinely takes half the time). Every
deliverable needs a definition of done agreed *before* work starts: the
checklist that makes "done" mean the same thing to the builder, the PM, and
the stakeholder.

## Scope creep and change control

Scope creep is not a villain; it is unmanaged change. The fix is a lightweight
change process: any scope addition gets written down, sized, and traded, what
moves out, or what date/cost moves, with the sponsor deciding. The discipline
is writing it down; a five-line change note beats a meeting memory every time.
