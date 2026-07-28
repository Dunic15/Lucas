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
// TURN-TAKING lives here, and it is the whole product on a group call:
//   * strict mode (≥2 humans) — nothing reaches the model unless the room is
//     talking TO her; human-to-human talk dies in this file, by construction.
//   * the conversational lock — being named is how you ENTER the conversation;
//     the person she just answered can keep talking to her without the name
//     until someone else takes the floor, they turn to a colleague, or the
//     follow-up window elapses.
//   * backchannel-safe barge-in — "mhmm" does not cut her off, a real
//     turn-grab does.
// HALF-DUPLEX applies only to the MIXED-audio fallback rung, where Recall
// sends the room mix (her own voice included) and an agent that hears itself
// self-interrupts forever; on per-participant streams her output is not a
// stream, so barge-in works.
//
// Tests: relay/cedric-voice/test/  (node --test "relay/cedric-voice/test/*.test.mjs")
// PII: audio and transcripts NEVER appear in logs — counters only.

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const ctrl = url.pathname.match(/^\/control\/([^/]+)$/);
    if (ctrl && req.method === "POST") {
      return handleControl(req, env, ctrl[1]);
    }
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

// Meeting-Director control plane (backend -> DO, plain POST — the backend
// owns wake detection on its transcript stream; the DO enforces). The
// unguessable capability in the path is the auth, same as the audio routes.
async function handleControl(req, env, cap) {
  const id = env.VOICE_SESSION.idFromName(cap);
  const stub = env.VOICE_SESSION.get(id);
  const fwd = new URL("https://do/control");
  fwd.searchParams.set("cap", cap);
  return stub.fetch(fwd.toString(), req);
}

// ~1.2s of pre-EL-connect audio. Was ~3s: replaying a long tail of startup
// room chatter made the agent answer conversations that predated him
// (live 2026-07-24, unsolicited "scheduling challenges" reply).
const PREBUFFER_MAX_B64 = 52_000;

