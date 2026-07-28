// Behaviour tests for the cedric-voice Durable Object — the multiparty
// Director gate, per-speaker buffering, turn boundaries and barge-in.
//
// Run: node --test relay/cedric-voice/test/
//
// Everything here is a MEETING scenario written as code: "three people are in
// the call, Ben addresses Laura, Dana says something to Ben mid-answer". The
// assertions are about what reaches ElevenLabs, because that is exactly what
// the room hears her respond to.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  makeSession,
  attachRecall,
  attachPage,
  elReady,
  separate,
  mixed,
  userChunks,
  contextUpdates,
  VOICED,
  SILENT,
  frame as frameOf,
  tick,
} from "./harness.mjs";

/** Let connectEL's two awaits resolve. */
async function settle(n = 6) {
  for (let i = 0; i < n; i++) await tick();
}

/** A live session: Recall ingress attached, page attached, EL ready. */
async function liveSession(opts = {}) {
  const ctx = await makeSession(opts);
  const page = attachPage(ctx.session);
  const recall = await attachRecall(ctx.session);
  await settle();
  await elReady(ctx.session, ctx.el);
  return { ...ctx, page, recall };
}

// ───────────────────────── open mode (1:1) ─────────────────────────

test("open mode forwards a participant's voiced audio to ElevenLabs", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    recall.emit(separate("Ben", VOICED));
    assert.equal(userChunks(el).length, 1);
    assert.equal(userChunks(el)[0].user_audio_chunk, VOICED);
  } finally {
    restore();
  }
});

test("the bot's own separate stream never reaches ElevenLabs", async () => {
  const { el, recall, restore } = await liveSession({ botName: "Laura" });
  try {
    recall.emit(separate("Laura", VOICED)); // matched by name
    recall.emit(separate("Someone", VOICED, { is_bot: true })); // matched by flag
    assert.equal(userChunks(el).length, 0);
  } finally {
    restore();
  }
});

// ───────────────────── strict Director gate ─────────────────────

test("strict + gate closed: human-to-human talk never reaches ElevenLabs", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    recall.emit(separate("Ben", VOICED));
    recall.emit(separate("Dana", VOICED));
    recall.emit(separate("Ben", VOICED));
    assert.equal(userChunks(el).length, 0, "no audio may reach the model while closed");
  } finally {
    restore();
  }
});

test("gate_open replays the addresser's buffered sentence, then forwards live frames", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    // "Laura, what's the status" — the first half lands before the backend
    // has even detected the wake word.
    recall.emit(separate("Ben", VOICED));
    recall.emit(separate("Ben", VOICED));
    assert.equal(userChunks(el).length, 0);

    session.handleControl({ type: "gate_open", speaker: "Ben" });
    assert.equal(userChunks(el).length, 2, "the buffered ask is replayed whole");

    recall.emit(separate("Ben", VOICED));
    assert.equal(userChunks(el).length, 3, "live frames follow");
  } finally {
    restore();
  }
});

test("gate_open matches the buffer case-insensitively", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    recall.emit(separate("Ben Hattar", VOICED));
    session.handleControl({ type: "gate_open", speaker: "ben hattar" });
    assert.equal(userChunks(el).length, 1);
  } finally {
    restore();
  }
});

test("gate_open with an unknown speaker falls back to the live voiced speaker", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    recall.emit(separate("Ben", VOICED)); // sets currentSpeaker
    session.handleControl({ type: "gate_open", speaker: "Guest 2" });
    assert.equal(session.gateSpeaker, "Ben");
    assert.equal(userChunks(el).length, 1, "the addressed turn is never dropped");
  } finally {
    restore();
  }
});

// ─────────────── the fluency defects this suite exists for ───────────────

test("two people talk at once: only the person who addressed her is heard", async () => {
  // Ben: "Laura, what's the status of the API project?"
  // Dana (over the top, to Ben): "— did we ship the migration?"
  // Her turn belongs to Ben; Dana's overlap is buffered for Dana's own turn.
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    recall.emit(separate("Ben", VOICED));
    session.handleControl({ type: "gate_open", speaker: "Ben" });
    el.reset();

    recall.emit(separate("Dana", VOICED));
    assert.equal(userChunks(el).length, 0, "the overlapping voice is not her turn");
    recall.emit(separate("Ben", VOICED));
    assert.equal(userChunks(el).length, 1, "the addresser keeps the floor");
  } finally {
    restore();
  }
});

