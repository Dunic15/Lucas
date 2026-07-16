# Risk & Issue Management

## Risk vs issue

A **risk** is a possible future problem; an **issue** is a problem you already
have. They need different treatment: risks get probability×impact scoring and
mitigation plans; issues get an owner, a next action, and a date — today.
Teams that only track issues are choosing to be surprised.

## The RAID log

One living document per project: **R**isks, **A**ssumptions, **I**ssues,
**D**ependencies. Assumptions and dependencies are the underrated half —
"we assumed the API team ships in June" and "we depend on legal sign-off"
cause more slips than any technical risk. Review the RAID log briefly every
week; a RAID log that isn't reviewed is a graveyard.

## Scoring and prioritizing risks

Score probability (1–5) × impact (1–5); anything ≥12 needs an owner and a
written response now. Keep it coarse — false precision ("37% likely") wastes
time. Re-score monthly: risks rot in both directions.

## The four risk responses

- **Avoid**: change the plan so the risk can't happen (cut the risky scope,
  choose the boring technology).
- **Mitigate**: reduce probability or impact (prototype early, add a fallback
  vendor, feature-flag the launch).
- **Transfer**: move it to someone better placed to hold it (insurance,
  fixed-price contract, managed service).
- **Accept**: consciously live with it — written down, with a trigger that
  tells you when acceptance stops being okay.

The silent fifth response — ignore — is the only wrong one.

## Early-warning signals worth watching

- A milestone that moves twice.
- "90% done" two weeks in a row.
- A dependency owner who stops coming to meetings.
- Estimates that only ever grow in review.
- The same risk raised verbally three times but never written down.

## Escalation without drama

Escalation is information routing, not blame: "this decision/blocker exceeds
what this room can resolve; it needs X by DATE or the consequence is Y."
Escalate on a schedule trigger you agreed in advance (e.g. any blocker open
>5 working days auto-escalates to the sponsor) so nobody has to be the
villain who raised it.
