# Agent Card, cross-repo skill discovery (Laura ⇄ Cedric)

**Status: PLAN / CONTRACT DRAFT, no code in this doc.** Written 2026-07-09.
This is the shared contract both repos pin to so each agent knows the other's
skills and nothing falls through the cracks. Companion to the live integration
contract ([`SURFACE-API.md`](SURFACE-API.md)) and the tenancy plan
([`../infra/MULTI-TENANCY.md`](../infra/MULTI-TENANCY.md)).

Laura and Cedric are two independently-deployed services in **two separate
repos** (Laura here; Cedric's Slack orchestrator in Ben's workspace). They must
NOT share a code package; a shared pip/npm dep forces lockstep version bumps on
every independent deploy. Instead **both repos pin to one versioned JSON Schema**
(this doc) and each serves its own card.

---

## North-star principle

> Capabilities are **static, declarative metadata generated from the same
> `avatar.yaml` that defines the persona**, served on the control-plane HTTP
> surface, and injected into the brain prompt as a compact digest. **never on
> the live `ws/{conversation_id}` path, never carrying transcript.**

This makes discovery + routing structurally incapable of regressing the
live-meeting contract or the PII rule, and keeps "adding an avatar = adding a
folder" true (the folder's `capabilities:` block auto-publishes as a skill).

---

## 1. The card schema (A2A-shaped subset)

