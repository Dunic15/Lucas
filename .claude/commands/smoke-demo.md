---
description: Smoke-test the demo end-to-end (health, /demo/ask, post-meeting, demo page)
---

Use the **demo-runner** agent to launch the backend and verify the offline demo
works end-to-end: `/health`, `/demo/ask` (grounded answer with citation),
`/demo/post_meeting` (summary + checklist + email), and the demo page serving.
Report pass/fail per step with the exact failing output if any.
