// Test harness for the cedric-voice Durable Object.
//
// The bridge holds the fluency-critical logic of a multiparty meeting (the
// strict Director gate, per-speaker buffering, turn end, barge-in) and ran
// with ZERO automated coverage — every regression there was found live, in
// front of customers. This harness runs the real VoiceSession class in plain
// Node by standing in for the three things Workers gives it: WebSocketPair,
// fetch, and the environment bindings.
//
// Nothing here talks to Cloudflare, Recall, ElevenLabs or the backend.

import { VoiceSession } from "../worker.js";

/** Minimal EventTarget-shaped socket with a recording send(). */
export class FakeWS {
  constructor(name = "ws") {
    this.name = name;
    this.sent = [];
    this.closed = null;
    this._listeners = { message: [], close: [], error: [] };
    this.accepted = false;
  }
  accept() {
    this.accepted = true;
  }
  addEventListener(type, fn) {
    (this._listeners[type] ||= []).push(fn);
  }
  send(data) {
    this.sent.push(data);
  }
  close(code, reason) {
    this.closed = { code, reason };
    this._fire("close", {});
  }
  /** Deliver an inbound frame to the DO. */
  emit(data) {
    this._fire("message", { data: typeof data === "string" ? data : JSON.stringify(data) });
  }
  emitClose() {
    this._fire("close", {});
  }
  _fire(type, ev) {
    for (const fn of this._listeners[type] || []) fn(ev);
  }
  /** Parsed view of everything the DO sent on this socket. */
  json() {
    return this.sent.map((s) => {
      try {
        return JSON.parse(s);
      } catch (_) {
        return { _raw: s };
      }
    });
  }
  reset() {
    this.sent = [];
  }
}

/** base64 PCM16 frame whose mean |amplitude| lands above/below the voice gate. */
export function frame(opts = {}) {
  const { voiced = true, samples = 320 } =
    typeof opts === "number" ? { samples: opts } : opts;
  const amp = voiced ? 4000 : 20;
  const buf = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) {
    // Alternate sign so the mean of |x| is `amp` regardless of DC handling.
    buf.writeInt16LE(i % 2 === 0 ? amp : -amp, i * 2);
  }
  return buf.toString("base64");
}

export const SILENT = frame({ voiced: false });
export const VOICED = frame({ voiced: true });

/**
 * Spin up a VoiceSession with fake sockets already attached.
 *
 * Returns { session, recall, page, el, backend, connectEL } where `el` is the
 * fake ElevenLabs socket (null until connectEL resolves) and `backend` records
 * every HTTP call the DO made.
 */
export async function makeSession({
  bootstrap = {},
  cap = "cap-test",
  botName = "Laura",
  toolResult = { ok: true, result: { ok: true } },
} = {}) {
  const backend = { calls: [], toolResult };
  const elSocket = new FakeWS("el");

  const priorFetch = globalThis.fetch;
  const priorPair = globalThis.WebSocketPair;

  globalThis.WebSocketPair = function () {
    const client = new FakeWS("client");
    const server = new FakeWS("server");
    return { 0: client, 1: server };
  };

  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    backend.calls.push({
      url: u,
      method: init.method || "GET",
      body: init.body ? JSON.parse(init.body) : null,
    });
    if (u.includes("/internal/voice-agent/bootstrap/")) {
      const cfg = {
        enabled: true,
        bot_id: "bot-1",
        bot_name: botName,
        signed_url: "wss://el.example/convai",
        init: { type: "conversation_initiation_client_data" },
        ...bootstrap,
      };
      return { ok: true, json: async () => cfg };
    }
    if (u.startsWith("https://el.example/")) {
      return { webSocket: elSocket };
    }
    if (u.includes("/internal/voice-agent/tool/")) {
      return { ok: true, json: async () => backend.toolResult };
    }
    // lifecycle events
    return { ok: true, json: async () => ({ ok: true }) };
  };

  const env = {
    BACKEND_URL: "https://backend.test",
    BACKEND_BEARER: "token",
    VOICE_SESSION: null,
  };
  const session = new VoiceSession({}, env);
  session.cap = cap;

  const restore = () => {
    globalThis.fetch = priorFetch;
    globalThis.WebSocketPair = priorPair;
  };

  return { session, el: elSocket, backend, env, restore };
}

/** Attach a Recall ingress socket and let connectEL settle. */
export async function attachRecall(session) {
  const ws = new FakeWS("recall");
  session.attachRecall(ws);
  await tick();
  return ws;
}

export function attachPage(session) {
  const ws = new FakeWS("page");
  session.attachPage(ws);
  return ws;
}

/** Bring ElevenLabs to the ready state (the DO's own handshake). */
export async function elReady(session, el) {
  await session.onELMessage({
    data: JSON.stringify({ type: "conversation_initiation_metadata" }),
  });
  await tick();
  el.reset();
}

/** A Recall `audio_separate_raw` frame from a named participant. */
export function separate(name, b64, extra = {}) {
  return {
    event: "audio_separate_raw.data",
    data: { data: { buffer: b64, participant: { name, is_bot: false, ...extra } } },
  };
}

/** A Recall `audio_mixed_raw` frame (the fallback rung). */
export function mixed(b64) {
  return { event: "audio_mixed_raw.data", data: { data: { buffer: b64 } } };
}

/** Audio chunks the DO forwarded to ElevenLabs. */
export function userChunks(el) {
  return el.json().filter((m) => m.user_audio_chunk !== undefined);
}

export function contextUpdates(el) {
  return el.json().filter((m) => m.type === "contextual_update");
}

export const tick = () => new Promise((r) => setImmediate(r));

/** Advance the DO's notion of "now" without sleeping. */
export function withClock(fn) {
  const realNow = Date.now;
  let offset = 0;
  Date.now = () => realNow.call(Date) + offset;
  const advance = (ms) => {
    offset += ms;
  };
  try {
    return fn(advance);
  } finally {
    Date.now = realNow;
  }
}
