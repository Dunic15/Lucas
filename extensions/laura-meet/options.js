const apiBaseInput = document.getElementById("apiBase");
const statusEl = document.getElementById("status");

chrome.runtime.sendMessage({ type: "LAURA_GET_CONFIG" }, (response) => {
  if (response && response.ok) apiBaseInput.value = response.apiBase;
});

document.getElementById("save").addEventListener("click", () => {
  statusEl.textContent = "";
  chrome.runtime.sendMessage(
    { type: "LAURA_SET_CONFIG", apiBase: apiBaseInput.value },
    (response) => {
      const error = chrome.runtime.lastError;
      if (error) {
        statusEl.style.color = "#b3261e";
        statusEl.textContent = error.message;
        return;
      }
      if (!response || !response.ok) {
        statusEl.style.color = "#b3261e";
        statusEl.textContent = response?.error || "Could not save backend URL";
        return;
      }
      apiBaseInput.value = response.apiBase;
      statusEl.style.color = "#188038";
      statusEl.textContent = "Saved.";
    }
  );
});