test("a second person naming her takes over the conversation", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    session.handleControl({ type: "gate_open", speaker: "Ben" });
    session.handleControl({ type: "gate_open", speaker: "Dana" });
    assert.equal(session.gateSpeaker, "Dana");
    el.reset();
    recall.emit(separate("Dana", VOICED));
    assert.equal(userChunks(el).length, 1);
  } finally {
    restore();
  }
});

// ── the conversational lock: follow-up without re-saying her name ──

test("the addresser's follow-up needs no wake word", async () => {
  // Duccio: "Laura, when is the deadline?"  ->  "Friday."
  // Duccio: "And who's on it?"              ->  she hears it.
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    recall.emit(separate("Duccio", VOICED));
    session.handleControl({ type: "gate_open", speaker: "Duccio" });
    await session.onELMessage({ data: JSON.stringify({ type: "agent_response_complete" }) });
    assert.ok(session.followupUntil > 0, "the floor stays open for her partner");
    el.reset();

    recall.emit(separate("Duccio", VOICED)); // "and who's on it?"
    assert.equal(userChunks(el).length, 1, "the follow-up reaches her");
  } finally {
    restore();
  }
});

test("the follow-up window belongs to her partner alone", async () => {
  // Ananth saying something to Duccio right after her answer is not a
  // follow-up — she drops out of the conversation instead of answering it.
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    session.handleControl({ type: "gate_open", speaker: "Duccio" });
    await session.onELMessage({ data: JSON.stringify({ type: "agent_response_complete" }) });
    el.reset();

    recall.emit(separate("Ananth", VOICED));
    assert.equal(userChunks(el).length, 0, "not her conversation");
    assert.equal(session.gateOpen, false, "a new speaker ends the follow-up");
  } finally {
    restore();
  }
});

test("the follow-up window expires", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    session.handleControl({ type: "gate_open", speaker: "Duccio" });
    await session.onELMessage({ data: JSON.stringify({ type: "agent_response_complete" }) });
    session.followupUntil = Date.now() - 1; // window elapsed
    el.reset();

    recall.emit(separate("Duccio", VOICED));
    assert.equal(session.gateOpen, false);
    assert.equal(userChunks(el).length, 0, "re-entering the conversation needs her name");
  } finally {
    restore();
  }
});

test("a follow-up turn is governed by the silence rule again, not the window", async () => {
  const { session, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    session.handleControl({ type: "gate_open", speaker: "Duccio" });
    await session.onELMessage({ data: JSON.stringify({ type: "agent_response_complete" }) });
    recall.emit(separate("Duccio", VOICED)); // he starts his follow-up
    assert.equal(session.followupUntil, 0, "back to an active ask");
    assert.equal(session.gateOpen, true);
  } finally {
    restore();
  }
});

test("gate_close drops her out of the conversation immediately", async () => {
  // Duccio: "Laura, which tasks are overdue?"  ->  "Three."
  // Duccio: "Ananth, can you take two?"  — the backend hears the vocative.
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    session.handleControl({ type: "gate_open", speaker: "Duccio" });
    await session.onELMessage({ data: JSON.stringify({ type: "agent_response_complete" }) });
    session.handleControl({ type: "gate_close" });
    el.reset();

    recall.emit(separate("Duccio", VOICED));
    assert.equal(session.gateOpen, false);
    assert.equal(userChunks(el).length, 0);
  } finally {
    restore();
  }
});

test("she stays interruptible through an answer longer than the silence timeout", async () => {
  // The addresser going quiet while she talks is them LISTENING. Closing the
  // gate there dropped their audio, so any answer longer than 5s could not be
  // interrupted at all — precisely when a human most wants to cut in.
  const { session, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    session.handleControl({ type: "gate_open", speaker: "Ben" });
    session.playheadMs = Date.now() + 10_000; // a long answer is playing
    session.gateVoiceAt = Date.now() - 8000; // he has been listening in silence
    recall.emit(separate("Ben", VOICED));
    assert.equal(session.gateOpen, true, "he can still cut in");
  } finally {
    restore();
  }
});

