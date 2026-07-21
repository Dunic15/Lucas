# Northstar × Laura: Demo Script (5–7 minutes)

> SYNTHETIC DEMO. Northstar and Acme Robotics are fictional. Reset before every
> run: `python -m demos.northstar.product.reset` (or `POST /admin/reset`).

**Avatar:** Laura · **Company:** Northstar · **Customer:** Acme Robotics
**Start:** `http://127.0.0.1:8971/` · **Goal:** confirm the blocker, create one
guarded follow-up task after approval.

---

## 0 · Setup (before the room): 20s

Reset the product. Confirm health: `GET /healthz` → `tasks: 2`, version `1.0.0`.
Laura's Company Brain has ingested the Northstar knowledge pack.

**Presenter says:** "Laura is joining a Northstar onboarding review for a
customer called Acme Robotics. Everything here is synthetic."

---

## 1 · Understand Northstar: 45s

**User asks Laura:** "What is Northstar, and what are the onboarding stages?"

**Laura explains (grounded):** Northstar is an operations workflow-automation
platform; onboarding runs five stages. Kickoff → Data Integration →
Configuration → UAT → Go-Live *(cite: customer-onboarding-process → "The five
onboarding stages")*.

**Pages:** Home (`/`).

---

## 2 · Explain Acme's onboarding status: 60s

**User asks:** "Where is Acme Robotics, and what's blocking them?"

**Laura explains:** Acme is **at risk**. Onboarding has reached **Data
Integration** and is **blocked**: Acme hasn't provisioned the WMS sandbox
credentials *(cite: acme-robotics-account-brief → "Current status";
acme-onboarding-plan → "Blocker: WMS sandbox credentials")*. With a 2026-08-15
go-live this is **P1; go-live at risk**, escalating to the CSM within **4
business hours** *(cite: escalation-policy → "Blocker severity and timers")*.
She does **not** confuse that with the 1-business-day approval window.

**Pages:** Acme account (`/customers/acme-robotics`); health "at risk", blocker
banner.

---

## 3 · The visual-only target: 60s

**User asks:** "Open the stage that's blocked."

The Acme page shows the onboarding **pipeline diagram**. Every node has the same
accessible label ("Onboarding stage") and no stage-name text. **text alone
can't tell them apart**. Laura must read the diagram: the **amber** node, second
from the left, is the blocked stage. She clicks it and lands on the **Data
Integration** stage page, blocker shown.

**Pages:** Acme account diagram → `/customers/acme-robotics/onboarding/integration`.
**This is where genuine visual perception is required.**

---

## 4 · Propose the guarded operation (preview): 45s

**User asks:** "What should we do about it?"

**Laura proposes:** a **follow-up task** for the CSM to chase the WMS sandbox
credentials *(cite: approval-policy → "Follow-up task creation")*. She
**previews** it; the exact task is shown, and **nothing is written yet** (Tasks
table unchanged).

**Pages:** Tasks (`/tasks`) → **Preview task**.

---

## 5 · Rejection path: 30s

**Presenter says:** "First, what if we reject?"

Approval is **rejected**. Laura writes nothing. The Tasks page still shows only
the two seed tasks: **zero follow-up tasks**. (This is the safe default.)

---

## 6 · Approval path: 45s

**Presenter says:** "Now we approve."

On approval, Laura executes the guarded action **exactly once**. `task-0003` is
created with a visible **confirmation banner**. Receipt: `status: created,
task_id: task-0003`.

**Pages:** Tasks → **Approve & create** → confirmation.

---

## 7 · Visual verification: 30s

**User asks:** "Did it actually get created?"

Laura re-reads the Tasks page and points to the **new row `task-0003`**: 
"Follow up with Acme Robotics on Data Integration blocker": visibly present.
If asked to run it again, a second approved execution returns **exists** and
creates **no duplicate** (idempotent).

**Pages:** Tasks (`/tasks`); row `task-task-0003` present.

---

## 8 · Clean ending: 15s

**Presenter says:** "That's the loop: understand, perceive, propose, wait for
approval, verify. Everything is reversible."

Reset for the next run: `python -m demos.northstar.product.reset`.

---

### Timing

| Section | Time |
|---|---|
| 1 Understand | 0:45 |
| 2 Status | 1:00 |
| 3 Visual target | 1:00 |
| 4 Preview | 0:45 |
| 5 Reject | 0:30 |
| 6 Approve | 0:45 |
| 7 Verify | 0:30 |
| 8 End | 0:15 |
| **Total** | **~5:30** (within 5–7 min) |
