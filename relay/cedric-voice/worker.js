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
    // Audio ingress: with audio_separate_raw Recall opens ONE WS PER
    // PARTICIPANT (born on unmute, torn down on mute) — a Set, never a
    // single socket. The mixed-audio fallback rung is just a Set of one.
    this.recallSockets = new Set();
    this.botName = ""; // from bootstrap: defensive self-stream filter
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
    this.lastUserChunkAt = 0; // last time we forwarded VOICED user audio
    this.inResponse = false; // between first audio chunk and next user audio
    this.tUserTranscript = 0; // EL finalized the user's utterance
    this.tAgentResponse = 0; // EL produced the reply text (pre-TTS)
    this.tFirstChunk = 0; // first audio chunk of the current reply
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
    const bye = () => {
      if (this.page !== ws) return;
      this.page = null;
      // The page lives exactly as long as the bot's browser: page gone AND
      // no ingress sockets = the meeting is over. (Sockets-empty alone is a
      // silent/all-muted room — stay alive; a transient page drop reconnects
      // with backoff and re-attaches.)
      if (this.recallSockets.size === 0) this.teardown("closed");
    };
    ws.addEventListener("close", bye);
    ws.addEventListener("error", bye);
  }

  sendPage(obj) {
    try { this.page?.send(JSON.stringify(obj)); } catch (_) {}
  }

  // ── Recall (audio ingress) ──────────────────────────────────────────
  attachRecall(ws) {
    this.recallSockets.add(ws);
    ws.addEventListener("message", (e) => this.onRecallMessage(e));
    const bye = () => {
      this.recallSockets.delete(ws);
      // Separate streams churn per unmute — a socket closing is routine.
      // Meeting-over is "no ingress AND no page".
      if (this.recallSockets.size === 0 && !this.page) this.teardown("closed");
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
    const separate = ev.event === "audio_separate_raw.data";
    if (!separate && ev.event !== "audio_mixed_raw.data") return;
    const inner = (ev.data && ev.data.data) || {};
    const buf = inner.buffer;
    if (!buf) return;
    if (separate) {
      // Per-participant mic stream: the bot's own output is not a stream, so
      // no self-hearing — NO half-duplex gate, and voice barge-in just works
      // (audio keeps flowing while he speaks; EL interruption handles it).
      // Defensive self-filter anyway, by the bot's display name.
      const p = inner.participant || (ev.data && ev.data.participant) || {};
      const pname = String(p.name || "").trim().toLowerCase();
      if (p.is_bot === true) return;
      if (this.botName && pname && pname === this.botName.toLowerCase()) return;
    } else {
      // Mixed fallback rung (workspace flag off): the room mix contains his
      // own voice while the answer plays — half-duplex gate stays. 800ms
      // grace covers the page's viseme segment buffering.
      if (Date.now() < this.playheadMs + 800) return;
    }
    this.frames++;
    if (this.frames === 1) {
      console.log("first recall frame (" + (separate ? "separate" : "mixed") + ")");
    }
    if (this.el && this.elReady) {
      try {
        this.el.send(JSON.stringify({ user_audio_chunk: buf }));
        // Streams flow CONTINUOUSLY (silence included), so "last chunk" is
        // meaningless for turn timing — anchor to frames that carry VOICE.
        // Cheap energy probe: mean |amplitude| over every 16th sample.
        if (this.frameHasVoice(buf)) {
          this.lastUserChunkAt = Date.now();
          this.inResponse = false; // the human has the floor again
        }
      } catch (_) {}
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
    this.botName = String(cfg.bot_name || "");
    try { el.send(JSON.stringify(cfg.init)); } catch (_) {}
    el.addEventListener("message", (e) => this.onELMessage(e));
    const dead = async () => {
      if (this.el !== el) return;
      this.el = null;
      this.elReady = false;
      // EL dying while the meeting is still live = mid-meeting failure →
      // the backend flips the voice back to the legacy brain.
      if (this.recallSockets.size > 0 || this.page) await this.postEvent("failed");
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
    if (t === "user_transcript") {
      // Timestamp only (the transcript CONTENT stays on the Recall path):
      // this is EL's own "the user finished saying something" moment — the
      // clean anchor the energy probe can't give (far-field echo through the
      // speaker's mic keeps re-stamping it).
      this.tUserTranscript = Date.now();
      return;
    }
    if (t === "agent_response_complete") {
      if (this.tFirstChunk) {
        console.log("stage_response_stream_ms=" + (Date.now() - this.tFirstChunk));
      }
      return;
    }
    if (t === "audio") {
      const b64 = msg.audio_event && msg.audio_event.audio_base_64;
      if (!b64) return;
      this.chunks++;
      if (this.chunks === 1) console.log("first agent audio chunk");
      // Per-turn stage decomposition (numbers only, never content):
      //   stage_transcript_to_audio_ms = EL turn-close + LLM + TTS
      //   stage_response_to_audio_ms   = TTS share (text ready -> first audio)
      //   turn_latency_ms              = voiced-frame anchor (echo-noisy)
      if (!this.inResponse) {
        this.inResponse = true;
        const now = Date.now();
        this.tFirstChunk = now;
        if (this.lastUserChunkAt) {
          console.log("turn_latency_ms=" + (now - this.lastUserChunkAt));
        }
        if (this.tUserTranscript) {
          console.log("stage_transcript_to_audio_ms=" + (now - this.tUserTranscript));
        }
        if (this.tAgentResponse) {
          console.log("stage_response_to_audio_ms=" + (now - this.tAgentResponse));
        }
      }
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
    if (t === "client_tool_call") {
      // Laura owns knowledge + actions: relay the call to the backend and
      // hand the JSON back as the tool result. Fire-and-forget so the audio
      // pumps never wait on a tool.
      this.handleToolCall(msg.client_tool_call || {});
      return;
    }
    if (t === "agent_response") {
      this.tAgentResponse = Date.now(); // reply TEXT ready (pre-TTS anchor)
      // The agent's SPOKEN words never pass through the backend under this
      // runtime (no _make_avatar_speak dispatch), so without this the meeting
      // transcript/artifact loses everything HE said (live bug 2026-07-24:
      // owner saw only the human lines). Forward the text to the transcript
      // recorder — content goes to the designed PII store, never to logs.
      const said =
        (msg.agent_response_event && msg.agent_response_event.agent_response) || "";
      if (said) this.postEventBody({ type: "agent_said", text: String(said) });
      return;
    }
    // user_transcript / vad_score …: user transcripts stay on the
    // Recall→backend path (speaker labels live there); nothing is logged here.
  }

  async postEventBody(body) {
    try {
      await fetch(`${this.env.BACKEND_URL}/internal/voice-agent/event/${this.cap}`, {
        method: "POST",
        headers: {
          Authorization: "Bearer " + this.env.BACKEND_BEARER,
          "content-type": "application/json",
        },
        body: JSON.stringify(body),
      });
    } catch (_) {}
  }

  async handleToolCall(call) {
    const id = call.tool_call_id || "";
    const name = call.tool_name || "";
    console.log("tool call: " + name); // name only — parameters are meeting content
    let result = "";
    let isError = false;
    try {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 12000);
      const r = await fetch(
        `${this.env.BACKEND_URL}/internal/voice-agent/tool/${this.cap}`,
        {
          method: "POST",
          headers: {
            Authorization: "Bearer " + this.env.BACKEND_BEARER,
            "content-type": "application/json",
          },
          body: JSON.stringify({
            tool_name: name,
            parameters: call.parameters || {},
            tool_call_id: id,
          }),
          signal: ctl.signal,
        }
      );
      clearTimeout(timer);
      const body = await r.json().catch(() => ({}));
      if (!r.ok || !body.ok) {
        isError = true;
        result = JSON.stringify({ error: (body && body.error) || "tool failed" });
      } else {
        result = JSON.stringify(body.result);
      }
    } catch (_) {
      // Timeout/unreachable: an honest error beats a silent hang — the agent
      // says "I'll check" instead of fabricating a result.
      isError = true;
      result = JSON.stringify({ error: "tool timeout" });
    }
    try {
      this.el?.send(
        JSON.stringify({
          type: "client_tool_result",
          tool_call_id: id,
          result,
          is_error: isError,
        })
      );
    } catch (_) {}
  }

  frameHasVoice(b64) {
    // Mean |amplitude| over every 16th s16le sample — enough to tell speech
    // from comfort noise without decoding cost mattering (~200 samples/frame).
    let raw;
    try { raw = atob(b64); } catch (_) { return false; }
    const n = raw.length >> 1;
    if (!n) return false;
    let sum = 0, count = 0;
    for (let i = 0; i < n; i += 16) {
      let v = raw.charCodeAt(2 * i) | (raw.charCodeAt(2 * i + 1) << 8);
      if (v >= 0x8000) v -= 0x10000;
      sum += v < 0 ? -v : v;
      count++;
    }
    return count > 0 && sum / count > 260; // ~-40dBFS: speech, not room hiss
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
    for (const ws of this.recallSockets) {
      try { ws.close(1000, "session over"); } catch (_) {}
    }
    this.recallSockets.clear();
    this.sendPage({ type: "interrupt" });
    this.preBuffer = [];
    this.preBufferB64 = 0;
    this.playheadMs = 0;
  }
}