// ── bridge start-up ──

test("the bridge learns strict mode from its own bootstrap", async () => {
  // A Durable Object that starts (or restarts) OPEN in a room that is already
  // strict streams the room's own conversation to the agent until the human
  // count next changes — which may be never.
  const ctx = await makeSession({ bootstrap: { strict: true } });
  try {
    attachPage(ctx.session);
    const recall = await attachRecall(ctx.session);
    await settle();
    assert.equal(ctx.session.strictMode, true);
    await elReady(ctx.session, ctx.el);
    recall.emit(separate("Ben", VOICED));
    assert.equal(userChunks(ctx.el).length, 0, "gated from the first frame");
  } finally {
    ctx.restore();
  }
});

test("pre-connect audio reaches ElevenLabs before any live frame", async () => {
  // elReady used to flip before an awaited POST, so frames arriving during
  // that await overtook the buffered tail and the meeting's first sentence
  // arrived scrambled.
  const ctx = await makeSession();
  const { session, el, restore } = ctx;
  try {
    attachPage(session);
    const recall = await attachRecall(session);
    await settle();
    const early = frameOf(1000);
    session.preBuffer = [early];
    session.preBufferB64 = early.length;
    await session.onELMessage({
      data: JSON.stringify({ type: "conversation_initiation_metadata" }),
    });
    const chunks = userChunks(el);
    assert.equal(chunks[0].user_audio_chunk, early, "the tail goes out first");
  } finally {
    restore();
  }
});

// ── backchannel-safe barge-in ──

test("a backchannel while she answers does not cut her off", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    session.playheadMs = Date.now() + 5000; // her answer is playing
    for (let i = 0; i < 8; i++) recall.emit(separate("Ben", VOICED)); // "mhmm"
    for (let i = 0; i < 25; i++) recall.emit(separate("Ben", SILENT)); // burst over
    assert.equal(userChunks(el).length, 0, "she keeps her turn");
  } finally {
    restore();
  }
});

test("a real interruption gets through, with its first words intact", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    session.playheadMs = Date.now() + 5000;
    for (let i = 0; i < 40; i++) recall.emit(separate("Ben", VOICED));
    const got = userChunks(el).length;
    assert.ok(got >= 40, `the held burst is flushed whole (got ${got})`);
  } finally {
    restore();
  }
});

test("once she stops speaking, audio flows normally again", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    session.playheadMs = Date.now() + 5000;
    for (let i = 0; i < 5; i++) recall.emit(separate("Ben", VOICED));
    assert.equal(userChunks(el).length, 0);
    session.playheadMs = 0; // her answer finished
    recall.emit(separate("Ben", VOICED));
    assert.equal(userChunks(el).length, 1);
  } finally {
    restore();
  }
});

test("REGRESSION: speaker announcements leak human-to-human turns into the model", async () => {
  // While the gate is closed the room's private conversation must be invisible
  // to the agent. Every speaker change still ships a contextual_update, which
  // both grows the context unboundedly and tells the model about talk it is
  // meant never to know about.
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    for (let i = 0; i < 10; i++) {
      recall.emit(separate(i % 2 ? "Ben" : "Dana", VOICED));
    }
    assert.equal(
      contextUpdates(el).length,
      0,
      "no speaker updates while the gate is closed"
    );
  } finally {
    restore();
  }
});

test("REGRESSION: the mixed-audio fallback rung ignores strict mode entirely", async () => {
  // If the workspace flag for separate streams is off, Recall sends one mixed
  // room stream — and the whole strict branch lives under `if (separate)`, so
  // every word the room says reaches the model.
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    recall.emit(mixed(VOICED));
    recall.emit(mixed(VOICED));
    assert.equal(
      userChunks(el).length,
      0,
      "strict mode must hold on the mixed rung too"
    );
  } finally {
    restore();
  }
});

