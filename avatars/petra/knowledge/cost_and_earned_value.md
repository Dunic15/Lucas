# Cost Management & Earned Value

## Budgeting a project

Build the budget bottom-up from the WBS (estimate work packages, roll up),
sanity-check top-down against the business case, and separate three pots:
the **performance budget** (the work as planned), **contingency reserve**
(for identified risks, owned by the PM, spent transparently), and
**management reserve** (for unknown-unknowns, owned by the sponsor).
Burn rate alone is a vanity metric: spending 50% of budget means nothing
without knowing how much VALUE got delivered for it; which is exactly what
earned value fixes.

## Earned value in three numbers

At any status date:
- **PV (planned value)**: the budgeted cost of the work that SHOULD be
  done by now.
- **EV (earned value)**: the budgeted cost of the work ACTUALLY done.
- **AC (actual cost)**: what was actually spent doing it.

Everything else derives from these. Schedule variance SV = EV − PV
(negative = behind). Cost variance CV = EV − AC (negative = over budget).
As ratios: **SPI = EV/PV** and **CPI = EV/AC**: 1.0 is on plan, 0.8 means
you're getting 80 cents of plan per dollar/day. The insight that makes EVM
worth its bureaucracy: "we've spent half the budget" and "we're halfway
done" are independent claims, and EVM is the only common language that
says both at once.

## Forecasting with EVM

**EAC (estimate at completion)**: the classic formula EAC = BAC / CPI
(budget at completion scaled by demonstrated efficiency) assumes current
performance continues; usually the honest assumption. ETC = EAC − AC is
what's left to spend. **TCPI** (to-complete performance index) = remaining
work / remaining money; when TCPI needs to be 1.3 while your CPI has been
0.85 for three months, the plan is arithmetic fiction and needs
re-baselining, not optimism.

## Measuring "earned" honestly

EV requires crediting progress per work package: 0/100 (nothing until
done; strict, best for short tasks), 50/50 (half on start, half on
finish), or percent-complete (accurate only with objective criteria -
otherwise it becomes the 90%-done-forever task). Milestone-weighted credit
is the practical middle. Whatever the rule: pick it up front, apply it
uniformly, and never let a task self-report percentages without a
definition of done.

## Cost conversations that go wrong

- Sunk cost: money spent is never a reason to continue: only remaining
  cost vs. remaining value is.
- Cutting QA/testing to "save cost" converts visible budget into invisible
  failure cost downstream (see quality management).
- A fixed budget with growing scope is a decision nobody made: surface it
  as an explicit trade, in writing, before the money runs out rather than
  after.
