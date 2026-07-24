// cedric-voice — ElevenLabs Agent bridge (Cedric pilot).
//
// One Durable Object per meeting session (keyed by the per-bot capability)
// ties together the three sockets a stateless Worker can never hold at once:
//
//   Recall  --audio_mixed_raw(WS)-->  [VoiceSession DO]  --WS--> ElevenLabs Agent
//                                            |
//                                            +--WS--> avatar page (/talk) audio out
//                                            +--HTTP--> backend /internal/voice-agent/*
//
// The backend owns auth + config: the DO bootstraps by exchanging its
// capability for a short-lived ElevenLabs signed URL plus the per-meeting
// conversation-init payload (prompt override, dynamic variables). No secrets
// live here beyond the backend bearer; the ElevenLabs API key never reaches
// this worker.
//
// HALF-DUPLEX (pilot 1): while the agent's audio is playing in the meeting,
// Recall ingress is DROPPED — Recall streams the room's MIXED audio, which
// includes Cedric's own voice, and an agent that hears itself self-interrupts
// forever. Cost: no voice barge-in while he speaks (answers are ≤200 tokens,
// so windows are short). PR 3 revisits with speaker-labeled gating.
//
// PII: audio and transcripts NEVER appear in logs — counters only.

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.headers.get("Upgrade") !== "websocket") {
      return new Response("cedric-voice bridge up", { status: 200 });
    }
    let role = null;
    let m = url.pathname.match(/^\/voice\/([^/]+)$/);
    if (m) role = "recall";
    else if ((m = url.pathname.match(/^\/voice-out\/([^/]+)$/))) role = "page";
    if (!role) return new Response("bad path", { status: 400 });
    const cap = m[1];
    const id = env.VOICE_SESSION.idFromName(cap);
    const stub = env.VOICE_SESSION.get(id);
    // Forward the upgrade to the session object; role+cap ride the query.
    const fwd = new URL("https://do/session");
    fwd.searchParams.set("role", role);
    fwd.searchParams.set("cap", cap);
    return stub.fetch(fwd.toString(), req);
  },
};

const PREBUFFER_MAX_B64 = 130_000; // ~3s of 16k s16le pre-EL-connect audio

export class VoiceSession {
  constructor(state, env) {
    this.env = env;
    this.cap = "";
    this.recall = null; // WS from Recall (audio ingress)
    this.page = null; // WS to the avatar page (audio egress)
    this.el = null; // WS to ElevenLabs
    this.elReady = false; // conversation_initiation_metadata seen
    this.started = false; // backend told voice_owner=elevenlabs
    this.connecting = false;
    this.preBuffer = []; // base64 chunks queued while EL connects
    this.preBufferB64 = 0;
    this.playheadMs = 0; // when the agent's queued audio finishes playing
    this.frames = 0;
    this.chunks = 0;
  }

  async fetch(req) {
    const url = new URL(req.url);
    const role = url.searchParams.get("role");
    this.cap = url.searchParams.get("cap") || this.cap;
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();
    if (role === "page") this.attachPage(server);
    else this.attachRecall(server);
    return new Response(null, { status: 101, webSocket: client });
  }

  // ── avatar page (audio egress) ──────────────────────────────────────
  attachPage(ws) {
    try { this.page?.close(1000, "replaced"); } catch (_) {}
    this.page = ws;
    ws.addEventListener("close", () => { if (this.page === ws) this.page = null; });
    ws.addEventListener("error", () => { if (this.page === ws) this.page = null; });
  }

  sendPage(obj) {
    try { this.page?.send(JSON.stringify(obj)); } catch (_) {}
  }

  // ── Recall (audio ingress) ──────────────────────────────────────────
  attachRecall(ws) {
    try { this.recall?.close(1000, "replaced"); } catch (_) {}
    this.recall = ws;
    ws.addEventListener("message", (e) => this.onRecallMessage(e));
    const bye = () => {
      if (this.recall !== ws) return;
      this.recall = null;
      this.teardown("closed"); // meeting over (or Recall re-dialing)
    };
    ws.addEventListener("close", bye);
    ws.addEventListener("error", bye);
    if (!this.el && !this.connecting) this.connectEL();
  }

  onRecallMessage(e) {
    let ev;
    try {
      ev = JSON.parse(typeof e.data === "string" ? e.data : new TextDecoder().decode(e.data));
    } catch (_) { return; }
    if (ev.event !== "audio_mixed_raw.data") return;
    const buf = ev.data && ev.data.data && ev.data.data.buffer;
    if (!buf) return;
    this.frames++;
    if (this.frames === 1) console.log("first recall frame");
    // Half-duplex: the room's mixed audio contains Cedric's own voice while
    // his answer plays — never let the agent hear itself. 800ms grace (was
    // 300): the page now buffers ~1s segments for viseme lip-sync, so real
    // playback ends later than this byte-clock estimate.
    if (Date.now() < this.playheadMs + 800) return;
    if (this.el && this.elReady) {
      try { this.el.send(JSON.stringify({ user_audio_chunk: buf })); } catch (_) {}
    } else {
      // EL still connecting: keep the tail so his first addressed sentence
      // isn't clipped; drop oldest beyond ~3s.
      this.preBuffer.push(buf);
      this.preBufferB64 += buf.length;
      while (this.preBufferB64 > PREBUFFER_MAX_B64 && this.preBuffer.length) {
        this.preBufferB64 -= this.preBuffer.shift().length;
      }
    }
  }

