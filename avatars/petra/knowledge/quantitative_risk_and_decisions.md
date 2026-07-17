# Quantitative Risk & Decision Analysis

## Putting numbers on risks — when it's worth it

Qualitative scoring (probability × impact, 1–5) triages the register;
quantitative analysis earns its cost only for the big calls: contingency
sizing, bid/no-bid, date commitments with penalties. Don't quantify what
you can't estimate honestly — false precision ("34.7% risk") is worse
than a scored guess because it launders uncertainty into authority.

## Expected monetary value (EMV)

EMV = probability × impact, summed over outcomes. A 30% chance of a €50k
delay is a −€15k expected cost; a mitigation costing €8k that cuts the
probability to 10% changes EMV to −€5k — the mitigation is worth €10k of
expected value against €8k of cost, so buy it. EMV is also how you SIZE
the contingency reserve: the sum of EMVs across the register is a
defensible reserve, unlike a flat 10% ritual. Its limit: EMV averages over
many trials, but your project runs once — for existential risks (the 5%
chance that kills the company), expected value is the wrong lens; treat
those as constraints, not costs.

## Decision trees

For sequential choices under uncertainty (build vs buy, settle vs fight,
release now vs harden first): draw the choice nodes and chance nodes,
put probabilities and payoffs on the branches, roll back from the leaves
taking EMV at chance nodes and the best branch at choice nodes. The
discipline matters more than the arithmetic — the tree forces you to name
the alternatives, the uncertainties, and the payoffs you were previously
arguing about implicitly. Include the value of WAITING (buying
information) as a branch: a €20k prototype that resolves a €200k
uncertainty is usually the best node on the tree.

## Ranges beat points: three-point and Monte Carlo

Single-point estimates hide the shape of uncertainty. Three-point
estimates (optimistic/likely/pessimistic) feed PERT means; running the
whole schedule or budget as distributions — Monte Carlo simulation —
answers the question leadership is actually asking: "what date/cost are we
X% confident in?" (P50 vs P80). The structural insight Monte Carlo makes
visible: parallel paths make projects LATER than intuition expects
(merge bias — every path must finish), and correlated risks (one vendor
behind several tasks) fatten the tail far beyond independent maths.
You rarely need the simulation itself in a small project — but always
speak in ranges and confidence levels, never in single dates you secretly
disbelieve.

## Sensitivity: manage the few risks that matter

Tornado analysis ranks which uncertainties actually move the outcome —
typically three or four inputs dominate. Manage those actively (buy
information, mitigate, transfer) and stop polishing the long tail of
trivial register entries. A risk register with forty entries and no
ranking is a filing cabinet; five quantified drivers with owners and
triggers is management.

## Decision hygiene under uncertainty

Separate the DECISION from the OUTCOME: a good decision can get a bad
result and vice versa — judge process, or you'll teach the team gambling.
Write one-page decision records (options, assumptions, probabilities as
believed THEN, the call, the trigger to revisit) — they make later reviews
honest and stop hindsight rewriting history. Beware anchoring (the first
number spoken owns the room), optimism bias (plan from reference class,
not aspiration), and the loudest-voice prior — structured estimation
(silent writes, then discussion) is cheap insurance against all three.