// ── conversational lock (owner spec 2026-07-28) ──────────────────────────
// The strict gate is how she ENTERS a conversation, not a toll she charges on
// every sentence. Before this, `agent_response_complete` slammed the gate shut,
// so the natural second beat of a dialogue —
//     "Laura, when is the deadline?"  "Friday."  "And who's on it?"
// — never reached her: the room had to re-say her name for every single line,
// which is exactly what made her read as a voice assistant instead of a
// participant. After an answer the gate now stays open for the person she was
// talking to, and to them only. It shuts the moment the conversation stops
// being with her: someone else takes the floor, she is silent past the window,
// or the backend hears a vocative aimed at another human ("Ananth, can you
// take two?" → /control gate_close).
const FOLLOWUP_WINDOW_MS = 12_000;
// Turn end WITHIN an ask (she has not answered yet): the addresser trailing off
// for this long ends their turn.
const GATE_SILENCE_MS = 5000;
// Per-speaker ring buffer: what gets replayed when the gate opens. It has to
// cover the WHOLE ask, because the name is often at the END of it ("what's the
// status on the API, Laura?") and the wake round trip (Deepgram endpointing →
// webhook → control POST) spends most of a second on top of that. At 2.5s a
// normal-length question reached the agent with its opening missing, which
// sounds like her answering something nobody asked. 4s of s16le@16k base64 is
// ~170 KB per speaker — nothing against the DO's memory.
const SPEAKER_BUFFER_MS = 4000;
// ── backchannel-safe barge-in ────────────────────────────────────────────
// "Mhmm", "sì", "ok" while she is answering are the room LISTENING, not
// interrupting — cutting her off there is the classic voice-assistant tell.
// A real interruption is longer, so while her audio is playing a voiced burst
// is HELD: past BARGE_MIN_VOICED_MS it is a genuine turn-grab and the whole
// held burst is flushed (nothing is lost, it just lands ~0.5s later); if the
// burst dies inside BARGE_GAP_MS of silence first, it was a backchannel and is
// dropped so she keeps talking.
const BARGE_MIN_VOICED_MS = 500;
const BARGE_GAP_MS = 400;
// s16le @ 16 kHz = 32 bytes per millisecond; base64 is 4 chars per 3 bytes.
const b64Ms = (b64) => Math.floor((b64.length * 3) / 4 / 32);
// Speaker key for the mixed-audio fallback rung, which carries no participant
// labels. Never collides with a display name.
const MIXED_KEY = "\u0000mixed";

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
    this.currentSpeaker = ""; // last VOICED participant (separate streams)
    // ── Meeting Director gate (owner plan P1) ──
    // strictMode: humans >= 2 (backend tells us via /control). While strict,
    // audio reaches ElevenLabs ONLY during an addressed turn: the backend
    // detects the wake word on its transcript stream and opens the gate;
    // the DO replays the addresser's buffered sentence and forwards their
    // frames until the turn ends. Human-to-human talk NEVER reaches the
    // model — silence is enforced, not requested.
    this.strictMode = false;
    this.gateOpen = false;
    this.gateSpeaker = ""; // WHO she is in conversation with (the partner)
    this.gateVoiceAt = 0; // last voiced frame of the gate speaker
    this.followupUntil = 0; // >0 = she answered; partner may follow up un-named
    // Tunable live from the Cloudflare dashboard (Settings → Variables) without
    // a code deploy — this is the one number the room's feel is most sensitive
    // to, and it wants a real meeting to settle.
    this.followupWindowMs =
      Number(env && env.FOLLOWUP_WINDOW_MS) > 0
        ? Number(env.FOLLOWUP_WINDOW_MS)
        : FOLLOWUP_WINDOW_MS;
    this.dropResponse = false; // stop command: swallow the in-flight reply
    this.speakerBuffers = new Map(); // name -> [{b64, at}] last ~2.5s each
    // Backchannel-safe barge-in state (see BARGE_MIN_VOICED_MS).
    this.bargeHold = [];
    this.bargeVoicedMs = 0;
    this.bargeSilentMs = 0;
    this.bargeThrough = false; // this burst already qualified as a real barge-in
  }

  async fetch(req) {
    const url = new URL(req.url);
    this.cap = url.searchParams.get("cap") || this.cap;
    if (url.pathname === "/control") {
      let body = {};
      try { body = await req.json(); } catch (_) {}
      this.handleControl(body || {});
      return new Response(JSON.stringify({ ok: true }), {
        headers: { "content-type": "application/json" },
      });
    }
    const role = url.searchParams.get("role");
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();
    if (role === "page") this.attachPage(server);
    else this.attachRecall(server);
    return new Response(null, { status: 101, webSocket: client });
  }

  handleControl(body) {
    const t = body.type;
    if (t === "mode") {
      this.strictMode = body.strict === true;
      if (!this.strictMode) { this.closeGate("open mode"); this.speakerBuffers.clear(); }
      console.log("director mode: " + (this.strictMode ? "strict" : "open"));
      return;
    }
    if (t === "gate_open") {
      // The backend heard the wake word. The addresser is whoever the
      // backend attributed (fall back to the currently voiced speaker).
      let speaker = String(body.speaker || "") || this.currentSpeaker;
      // The backend's resolved identity can spell the name differently from
      // Recall's frame label (relabels, "Guest 2" merges). Match buffers
      // case-insensitively; if still unknown, trust the live voiced speaker
      // — dropping an addressed turn is the one failure strict mode must
      // never have.
      if (speaker && !this.speakerBuffers.has(speaker)) {
        const lower = speaker.toLowerCase();
        for (const k of this.speakerBuffers.keys()) {
          if (k.toLowerCase() === lower) { speaker = k; break; }
        }
        if (!this.speakerBuffers.has(speaker) && this.currentSpeaker) {
          speaker = this.currentSpeaker;
        }
      }
      this.gateOpen = true;
      this.gateSpeaker = speaker;
      this.gateVoiceAt = Date.now();
      // A fresh addressed turn: this is an ASK, not the follow-up beat.
      this.followupUntil = 0;
      // Re-arm her voice. A "Laura, stop" in strict mode leaves dropResponse
      // set, and the usual disarm (EL's next user_transcript) can never fire
      // because the closed gate forwards no audio — so without this she stayed
      // mute for the REST of the meeting, answering internally and having every
      // reply swallowed here.
      this.dropResponse = false;
      console.log("gate open");
      // Replay the addresser's buffered sentence: the wake word is detected
      // AFTER the phrase started — without this the agent hears half an ask.
      // On the mixed rung there are no per-speaker labels, so the room buffer
      // is the only thing there is to replay.
      const key = this.speakerBuffers.has(speaker) ? speaker : MIXED_KEY;
      const buf = this.speakerBuffers.get(key) || [];
      this.speakerBuffers.delete(key);
      if (this.el && this.elReady) {
        for (const fr of buf) this.sendEL({ user_audio_chunk: fr.b64 });
      }
      return;
    }
    if (t === "gate_close") {
      // The backend heard the conversation leave her: a vocative aimed at
      // another human ("Ananth, can you take two?"). She must drop out of the
      // dialogue THAT sentence, not 12 seconds later.
      this.closeGate("addressed elsewhere");
      return;
    }
    if (t === "stop") {
      // "Cedric, stop/shut up": kill the in-flight reply at the SOURCE —
      // the page flush alone left EL streaming the rest (fragments).
      this.dropResponse = true;
      this.closeGate("stop");
      this.sendPage({ type: "interrupt" });
      return;
    }
  }

  closeGate(why) {
    if (!this.gateOpen && !this.followupUntil) return;
    this.gateOpen = false;
    this.followupUntil = 0;
    this.gateSpeaker = "";
    this.resetBarge();
    console.log("gate closed (" + why + ")");
  }

  gateTick() {
    if (!this.gateOpen) return;
    const now = Date.now();
    if (this.followupUntil) {
      // She has answered and holds the floor open for her partner. Silence is
      // EXPECTED here (they are thinking), so the mid-ask silence rule must not
      // apply — only the window itself ends it.
      if (now > this.followupUntil) this.closeGate("follow-up expired");
      return;
    }
    // While SHE is talking, the addresser being silent is them listening, not
    // their turn ending — closing here dropped their audio and made her
    // uninterruptible for any answer longer than GATE_SILENCE_MS, which is
    // exactly when a human most wants to cut in.
    if (now < this.playheadMs) return;
    // Mid-ask: the addresser trailed off and never finished. Sentence
    // fragments keep the gate alive via gateVoiceAt.
    if (now - this.gateVoiceAt > GATE_SILENCE_MS) this.closeGate("silence");
  }

  /** Keep the last SPEAKER_BUFFER_MS of a speaker so their ask can be replayed
   *  whole once the backend detects the name buried mid-sentence. */
  bufferFrame(name, b64) {
    if (!name) return;
    const q = this.speakerBuffers.get(name) || [];
    q.push({ b64, at: Date.now() });
    while (q.length && Date.now() - q[0].at > SPEAKER_BUFFER_MS) q.shift();
    this.speakerBuffers.set(name, q);
  }

  // ── backchannel-safe barge-in ───────────────────────────────────────
  /** True when the frame was withheld from ElevenLabs (caller must return). */
  holdBarge(b64, voiced) {
    if (Date.now() >= this.playheadMs) {
      // She is not speaking: normal conversation, nothing to protect.
      this.resetBarge();
      return false;
    }
    if (this.bargeThrough) return false; // already a confirmed interruption
    if (voiced) {
      this.bargeVoicedMs += b64Ms(b64);
      this.bargeSilentMs = 0;
      this.bargeHold.push(b64);
      if (this.bargeVoicedMs >= BARGE_MIN_VOICED_MS) {
        // A real turn-grab: hand over everything we held so the interruption
        // carries its own first words, then go back to live forwarding.
        this.bargeThrough = true;
        for (const b of this.bargeHold.splice(0)) this.sendEL({ user_audio_chunk: b });
        this.bargeVoicedMs = 0;
        return true;
      }
      return true;
    }
    if (!this.bargeHold.length) return true; // her own answer, room quiet
    this.bargeSilentMs += b64Ms(b64);
    this.bargeHold.push(b64);
    if (this.bargeSilentMs > BARGE_GAP_MS) {
      // The burst ended before it became a sentence: "mhmm" / "sì" / "ok".
      // Drop it — she keeps her turn.
      this.resetBarge();
    }
    return true;
  }

  resetBarge() {
    this.bargeHold = [];
    this.bargeVoicedMs = 0;
    this.bargeSilentMs = 0;
    this.bargeThrough = false;
  }

  sendEL(obj) {
    try { this.el?.send(JSON.stringify(obj)); } catch (_) {}
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
      const rawName = String(p.name || "").trim();
      const pname = rawName.toLowerCase();
      if (p.is_bot === true) return;
      if (this.botName && pname && pname === this.botName.toLowerCase()) return;
      // SPEAKER IDENTITY (owner plan P2, live bug: "what's my name?" got the
      // wrong participant): the stream itself tells us WHO this voice is —
      // tell the agent whenever the voiced speaker changes. Names only, no
      // transcript content.
      const voiced = this.frameHasVoice(buf);
      // WHO is speaking — tracked ALWAYS, announced only when she is in the
      // conversation (below). The gate_open fallback leans on this to rescue a
      // turn whose speaker label the backend spelled differently, so it has to
      // stay current even while she is shut out of the room's own talk.
      const speakerChanged = voiced && rawName && rawName !== this.currentSpeaker;
      if (speakerChanged) this.currentSpeaker = rawName;
      // ── STRICT GATE (owner plan P1) ──
      if (this.strictMode) {
        this.gateTick();
        if (!this.gateOpen) {
          // Not an addressed turn: buffer ~2.5s per speaker (so the wake
          // sentence can be replayed whole when the backend opens the gate)
          // and forward NOTHING. Human-to-human talk dies here.
          this.bufferFrame(rawName, buf);
          return;
        }
        if (this.gateSpeaker && rawName && rawName !== this.gateSpeaker) {
          if (voiced && this.followupUntil) {
            // Her partner answered and SOMEONE ELSE took the floor — the
            // conversation is no longer with her. Drop out of it now instead
            // of holding the follow-up window open over the room's own talk.
            this.closeGate("new speaker");
            this.bufferFrame(rawName, buf);
            return;
          }
          // Mid-ask: this turn belongs to the addresser — others keep
          // buffering for their own (future) turn.
          this.bufferFrame(rawName, buf);
          return;
        }
        if (voiced) {
          this.gateVoiceAt = Date.now();
          // The partner spoke inside the follow-up window: this is a real
          // follow-up turn, so it is governed by the mid-ask silence rule
          // again, not by the window's clock.
          this.followupUntil = 0;
        }
      }
      // SPEAKER IDENTITY (owner plan P2, live bug: "what's my name?" got the
      // wrong participant): tell the agent whenever the voiced speaker changes.
      // Names only, no transcript content. Announced only from HERE, past the
      // gate: while she is shut out of the room's own talk those speaker
      // changes are none of her business, and announcing every one of them
      // both leaked who-was-talking-to-whom into her context and grew that
      // context unboundedly over a long meeting.
      if (speakerChanged && this.el && this.elReady) {
        this.sendEL({ type: "contextual_update", text: "Speaker now talking: " + rawName });
      }
      // Backchannel-safe barge-in: "mhmm" while she answers must not cut her off.
      if (this.holdBarge(buf, voiced)) return;
    } else {
      // Mixed fallback rung (workspace flag off): the room mix contains his
      // own voice while the answer plays — half-duplex gate stays. 800ms
      // grace covers the page's viseme segment buffering.
      if (Date.now() < this.playheadMs + 800) return;
      // The strict gate has to hold on this rung too. It used to live entirely
      // under `if (separate)`, so a workspace without per-participant streams
      // silently ran with NO multiparty gate at all — every word the room said
      // reached the model. Without speaker labels the room is one speaker: the
      // gate is all-or-nothing, and the replay buffer is the room's.
      if (this.strictMode) {
        this.gateTick();
        if (!this.gateOpen) {
          this.bufferFrame(MIXED_KEY, buf);
          return;
        }
        if (this.frameHasVoice(buf)) {
          this.gateVoiceAt = Date.now();
          this.followupUntil = 0;
        }
      }
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
    // Learn the multiparty mode HERE, not from a later crossing signal. A DO
    // starting (or restarting) OPEN in a room that is already strict lets the
    // room's own conversation reach the agent until the next threshold change
    // — which, if the count never changes again, is never.
    if (typeof cfg.strict === "boolean") this.strictMode = cfg.strict;
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
      // ORDER MATTERS. The tail must go out BEFORE elReady is set and before
      // the awaited POST: flipping elReady first let live frames arriving
      // during that await jump ahead of the buffered ones, so the agent heard
      // the opening of the meeting's first sentence AFTER its end — garbled
      // audio on exactly the utterance that decides whether she answers.
      for (const b of this.preBuffer.splice(0)) {
        try { this.el?.send(JSON.stringify({ user_audio_chunk: b })); } catch (_) {}
      }
      this.preBufferB64 = 0;
      this.elReady = true;
      console.log("el ready");
      await this.postEvent("started");
      this.started = true;
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
      // A NEW user utterance closed: any stop applied to the PREVIOUS reply.
      // Without this, a stop fired while the agent was already quiet leaves
      // dropResponse armed and silently swallows the next legit answer.
      this.dropResponse = false;
      return;
    }
    if (t === "agent_response_complete") {
      if (this.tFirstChunk) {
        console.log("stage_response_stream_ms=" + (Date.now() - this.tFirstChunk));
      }
      // The page uses this to close the mouth cleanly: no amplitude flap on
      // trailing padding chunks after the reply is over (live 2026-07-25:
      // "he continues moving the mouth after it finishes").
      this.sendPage({ type: "response_end" });
      // She has answered. Instead of slamming the gate (which forced the room
      // to re-say her name for every single sentence), hand her partner a
      // follow-up window: for the next FOLLOWUP_WINDOW_MS *they* — and nobody
      // else — can keep talking to her without the wake word. It ends early on
      // a new speaker (above) or a vocative aimed at another human
      // (/control gate_close).
      if (this.strictMode) {
        if (this.gateOpen && this.gateSpeaker) {
          this.followupUntil = Date.now() + this.followupWindowMs;
          this.gateVoiceAt = Date.now();
          console.log("follow-up open");
        } else {
          this.closeGate("turn done");
        }
      }
      this.resetBarge();
      this.dropResponse = false;
      return;
    }
    if (t === "audio") {
      const b64 = msg.audio_event && msg.audio_event.audio_base_64;
      if (!b64) return;
      if (this.dropResponse) return; // stop command swallowed this reply
      this.chunks++;
      if (this.chunks === 1) console.log("first agent audio chunk");
      // Per-turn stage decomposition (numbers only, never content):
      //   stage_transcript_to_audio_ms = EL turn-close + LLM + TTS
      //   stage_response_to_audio_ms   = TTS share (text ready -> first audio)
      //   turn_latency_ms              = voiced-frame anchor (echo-noisy)
      if (!this.inResponse) {
        this.inResponse = true;
        this.resetBarge(); // a new reply: the previous turn's burst is over
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
        // Consume-once: stale anchors from a previous turn were summing the
        // whole conversation pause into the next reading (first live run).
        this.tUserTranscript = 0;
        this.tAgentResponse = 0;
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
      this.resetBarge(); // she is no longer speaking: nothing left to protect
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
            // Action provenance (owner plan P2): who was speaking when the
            // agent decided to act — stamped onto queued actions backend-side.
            speaker: this.currentSpeaker || "",
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