  // ── ElevenLabs ──────────────────────────────────────────────────────
  async connectEL() {
    this.connecting = true;
    let cfg;
    try {
      const r = await fetch(
        `${this.env.BACKEND_URL}/internal/voice-agent/bootstrap/${this.cap}`,
        { headers: { Authorization: "Bearer " + this.env.BACKEND_BEARER } }
      );
      cfg = r.ok ? await r.json() : null;
    } catch (_) { cfg = null; }
    if (!cfg || !cfg.enabled || !cfg.signed_url) {
      console.log("bootstrap: legacy (" + (cfg && cfg.reason ? cfg.reason : "unreachable") + ")");
      this.connecting = false;
      this.teardown("closed"); // bot simply runs the legacy path
      return;
    }
    let el;
    try {
      const resp = await fetch(cfg.signed_url.replace(/^wss:/, "https:"), {
        headers: { Upgrade: "websocket" },
      });
      el = resp.webSocket;
      if (!el) throw new Error("no ws");
      el.accept();
    } catch (_) {
      console.log("el-connect failed");
      this.connecting = false;
      await this.postEvent("failed");
      this.teardown(null);
      return;
    }
    this.el = el;
    this.connecting = false;
    try { el.send(JSON.stringify(cfg.init)); } catch (_) {}
    el.addEventListener("message", (e) => this.onELMessage(e));
    const dead = async () => {
      if (this.el !== el) return;
      this.el = null;
      this.elReady = false;
      // EL dying while the meeting is still live = mid-meeting failure →
      // the backend flips the voice back to the legacy brain.
      if (this.recall) await this.postEvent("failed");
      this.sendPage({ type: "interrupt" });
    };
    el.addEventListener("close", dead);
    el.addEventListener("error", dead);
  }

  async onELMessage(e) {
    let msg;
    try {
      msg = JSON.parse(typeof e.data === "string" ? e.data : new TextDecoder().decode(e.data));
    } catch (_) { return; }
    const t = msg.type;
    if (t === "conversation_initiation_metadata") {
      this.elReady = true;
      console.log("el ready");
      await this.postEvent("started");
      this.started = true;
      // Flush the pre-connect tail so an early "Cedric, …" isn't clipped.
      for (const b of this.preBuffer.splice(0)) {
        try { this.el.send(JSON.stringify({ user_audio_chunk: b })); } catch (_) {}
      }
      this.preBufferB64 = 0;
      return;
    }
    if (t === "ping") {
      const id = msg.ping_event && msg.ping_event.event_id;
      try { this.el.send(JSON.stringify({ type: "pong", event_id: id })); } catch (_) {}
      return;
    }
    if (t === "audio") {
      const b64 = msg.audio_event && msg.audio_event.audio_base_64;
      if (!b64) return;
      this.chunks++;
      if (this.chunks === 1) console.log("first agent audio chunk");
      // pcm_16000 s16le: 32 bytes/ms. Track when playback will END so the
      // half-duplex gate re-opens right after he goes quiet.
      const ms = Math.floor((b64.length * 3) / 4 / 32);
      const now = Date.now();
      this.playheadMs = Math.max(this.playheadMs, now) + ms;
      // Character-level alignment rides along: the page rebuilds word timings
      // from it and runs the REAL TalkingHead viseme pipeline (same one the
      // legacy voice uses) instead of the amplitude fallback. Never logged.
      this.sendPage({
        type: "audio",
        data: b64,
        align: (msg.audio_event && msg.audio_event.alignment) || null,
      });
      return;
    }
    if (t === "interruption") {
      this.playheadMs = 0; // gate open immediately; the human has the floor
      this.sendPage({ type: "interrupt" });
      return;
    }
    // user_transcript / agent_response / vad_score …: transcripts stay on the
    // Recall→backend path (speaker labels live there); nothing is logged here.
  }

  async postEvent(type) {
    try {
      await fetch(`${this.env.BACKEND_URL}/internal/voice-agent/event/${this.cap}`, {
        method: "POST",
        headers: {
          Authorization: "Bearer " + this.env.BACKEND_BEARER,
          "content-type": "application/json",
        },
        body: JSON.stringify({ type }),
      });
    } catch (_) {}
  }

  teardown(event) {
    if (event && this.started) this.postEvent(event);
    this.started = false;
    this.elReady = false;
    try { this.el?.close(1000, "session over"); } catch (_) {}
    this.el = null;
    try { this.recall?.close(1000, "session over"); } catch (_) {}
    this.recall = null;
    this.sendPage({ type: "interrupt" });
    this.preBuffer = [];
    this.preBufferB64 = 0;
    this.playheadMs = 0;
  }
}
