# Codex task brief — Callable AI Process Avatar

Current product direction: implement **one first AI agent** (Sofia) and use
**Anam** for the realtime face/voice layer. Do not add Marcus or any second
agent yet.

Read [README.md](README.md) first for what this project is and how it fits
together, and [avatars/README.md](avatars/README.md) for Sofia's editable
configuration.

---

## Scope

- Keep `avatars/sofia/` as the only tracked agent.
- Use Anam, not Tavus, for face/voice streaming.
- Keep the backend brain responsible for deciding when and what Sofia says.
- The Anam browser page receives backend `speak` messages and calls
  `anamClient.talk(text)`.

## Integration

Anam API keys stay server-side. The browser gets a short-lived token by calling:

```text
POST /anam/session-token/{anam_session_id}
```

The camera page connects to:

```text
ws://<host>/ws/<anam_session_id>
```

The backend sends:

```json
{ "type": "speak", "text": "<words>" }
```

---

## Conventions
- Synthetic, audit-safe data only. **No secrets, no real PII.**
- Keep changes focused on Sofia, Anam, and the live meeting loop.
- Match the existing tone and markdown/code style.