test("REGRESSION: pre-connect room chatter is flushed past a closed gate", async () => {
  // Frames buffered while ElevenLabs was connecting are flushed on ready with
  // no gate check. If strict mode was already signalled, that flush hands the
  // model ~1.2s of human-to-human talk and it answers a conversation nobody
  // addressed to it.
  const ctx = await makeSession();
  const { session, el, restore } = ctx;
  try {
    attachPage(session);
    const recall = await attachRecall(session);
    session.handleControl({ type: "mode", strict: true });
    // EL is not ready yet: these land in preBuffer.
    recall.emit(separate("Ben", VOICED));
    recall.emit(separate("Dana", VOICED));
    await settle();
    await elReady(session, el);
    assert.equal(
      userChunks(el).length,
      0,
      "the pre-connect tail must respect the gate"
    );
  } finally {
    restore();
  }
});

// ───────────────────── turn boundaries ─────────────────────

test("the gate closes after the addresser goes silent", async () => {
  const { session, el, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    session.handleControl({ type: "gate_open", speaker: "Ben" });
    session.gateVoiceAt = Date.now() - 6000; // 6s of silence
    el.reset();
    recall.emit(separate("Dana", VOICED));
    assert.equal(session.gateOpen, false);
    assert.equal(userChunks(el).length, 0);
  } finally {
    restore();
  }
});

test("silence frames from the addresser do not hold the gate open", async () => {
  const { session, recall, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    session.handleControl({ type: "gate_open", speaker: "Ben" });
    session.gateVoiceAt = Date.now() - 6000;
    recall.emit(separate("Ben", SILENT));
    assert.equal(session.gateOpen, false, "comfort noise is not speech");
  } finally {
    restore();
  }
});

// ───────────────────── stop / barge-in ─────────────────────

test("stop swallows the in-flight reply and interrupts the page", async () => {
  const { session, el, page, restore } = await liveSession();
  try {
    session.handleControl({ type: "stop" });
    assert.equal(session.dropResponse, true);
    assert.ok(page.json().some((m) => m.type === "interrupt"));
    page.reset();
    await session.onELMessage({
      data: JSON.stringify({ type: "audio", audio_event: { audio_base_64: VOICED } }),
    });
    assert.equal(
      page.json().filter((m) => m.type === "audio").length,
      0,
      "no audio reaches the room after a stop"
    );
  } finally {
    restore();
  }
});

test("stop disarms on the next user utterance", async () => {
  const { session, el, page, restore } = await liveSession();
  try {
    session.handleControl({ type: "stop" });
    await session.onELMessage({ data: JSON.stringify({ type: "user_transcript" }) });
    assert.equal(session.dropResponse, false);
    await session.onELMessage({
      data: JSON.stringify({ type: "audio", audio_event: { audio_base_64: VOICED } }),
    });
    assert.equal(page.json().filter((m) => m.type === "audio").length, 1);
  } finally {
    restore();
  }
});

test("REGRESSION: a stop in strict mode leaves the next answer swallowed", async () => {
  // In strict mode nothing reaches ElevenLabs while the gate is closed, so no
  // user_transcript ever arrives to disarm dropResponse. The next properly
  // addressed question is answered by the model and silently dropped by the
  // bridge — she goes mute for the rest of the meeting.
  const { session, page, restore } = await liveSession();
  try {
    session.handleControl({ type: "mode", strict: true });
    session.handleControl({ type: "stop" });
    session.handleControl({ type: "gate_open", speaker: "Ben" });
    assert.equal(
      session.dropResponse,
      false,
      "opening a new addressed turn must re-arm her voice"
    );
  } finally {
    restore();
  }
});

// ───────────────────── lifecycle ─────────────────────

test("a page reconnect does not tear the session down while Recall is attached", async () => {
  const { session, page, restore } = await liveSession();
  try {
    page.emitClose();
    assert.equal(session.el !== null, true, "EL stays up across a page blip");
  } finally {
    restore();
  }
});

test("agent_response_complete tells the page to close the mouth", async () => {
  const { session, page, restore } = await liveSession();
  try {
    await session.onELMessage({ data: JSON.stringify({ type: "agent_response_complete" }) });
    assert.ok(page.json().some((m) => m.type === "response_end"));
  } finally {
    restore();
  }
});
