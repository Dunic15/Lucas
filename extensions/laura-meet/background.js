const DEFAULT_API_BASE = "https://laura-avatar.onrender.com";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message.type !== "string") return false;

  handleMessage(message)
    .then(sendResponse)
    .catch((error) => {
      sendResponse({
        ok: false,
        error: error instanceof Error ? error.message : String(error)
      });
    });

  return true;
});

async function handleMessage(message) {
  if (message.type === "LAURA_STATUS") {
    const meetingUrl = requireMeetingUrl(message.meetingUrl);
    const sessions = await getSessions();
    return { ok: true, botId: sessions[meetingUrl] || "" };
  }

  if (message.type === "LAURA_START") {
    const meetingUrl = requireMeetingUrl(message.meetingUrl);
    const apiBase = await getApiBase();
    const response = await fetch(`${apiBase}/sessions/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ meeting_url: meetingUrl, avatar_id: "laura" })
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.error || `Laura start failed (${response.status})`);

    const botId = data.bot_id || "";
    if (botId) {
      const sessions = await getSessions();
      sessions[meetingUrl] = botId;
      await chrome.storage.local.set({ activeSessions: sessions });
    }
    return { ok: true, botId, data };
  }

  if (message.type === "LAURA_STOP") {
    const meetingUrl = requireMeetingUrl(message.meetingUrl);
    const sessions = await getSessions();
    const botId = sessions[meetingUrl] || message.botId || "";
    if (!botId) return { ok: true, botId: "" };

    const apiBase = await getApiBase();
    const response = await fetch(`${apiBase}/sessions/${encodeURIComponent(botId)}/end`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.error || `Laura stop failed (${response.status})`);

    delete sessions[meetingUrl];
    await chrome.storage.local.set({ activeSessions: sessions });
    return { ok: true, botId };
  }

  if (message.type === "LAURA_GET_CONFIG") {
    return { ok: true, apiBase: await getApiBase() };
  }

  if (message.type === "LAURA_SET_CONFIG") {
    const apiBase = sanitizeApiBase(message.apiBase || DEFAULT_API_BASE);
    await chrome.storage.sync.set({ apiBase });
    return { ok: true, apiBase };
  }

  throw new Error(`Unknown Laura message: ${message.type}`);
}

async function getApiBase() {
  const result = await chrome.storage.sync.get({ apiBase: DEFAULT_API_BASE });
  return sanitizeApiBase(result.apiBase || DEFAULT_API_BASE);
}

async function getSessions() {
  const result = await chrome.storage.local.get({ activeSessions: {} });
  return result.activeSessions && typeof result.activeSessions === "object"
    ? result.activeSessions
    : {};
}

function requireMeetingUrl(value) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error("Missing meeting URL");
  }
  return value.trim();
}

function sanitizeApiBase(value) {
  const url = new URL(value);
  if (url.protocol !== "https:") throw new Error("Laura backend must use HTTPS");
  url.pathname = url.pathname.replace(/\/+$/, "");
  url.search = "";
  url.hash = "";
  return url.toString().replace(/\/$/, "");
}

async function readJson(response) {
  try {
    return await response.json();
  } catch (_error) {
    return {};
  }
}
