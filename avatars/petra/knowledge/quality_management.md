# Quality Management

## Quality is planned, not inspected in

The core theorem: the cost of preventing a defect < the cost of finding it
< the cost of a customer finding it — each step roughly an order of
magnitude. So quality budget belongs early: clear acceptance criteria,
design reviews, automated checks, definition of done. **Cost of quality**
splits into prevention (standards, training, automation), appraisal
(reviews, testing, audits), and failure cost (internal rework + external
incidents, support, reputation). Teams that "can't afford" prevention are
already paying failure cost — it's just booked somewhere less visible.

## Quality of the PRODUCT vs quality of the PROCESS

Product quality: does the deliverable meet its acceptance criteria and the
user's actual need (fitness for purpose beats spec compliance when they
diverge — surface that divergence, don't silently pick one). Process
quality: does the way of working reliably produce good output — review
coverage, escaped-defect rate, rework percentage. A project can ship a good
product from a burning process once; process quality is what makes the
second and tenth time repeatable.

## The practical toolkit

- **Definition of done** per deliverable type, written before work starts —
  the anti-"90% done" device.
- **Reviews with teeth**: a review that never rejects anything is a
  ceremony. Small batches review better than big ones.
- **Root-cause habits**: five-whys or fishbone on repeated defects; fix the
  category, not the instance. One escaped defect is noise; the same class
  three times is a process signal.
- **Metrics worth tracking**: escaped defects per release, rework share of
  effort, review turnaround, first-pass acceptance rate. Metrics nobody
  acts on get deleted — a dashboard is not a quality program.
- **Non-functional criteria** (performance, security, accessibility,
  compliance) go INTO acceptance criteria, or they will be discovered as
  incidents.

## Quality vs the triangle

When scope/time/cost squeeze, quality is the invisible fourth constraint
that absorbs the pressure silently unless the PM makes it visible. The
professional move: quality reductions are EXPLICIT scope decisions ("we
ship without load-testing the import path; here's the risk") signed off by
the owner — never a private engineering shortcut. "Technical debt" is a
loan with interest; log it like a risk, with an owner and a repayment
intent, or it compounds into the next project's velocity problem.
