# cedric-voice tests

```bash
node --test "relay/cedric-voice/test/*.test.mjs"
```

No wrangler, no miniflare, no network. `harness.mjs` stands in for the three
things Workers gives the Durable Object — `WebSocketPair`, `fetch`, and the env
bindings — so `worker.js` runs unmodified in plain Node.

**Why this exists.** The bridge holds the fluency-critical logic of a
multiparty meeting: the Director gate, per-speaker buffering, turn boundaries,
follow-up windows, barge-in. It shipped with zero automated coverage, so every
regression in it was found live, in front of customers. Five defects were open
in it the day this suite was written, including "every sentence must re-say her
name" and "a `stop` in strict mode mutes her for the rest of the meeting".

**How to write one.** Tests are meeting scenarios. Assert on what reaches
ElevenLabs (`userChunks(el)`), because that is exactly what the room hears her
respond to — not on internal flags, unless the flag *is* the contract.

```js
session.handleControl({ type: "mode", strict: true });   // 3 people in the call
recall.emit(separate("Ben", VOICED));                    // Ben starts talking
session.handleControl({ type: "gate_open", speaker: "Ben" }); // backend heard "Laura"
assert.equal(userChunks(el).length, 1);                  // she hears his ask
```

Time is real but never slept on: push `Date.now()`-derived state backwards
(`session.gateVoiceAt = Date.now() - 6000`) instead of waiting.

**Deploying the worker is a separate step from merging.** `git push` does not
touch Cloudflare — see the deploy note in
`docs/product/CEDRIC-ELEVENLABS-PILOT.md`.
