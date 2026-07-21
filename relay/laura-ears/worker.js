// laura-ears: Cloudflare Worker relay.
//
// Why this exists: AWS App Runner (the Laura backend) does NOT accept inbound
// WebSockets, so Recall's real-time audio (audio_mixed_raw) can't reach it.
// Cloudflare Workers DO accept inbound WS (and can open an outbound WS to
// Gemini Live). This relay sits in the middle:
//
//   Recall --audio(WS)--> [this worker] --Gemini Live(WS)--> transcription/turns
//                              |
//                              +--HTTP POST--> backend /webhooks/recall (App Runner)
//
// The backend owns config + auth + the whole meeting pipeline (gates, brain,
// ElevenLabs voice). The relay is deliberately dumb: it fetches per-session
// config (mode/model/persona + a fresh Vertex token) from the backend, streams
// audio into Gemini, and POSTs each completed turn back to the backend. No
// secrets live in the worker except the backend URL + a shared bearer.

const GEMINI_WS =
  "https://aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent";

function decode(data) {
  return typeof data === "string" ? data : new TextDecoder().decode(data);
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (req.headers.get("Upgrade") !== "websocket") {
      return new Response("laura-ears relay up", { status: 200 });
    }
    const m = url.pathname.match(/\/realtime\/recall-audio\/([^/]+)/);
    if (!m) return new Response("bad path", { status: 400 });
    const cap = m[1];

    // Per-session config + a fresh Vertex token, from the backend (which owns
    // the service account and validates the capability -> bot).
    let cfg;
    try {
      const r = await fetch(`${env.BACKEND_URL}/internal/ears-config/${cap}`, {
        headers: { Authorization: "Bearer " + env.BACKEND_BEARER },
      });
      if (!r.ok) return new Response("config " + r.status, { status: 403 });
      cfg = await r.json();
    } catch (e) {
      return new Response("config-err", { status: 502 });
    }
    if (!cfg.enabled) return new Response("ears off", { status: 409 });

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();
    // ctx.waitUntil keeps the worker (and the outbound Gemini WS + its pumps)
    // alive for the whole session; without it a plain Worker is torn down after
    // fetch() returns and the audio never reaches Gemini.
    ctx.waitUntil(handleSession(server, cap, cfg, env));
    return new Response(null, { status: 101, webSocket: client });
  },
};

function handleSession(recall, cap, cfg, env) {
  return new Promise((resolveSession) => { runSession(recall, cap, cfg, env, resolveSession); });
}

async function runSession(recall, cap, cfg, env, done) {
  let gemini;
  try {
    const resp = await fetch(GEMINI_WS, {
      headers: { Upgrade: "websocket", Authorization: "Bearer " + cfg.vertex_token },
    });
    gemini = resp.webSocket;
    if (!gemini) {
      try { recall.close(1011, "gemini-connect"); } catch (_) {}
      done(); return;
    }
    gemini.accept();
  } catch (e) {
    try { recall.close(1011); } catch (_) {}
    done(); return;
  }

  const modelPath =
    `projects/${cfg.project}/locations/${cfg.location}` +
    `/publishers/google/models/${cfg.live_model}`;
  const reply = cfg.mode === "reply";
  const gen = reply
    ? { responseModalities: ["TEXT"], maxOutputTokens: 150 }
    : { responseModalities: ["TEXT"], maxOutputTokens: 1 };
  const sys = reply
    ? `You are ${cfg.persona || "Laura"}, an assistant taking part in a work ` +
      `meeting. CRITICAL: always reply in the SAME language the person just ` +
      `spoke; if they speak English, reply in English. Keep it short and ` +
      `natural, like on a phone call. If you don't know something, say so ` +
      `briefly. No bullet lists.`
    : "Reply only with: .";
  console.log('gemini connected, sending setup, mode=' + cfg.mode);
  gemini.send(
    JSON.stringify({
      setup: {
        model: modelPath,
        generationConfig: gen,
        systemInstruction: { parts: [{ text: sys }] },
        inputAudioTranscription: {},
      },
    })
  );

  let acc = "";
  let rep = "";
  let audioFrames = 0;
  let gemMsgs = 0;
  gemini.addEventListener("message", async (e) => {
    // Gemini sends BINARY frames (Blob/ArrayBuffer), NOT strings. The simple
    // decode() throws on a Blob and the catch swallowed EVERY message -> the
    // worker never saw setupComplete or any turn (gemMsgs stayed 0).
    let text;
    try {
      if (typeof e.data === "string") text = e.data;
      else if (e.data instanceof ArrayBuffer) text = new TextDecoder().decode(e.data);
      else if (e.data && e.data.arrayBuffer) text = new TextDecoder().decode(await e.data.arrayBuffer());
      else return;
    } catch (_) { return; }
    let msg;
    try { msg = JSON.parse(text); } catch (_) { return; }
    gemMsgs++;
    if (gemMsgs === 1) console.log('gemini first msg: ' + (msg.setupComplete ? 'setupComplete' : Object.keys(msg).join(',')));
    const sc = msg.serverContent;
    if (!sc) return;
    const it = sc.inputTranscription && sc.inputTranscription.text;
    if (it) acc += it;
    if (reply && sc.modelTurn && sc.modelTurn.parts) {
      for (const p of sc.modelTurn.parts) if (p.text) rep += p.text;
    }
    if (sc.turnComplete) {
      const text = acc.trim();
      let r = rep.trim();
      acc = "";
      rep = "";
      if (r === ".") r = "";
      console.log('turn: text_len=' + text.length + ' reply_len=' + r.length);
      if (text) await postTurn(cap, cfg, env, text, reply ? r : "");
    }
  });
  gemini.addEventListener("close", () => { try { recall.close(); } catch (_) {} done(); });
  gemini.addEventListener("error", () => { try { recall.close(); } catch (_) {} done(); });

  recall.addEventListener("message", (e) => {
    let ev;
    try { ev = JSON.parse(decode(e.data)); } catch (_) { return; }
    if (ev.event !== "audio_mixed_raw.data") return;
    const buf = ev.data && ev.data.data && ev.data.data.buffer;
    audioFrames++;
    if (audioFrames === 1) console.log('FIRST audio frame, buf len=' + (buf?buf.length:'MISSING') + ' keys=' + JSON.stringify(Object.keys(ev.data||{})));
    if (audioFrames % 50 === 0) console.log('audio frames=' + audioFrames + ' gemMsgs=' + gemMsgs);
    if (!buf) return;
    try {
      gemini.send(
        JSON.stringify({
          realtimeInput: {
            mediaChunks: [{ mimeType: "audio/pcm;rate=16000", data: buf }],
          },
        })
      );
    } catch (_) {}
  });
  recall.addEventListener("close", () => { try { gemini.close(); } catch (_) {} done(); });
  recall.addEventListener("error", () => { try { gemini.close(); } catch (_) {} done(); });
}

async function postTurn(cap, cfg, env, text, reply) {
  // Synthesize the exact transcript.data payload the backend already knows how
  // to process (marker laura_ears). The backend attributes the speaker from its
  // own Recall-final ring; the relay hears mixed audio and can't. bot_id lets
  // the webhook's capability/session check pass.
  const payload = {
    event: "transcript.data",
    laura_ears: true,
    laura_ears_text: text,
    data: {
      bot: { id: cfg.bot_id },
      data: { words: [{ text }], participant: {} },
    },
  };
  if (reply) payload.laura_ears_reply = reply;
  try {
    await fetch(`${env.BACKEND_URL}/webhooks/recall?cap=${encodeURIComponent(cap)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (_) {}
}
