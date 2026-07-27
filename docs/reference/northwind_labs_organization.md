# Northwind Labs — company organization

*Synthetic reference company. Every person, team and project below is fictional.*

**NOT in any avatar's knowledge pack, deliberately.** It lived in
`avatars/petra/knowledge/` for one day and had to be pulled: on the ElevenLabs
runtime the avatar had no real Asana board in her prompt, and this document gave
her a fluent, plausible project status to invent instead ("Project Harbor, owner
Priya Raghavan"). Kept here as a writing reference for org-knowledge docs; put it
back in a knowledge pack only for an avatar with no live workspace to confuse it
with.

## What Northwind Labs is

Northwind Labs builds supply-chain visibility software for mid-market
distributors: a platform that ingests carrier, warehouse and ERP feeds and tells
operations teams where a shipment actually is, and what to do when it slips.
Founded 2021, 42 people, headquartered in Amsterdam with a distributed
engineering team across Europe. Roughly 60 paying customers. Revenue is annual
SaaS subscription plus a per-integration setup fee.

Two product lines: **Atlas** (the core visibility platform) and **Beacon** (the
alerting/exception-handling layer sold as an add-on).

---

## Leadership team

The five people who own budget and can make a final call without escalating.

- **Marisa Feld — CEO.** Owns strategy, fundraising, and any decision that
  changes the company's direction or headcount plan. Final say on pricing.
  Chairs the Monday leadership sync.
- **Tomas Kruger — CTO.** Owns engineering, architecture and security. Final say
  on technical direction, build-vs-buy, and anything touching the data platform.
- **Aisha Bello — VP Product.** Owns the roadmap and what gets built next. Final
  say on scope and prioritisation; runs quarterly planning.
- **Daniel Okafor — VP Revenue.** Owns sales, customer success and renewals.
  Final say on discounts up to 20%; beyond that it goes to Marisa.
- **Elena Rossi — COO.** Owns finance, people operations, legal and vendor
  contracts. Final say on spend under €25k and on hiring process.

---

## Engineering (18 people, reports to Tomas Kruger)

Four squads, each with a tech lead who owns delivery for that squad.

- **Platform squad** — *Lead: Priya Raghavan.* Owns the ingestion pipeline,
  carrier integrations and the data model. Members: Jonas Lindqvist (backend),
  Wei Chen (backend), Karim Haddad (data engineering).
- **Atlas squad** — *Lead: Sofia Marchetti.* Owns the core visibility product:
  tracking, dashboards, the customer-facing web app. Members: Lucas Ferreira
  (full-stack), Nina Weber (frontend), Adam Novak (backend).
- **Beacon squad** — *Lead: David Chen.* Owns alerting, exception rules and the
  notification engine. Members: Fatima Zahra (backend), Oliver Bright
  (full-stack).
- **Infrastructure & Reliability** — *Lead: Henrik Sørensen.* Owns cloud, CI/CD,
  observability and the on-call rotation. Members: Grace Mwangi (SRE),
  Tobias Klein (SRE).

**QA:** Ana Ribeiro (single QA lead, works across squads — she owns the release
checklist and is the last sign-off before a production release).

**On-call:** one primary and one secondary per week, rotating across
Infrastructure and the squad that shipped last. Henrik Sørensen owns the
rotation schedule.

---

## Product & Design (5 people, reports to Aisha Bello)

- **Julia Sandberg — Senior Product Manager, Atlas.** Owns the Atlas roadmap and
  writes the specs that Sofia's squad builds.
- **Rahul Menon — Product Manager, Beacon.** Owns Beacon and the integrations
  backlog; the main counterpart for David Chen's squad.
- **Clara Boldt — Lead Designer.** Owns design system, UX research and all
  customer-facing flows.
- **Marco Bianchi — Product Designer.** Works with Julia on Atlas.
- **Yuki Tanaka — Technical Writer.** Owns docs, release notes and in-app help.

---

## Go-to-market (11 people, reports to Daniel Okafor)

- **Sales — *Lead: Anders Holm* (Sales Manager).** Account executives:
  Beatriz Costa, Samuel Adeyemi, Lena Fischer. They own new business; deals over
  €100k ARR need Daniel's sign-off, and over €250k need Marisa's.
- **Customer Success — *Lead: Nadia Petrova* (Head of CS).** CSMs: Tom Whitfield,
  Ingrid Larsen. They own renewals, onboarding and the quarterly business
  reviews. Nadia is the escalation point for any unhappy customer.
- **Solutions Engineering — Pavel Novotny.** The technical pre-sales person;
  runs demos and scopes integrations with prospects. The bridge between Sales
  and Priya's Platform squad.
- **Marketing — *Lead: Chiara Esposito* (Head of Marketing).** Members:
  Felix Braun (content), Amira Said (demand generation).

---

## Operations (7 people, reports to Elena Rossi)

- **Finance — Robert Nowak (Finance Manager).** Owns budget, invoicing,
  forecasting. Approves purchase orders under €25k with Elena.
- **People Ops — Hannah Meier (Head of People).** Owns hiring, onboarding,
  performance cycles.
- **Legal & Compliance — Sandrine Dubois (Counsel, part-time).** Owns contracts,
  DPAs, GDPR and security questionnaires.
- **IT & Workplace — Miguel Santos.** Owns laptops, access, SaaS accounts.
- **Executive Assistant — Laura Vermeulen.** Supports Marisa; owns the
  leadership calendar and board-meeting logistics.

---

## Who decides what

When a meeting stalls on "who owns this", this is the answer.

| Decision | Accountable (one person) | Consulted |
|---|---|---|
| Product roadmap and priority | Aisha Bello | Julia Sandberg, Rahul Menon, Tomas Kruger |
| Technical architecture, build vs buy | Tomas Kruger | Priya Raghavan, Henrik Sørensen |
| Release go/no-go | Ana Ribeiro | squad tech lead, Henrik Sørensen |
| Pricing and discounts >20% | Marisa Feld | Daniel Okafor, Robert Nowak |
| Discounts up to 20% | Daniel Okafor | — |
| Hiring a new role | Elena Rossi (process), the hiring manager (choice) | Marisa Feld for headcount |
| Spend under €25k | Elena Rossi | Robert Nowak |
| Spend over €25k | Marisa Feld | Elena Rossi |
| Customer escalation | Nadia Petrova | Daniel Okafor, relevant tech lead |
| Security incident | Tomas Kruger | Henrik Sørensen, Sandrine Dubois |
| Vendor and tool contracts | Elena Rossi | Miguel Santos, Sandrine Dubois |

---

## Standing meetings

- **Leadership sync** — Mondays 09:00, 45 min. The five leads. Chaired by Marisa.
- **Squad standups** — daily 09:30, 15 min, per squad.
- **Product/Engineering weekly** — Wednesdays 14:00. Aisha, Tomas, all tech leads
  and PMs. Where scope disagreements get settled.
- **Pipeline review** — Thursdays 11:00. Daniel, Anders, the AEs, Pavel.
- **Customer health review** — every second Tuesday. Nadia, the CSMs, Daniel.
- **Quarterly planning** — one full day at the start of each quarter. Aisha runs
  it; every squad lead presents commitments.
- **All-hands** — last Friday of the month, 16:00.

---

## Active projects and codenames

- **Project Harbor** — replacing the legacy carrier-integration layer.
  Owner: Priya Raghavan. Target: end of Q3. The biggest technical risk this year.
- **Beacon 2.0** — configurable exception rules for customers.
  Owner: Rahul Menon, built by David Chen's squad.
- **Atlas Mobile** — a read-only mobile view for warehouse staff.
  Owner: Julia Sandberg. Currently descoped to a pilot with three customers.
- **SOC 2 Type II** — compliance programme. Owner: Sandrine Dubois, with
  Henrik Sørensen on the technical controls. Blocks two enterprise deals.

---

## Escalation paths

- A **customer** is unhappy → Nadia Petrova → Daniel Okafor → Marisa Feld.
- A **release is broken in production** → on-call primary → Henrik Sørensen →
  Tomas Kruger.
- A **deadline is going to slip** → the squad tech lead tells the PM the same
  day; PM tells Aisha at the Wednesday Product/Engineering weekly, or sooner if
  a customer commitment is at risk.
- A **security issue or data request** → Tomas Kruger and Sandrine Dubois
  together, immediately.

---

## Quick answers

- **"Who is in the organization?"** 42 people across five functions:
  Engineering (18, under Tomas Kruger), Go-to-market (11, under Daniel Okafor),
  Operations (7, under Elena Rossi), Product & Design (5, under Aisha Bello),
  plus the CEO Marisa Feld.
- **"Who runs engineering?"** Tomas Kruger (CTO); day to day it is the four
  squad leads: Priya Raghavan, Sofia Marchetti, David Chen, Henrik Sørensen.
- **"Who owns the roadmap?"** Aisha Bello, with Julia Sandberg on Atlas and
  Rahul Menon on Beacon.
- **"Who do I talk to about a customer problem?"** Nadia Petrova.
- **"Who approves this spend?"** Under €25k Elena Rossi; above, Marisa Feld.
- **"Who signs off a release?"** Ana Ribeiro.
