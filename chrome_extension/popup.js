// DeepFlux Integration Module — Popup Script

document.addEventListener("DOMContentLoaded", () => {
  // Get the current tab.
  chrome.tabs.query({active: true, currentWindow: true}, (tabs) => {
    const tabId = tabs[0]?.id;
    if (tabId) {
      loadDetectedMedia(tabId);
    }
  });

  // Load status.
  loadStatus();

  // Options link.
  document.getElementById("open-options").addEventListener("click", () => {
    chrome.runtime.openOptionsPage();
  });

  // Ping native host.
  document.getElementById("ping-native").addEventListener("click", () => {
    chrome.runtime.sendMessage({type: "ping_native"}, (response) => {
      const statusEl = document.getElementById("status-text");
      if (response && response.status === "ping_sent") {
        statusEl.textContent = "Ping sent to DeepFlux";
        statusEl.classList.add("active");
      } else {
        statusEl.textContent = "Failed to ping DeepFlux";
      }
    });
  });
});

function loadDetectedMedia(tabId) {
  chrome.runtime.sendMessage({type: "get_detected_media", tabId: tabId}, (response) => {
    const listEl = document.getElementById("media-list");
    if (!response || !response.media || response.media.length === 0) {
      listEl.innerHTML = '<div class="no-media">No media detected on this tab.</div>';
      return;
    }

    listEl.innerHTML = "";
    response.media.forEach((media, i) => {
      const item = document.createElement("div");
      item.className = "media-item";

      const info = document.createElement("div");
      info.className = "media-info";

      const title = document.createElement("div");
      title.className = "media-title";
      title.textContent = media.title || `Media ${i + 1}`;

      const meta = document.createElement("div");
      meta.className = "media-type";
      meta.textContent = media.type.toUpperCase() +
        (media.quality ? ` · ${media.quality}` : "") +
        (media.size ? ` · ${formatSize(media.size)}` : "");

      info.appendChild(title);
      info.appendChild(meta);

      const btn = document.createElement("button");
      btn.className = "btn-download";
      btn.textContent = "Download";
      btn.addEventListener("click", () => {
        chrome.tabs.query({active: true, currentWindow: true}, (tabs) => {
          chrome.runtime.sendMessage({
            type: "download_video",
            url: media.url,
            mediaType: media.type,
            pageUrl: tabs[0]?.url || ""
          }, (resp) => {
            if (resp && resp.status === "sent") {
              btn.textContent = "Sent!";
              btn.style.background = "#00ff9d";
              setTimeout(() => window.close(), 1000);
            }
          });
        });
      });

      item.appendChild(info);
      item.appendChild(btn);
      listEl.appendChild(item);
    });
  });
}

function loadStatus() {
  chrome.runtime.sendMessage({type: "get_status"}, (response) => {
    const statusEl = document.getElementById("status-text");
    if (response) {
      if (response.activeDownloads > 0) {
        statusEl.textContent = `${response.activeDownloads} active download(s)`;
        statusEl.classList.add("active");
      } else {
        statusEl.textContent = "No active downloads";
      }
    } else {
      statusEl.textContent = "DeepFlux not connected";
    }
  });
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024;
    i++;
  }
  return `${bytes.toFixed(1)} ${units[i]}`;
}

// Listen for status updates.
chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "status_update") {
    const statusEl = document.getElementById("status-text");
    const count = message.data?.active_count || 0;
    if (count > 0) {
      statusEl.textContent = `${count} active download(s)`;
      statusEl.classList.add("active");
    } else {
      statusEl.textContent = "No active downloads";
      statusEl.classList.remove("active");
    }
  }
});
