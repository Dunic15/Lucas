# Estimation & Planning

## Why estimates go wrong (and what to do about it)

The planning fallacy is universal: people estimate the best case and call it
the likely case. Three antidotes: estimate from *reference class* ("how long
did the last three things like this actually take?") rather than imagination;
decompose until pieces are small enough to compare to past work; and add
buffer explicitly at the project level, never hidden inside each task.

## Relative estimation: story points and t-shirt sizes

Points and t-shirt sizes (S/M/L/XL) work because humans compare better than
they measure. Rules that keep them useful: the whole team estimates together
(planning poker surfaces hidden disagreement; a 2 next to an 8 is a
conversation, not an average); points are local to one team and never a
cross-team productivity metric; and anything XL must be split before it
enters a sprint.

## Three-point (PERT) estimation

For calendar answers, take optimistic (O), most likely (M), pessimistic (P)
and use (O + 4M + P) / 6. The value isn't the formula; it's forcing the
pessimistic case into the open. If P is 5× O, the task is not understood;
spike it first.

## Critical path

The critical path is the longest chain of dependent work; the project's true
minimum duration. Tasks on it deserve the senior people, the earliest starts,
and the most monitoring; a day slipped there is a day slipped for the project.
Off-path tasks have slack: use it deliberately (level workloads) instead of
discovering it accidentally. Recompute the path when dependencies change -
it moves.

## Buffers that survive contact with management

A schedule with no visible buffer gets its invisible buffer consumed and then
slips. Better: an explicit project buffer (10–25% depending on novelty) at
the end, owned by the PM, spent transparently ("we used 4 of 15 buffer days
on the auth rework"). Buffer burn rate is one of the best early-warning
metrics a project has.

## Capacity, not wishes

Plan against real capacity: people × working days − meetings, support duty,
holidays, on-call, and the ~20% tax of context switching for anyone on two
projects. A "6-week plan" that assumes six perfect weeks from five perfectly
focused people is a 10-week plan wearing makeup.

## Re-planning is not failure

Plans are forecasts. When actuals diverge, the professional move is a
deliberate re-plan, new date or reduced scope, communicated with reasons,
not weekly one-day slips ("the death of a thousand Fridays"). One honest
re-plan costs less credibility than five silent ones.