We adopt the **shape** of a Google A2A *Agent Card*: not the A2A JSON-RPC
transport (two known, mutually-authenticated partners don't need it). Served at
`GET /.well-known/agent-card.json`.

```jsonc
{
  "schema_version": "1.0",        // CONTRACT version; bumped only on breaking change; additive otherwise
  "agent_id": "laura",            // "laura" | "cedric"
  "name": "Laura",
  "description": "Callable AI process avatar: joins meetings, answers grounded+cited from process docs, tracks vs templates, delivers post-meeting artifacts.",
  "url": "https://api.lauravatar.com",
  "version": "2026.07.09",        // this agent's own deploy version
  "tenant": null,                 // RESERVED for org_id (multi-tenant later); null = global/demo
  "invocation_surface": "in-meeting",   // "in-meeting" | "slack"
  "securitySchemes": { "bearer": { "type": "http", "scheme": "bearer" } },
  "skills": [
    {
      "id": "laura.meeting.answer",
      "name": "Grounded meeting answers",
      "family": "meeting",                          // route by FAMILY/tag, never by leaf id (see §5)
      "tags": ["meeting", "rag", "process-qa"],
      "examples": ["What's our refund SOP?", "Who signs off on this stage?"],
      "defer_to_me_when": "someone needs a grounded, cited answer from process docs during a call"
    },
    { "id": "laura.meeting.artifact", "family": "meeting", "name": "Post-meeting artifact",
      "defer_to_me_when": "you need summary/decisions/actions/missing-steps/readiness/follow-up email for a call" },
    { "id": "laura.org.search", "family": "memory", "name": "Cross-meeting memory",
      "defer_to_me_when": "you need what was decided/committed across past meetings",
      "invocation": { "method": "GET", "path": "/org/search?q=" } }
  ]
}
```

Cedric's card is the identical shape with `agent_id: "cedric"`,
`invocation_surface: "slack"`, and his ~41 tools rolled into ~8 **skill
families** (`cedric.calendar.*`, `cedric.email.*`, `cedric.slack.*`,
`cedric.task.*`, `cedric.approvals.*`, `cedric.crm.*`, …), each with a
`defer_to_me_when`.

### Versioning

`schema_version` (shared, additive-only; copy the `ARTIFACT_VERSION` discipline
in `cedric/integration.py`), plus a per-card `version` and an HTTP `ETag`. The
two repos stay in sync by pinning `schema_version` here **and** a CI fixture
test in *both* repos (see §4).

---

## 2. How each avatar declares its skills (`avatars/<id>/avatar.yaml`)

Keeps the moat literal; a new avatar is still one folder, and its skills
auto-publish with it, generated from the same yaml that defines the persona (so
they never drift from reality).

```yaml
# avatars/cedric/avatar.yaml  (append; optional: absent → derived from
# role + process_templates + knowledge headings, so existing avatars still
# publish something)
capabilities:
  invocation_surface: in-meeting
  skills:
    - id: cedric.meeting.answer
      family: meeting
      name: In-meeting answers & action capture
      examples: ["schedule the follow-up", "remind me to send the deck"]
      defer_to_me_when: "someone asks to DO something (schedule/send/create/remind). I capture it and hand to Slack"
  peer:                                # who to hand OFF to (mirror of the fetched peer card)
    agent_id: cedric-slack
    card_url: ${SURFACE_CARD_URL}      # Ben's /.well-known/agent-card.json
```

The prose self-description that exists today
(`avatars/cedric/about/cedric_capabilities.md`) stays as the **human/spoken**
face ("what can you do?"); this block is the **machine-readable** source the card
and the digest are built from. One source of truth, two renderings.

Loader: `Avatar` gains `capabilities: dict = None` (parsed with the same
`_coalesce` defaulting discipline as `talk_body`/`silent`);
`cards.build_card(avatar)` serializes it into the card's `skills[]`;
`build_agent_card()` unions every avatar's skills into the aggregate card.

---

## 3. Endpoints, fetch/cache, and the prompt digest

All on the control-plane HTTP surface, all off the live path; this mirrors the
`org_api.py` pattern exactly (new router, 2-line `include_router` in `main.py`).

**Laura serves (new `cards_api.py` + `cedric/cards.py`):**

```
GET /.well-known/agent-card.json   → build_agent_card()   # aggregate, ETag + Cache-Control
GET /avatars/{id}/card             → build_card(load(id)) # per-avatar A2A card
```

Both read-only, `ETag = sha256(body)`, `304` on matching `If-None-Match`. Reuse
the deliberately-open `/avatars` auth stance; static skill metadata, zero PII.
(Once `org_id` lands and the card becomes per-org, move it behind the Bearer.)

**Laura consumes Cedric's card**: `fetch_peer_card()` as a sibling of
`fetch_context()` in `cedric/callback.py`: `GET` Cedric's card URL at startup +
on a TTL, send `If-None-Match` with the stored ETag, reuse the existing Bearer,
cache the parsed card + ETag in a module global. New config:
`surface_card_url`, `peer_card_ttl_seconds` (mirroring `surface_context_url`).

**The digest**: distilled to one line per skill and injected in the **same
place** `inject_brief()` folds context into the live memory channel (precomputed,
cached, so nothing touches `answer_question_stream`'s retrieval hot path):

```
PEER AGENT: Cedric (Slack). If a request matches one of his skill families,
DON'T answer it: say "that's Cedric's area; want me to bring him in?" and
capture it with queue_action(route_to='cedric', family=<family>).
- meeting: someone asks to DO something (schedule/send/create/remind)
- calendar: book/reschedule/find a time
- email: send/draft an email
- ...
```

Graceful degrade: **no peer card → no digest, and never claim/route to a skill
absent from the current card.**

---

## 4. The handoff (NEXT: additive fields on the existing webhook)

Reuse the signed, retried, PII-safe `action.requested` seam
(`callback.send_action_requested` / `notify_action_requested`); no new
transport. Two additive optional fields:

```jsonc
{ "event": "action.requested", "bot_id": "...", "external_ref": {},
  "action": "schedule the review", "owner": "", "due": "",
  "route_to": "cedric", "family": "calendar",   // NEW, additive
  "at": "..." }
```

Cedric's receiver turns `route_to` + `family` into a Slack deep-link / approval
card. No change to `ws/{conversation_id}`, `{type:"speak",text}`, or
`wire_artifact`'s transcript stripping.

**If the handoff is load-bearing** (the pitch is "nothing falls through the
cracks"), it needs delivery guarantees, or we shouldn't oversell it:

- **Idempotency key** (`bot_id` + `action_norm`) so retries don't double-book.
- **Cedric → Laura ack** that flips the ledger action to `routed`, so Laura
  learns the handoff landed (falling back to "check the artifact later" *is* the
  manual gap the feature claims to remove).
- **Hop-count / origin tag**: Laura routes to Cedric, who can book Laura via
  `/sessions/start`; a loop-guard prevents ping-pong.

---

## 5. Gotchas the adversarial review caught

1. **Cedric's half is assumed, not owned.** "Mutual" discovery depends entirely
   on Ben's repo serving the same-schema card and running the fixture test -
   neither enforceable from here. **A doc is not a cross-repo contract:** open an
   explicit issue/PR into Cedric's repo with a named owner and a target
   `schema_version` as a tracked dependency. Until then, Laura ships only her
   PUBLISH half + graceful-degrade CONSUME half.
2. **Route by skill FAMILY/tag, never by hardcoded leaf id.** The plan warns
   against hardcoding peer skill strings; so the defer logic must match by
   `family` with alias tolerance, or every Cedric-side rename silently
   misroutes. (This is why the schema carries `family`.)
3. **Card rot.** Skills must be generated from `avatar.yaml`/source, never
   hand-maintained in the card JSON, or the digest misroutes. Cedric's side must
   do the same from his tool registry.
4. **Defer decisions have no eval yet.** "Laura's brain judges it's Cedric's
   domain" is an unmeasured LLM classification; false-defers (Laura refusing
   something she *can* answer) directly damage the product. Add a small labeled
   defer/answer eval (20–30 asks: Laura-domain, Cedric-domain, ambiguous) and
   gate the digest on a measured misroute rate before it influences the live
   path.
5. **Reserve `tenant: null` now.** When `org_id` lands the card becomes
   per-org capability disclosure; the reserved field avoids a card re-version.

---

## 6. Sequencing

- **NOW:** freeze this schema + the skill-family taxonomy; add the
  `capabilities:` block + loader; ship Laura's two discovery endpoints; ship
  `fetch_peer_card` + the digest (graceful-degrade). Open the cross-repo
  dependency issue in Cedric's repo.
- **NEXT:** the `action.requested` handoff fields + reliability (idempotency,
  ack, loop-guard); Cedric publishes his card and consumes Laura's; CI fixture
  test gating both repos; the defer/answer eval.
- **LATER:** org-addressable cards (per-org, behind Bearer); A2A extended
  (authenticated) card for structured invocation; a curated registry **only** if
  a third agent/marketplace appears.

---

## Open questions for the owner / Ben

1. **Cedric's card URL + auth**: does Ben's orchestrator expose
   `GET /.well-known/agent-card.json`, and does it accept the same Bearer
   (`LAURA_CONTEXT_TOKEN`) Laura already presents on `fetch_context`, or a
   distinct token?
2. **Owner of the shared taxonomy**: this file is the one home both repos pin
   to; confirm Ben references it (vs a `docs/04-api-contract.md` on his side).
3. **Cedric's ~41 tools → ~8 skill families**: confirm the family list so
   Laura's defer logic targets stable families, not individual tools.
4. **Cedric as a per-org principal**: the same open question that blocks the
   tenancy meeting→org binding
   ([`../infra/MULTI-TENANCY.md`](../infra/MULTI-TENANCY.md) §5.1). *Flagged as
   "depends on Ben" (2026-07-09).*
5. **In-meeting handoff**: conversational-only + user consent
   ("want me to bring him in?"), or auto-fire the `action.requested` webhook the
   moment a peer-domain ask is detected?
