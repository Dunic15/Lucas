const ROOT_ID = "laura-meet-control";

let state = {
  botId: "",
  busy: false,
  status: ""
};

init();

function init() {
  ensureControl();
  refreshStatus();
  window.addEventListener("locationchange", refreshStatus);
  patchHistory("pushState");
  patchHistory("replaceState");
  window.addEventListener("popstate", () => window.dispatchEvent(new Event("locationchange")));
}

function patchHistory(methodName) {
  const original = history[methodName];
  if (original.__lauraPatched) return;

  history[methodName] = function patchedHistoryMethod(...args) {
    const result = original.apply(this, args);
    window.dispatchEvent(new Event("locationchange"));
    return result;
  };
  history[methodName].__lauraPatched = true;
}

function ensureControl() {
  if (document.getElementById(ROOT_ID)) return;
  if (!document.body) {
    requestAnimationFrame(ensureControl);
    return;
  }

  const root = document.createElement("div");
  root.id = ROOT_ID;
  root.innerHTML = `
    <button class="laura-meet-button" type="button" aria-live="polite">
      <span class="laura-meet-dot"></span>
      <span class="laura-meet-label">Send Laura</span>
    </button>
    <div class="laura-meet-status" role="status"></div>
  `;
  document.body.appendChild(root);
  root.querySelector("button").addEventListener("click", onButtonClick);
  render();
}

async function refreshStatus() {
  const meetingUrl = normalizeMeetUrl(location.href);
  if (!meetingUrl) {
    state = { botId: "", busy: false, status: "Open a Meet call first" };
    render();
    return;
  }

  sendMessage({ type: "LAURA_STATUS", meetingUrl })
    .then((response) => {
      if (response.ok) {
        state = { ...state, botId: response.botId || "", status: "" };
        render();
      }
    })
    .catch(() => {});
}

async function onButtonClick() {
  if (state.busy) return;

  const meetingUrl = normalizeMeetUrl(location.href);
  if (!meetingUrl) {
    state = { ...state, status: "Open a Meet call first" };
    render();
    return;
  }

  if (state.botId) {
    await stopLaura(meetingUrl);
  } else {
    await startLaura(meetingUrl);
  }
}

async function startLaura(meetingUrl) {
  state = { ...state, busy: true, status: "Sending Laura..." };
  render();

  try {
    const response = await sendMessage({ type: "LAURA_START", meetingUrl });
    if (!response.ok) throw new Error(response.error || "Could not send Laura");
    state = {
      botId: response.botId || "",
      busy: false,
      status: "Sent. Admit Laura if Google Meet asks."
    };
  } catch (error) {
    state = { ...state, busy: false, status: error.message || "Could not send Laura" };
  }
  render();
}

async function stopLaura(meetingUrl) {
  state = { ...state, busy: true, status: "Stopping Laura..." };
  render();

  try {
    const response = await sendMessage({ type: "LAURA_STOP", meetingUrl, botId: state.botId });
    if (!response.ok) throw new Error(response.error || "Could not stop Laura");
    state = { botId: "", busy: false, status: "Laura stopped." };
  } catch (error) {
    state = { ...state, busy: false, status: error.message || "Could not stop Laura" };
  }
  render();
}

function render() {
  const root = document.getElementById(ROOT_ID);
  if (!root) return;

  const button = root.querySelector("button");
  const label = root.querySelector(".laura-meet-label");
  const status = root.querySelector(".laura-meet-status");

  root.classList.toggle("is-active", Boolean(state.botId));
  root.classList.toggle("is-busy", state.busy);
  button.disabled = state.busy;
  label.textContent = state.busy
    ? "Laura..."
    : state.botId
      ? "Stop Laura"
      : "Send Laura";
  status.textContent = state.status || "";
}

function sendMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      const error = chrome.runtime.lastError;
      if (error) {
        reject(new Error(error.message));
        return;
      }
      resolve(response || { ok: false, error: "No extension response" });
    });
  });
}

function normalizeMeetUrl(raw) {
  try {
    const url = new URL(raw);
    if (url.hostname !== "meet.google.com") return "";
    if (!/^\/[a-z]{3}-[a-z]{4}-[a-z]{3}$/.test(url.pathname)) return "";
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch (_error) {
    return "";
  }
}
