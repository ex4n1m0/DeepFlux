// DeepFlux Integration Module — Content Script
//
// Scans the page for video/audio elements and player globals, injects
// a floating "Download Video" button overlay near detected players,
// and relays download requests to the background service worker.

let detectedVideos = [];
let overlayButton = null;

// ---------------------------------------------------------------------------
// Video element detection
// ---------------------------------------------------------------------------

function scanForVideoElements() {
  const videos = document.querySelectorAll("video, source, audio");
  videos.forEach((el) => {
    const src = el.src || el.currentSrc || el.getAttribute("src");
    if (src && src.startsWith("http")) {
      const media = {
        url: src,
        type: guessType(src),
        title: document.title || "",
        quality: el.getAttribute("height") ? `${el.getAttribute("height")}p` : ""
      };
      addVideo(media);
    }
  });
}

function guessType(url) {
  const lower = url.toLowerCase();
  if (lower.includes(".m3u8")) return "hls";
  if (lower.includes(".mpd")) return "dash";
  if (lower.match(/\.(mp4|webm|mkv|avi|mov|flv|m4v)(\?|$)/)) return "file";
  if (lower.match(/\.(mp3|m4a|aac|ogg|wav)(\?|$)/)) return "file";
  return "file";
}

function addVideo(media) {
  if (!detectedVideos.find(v => v.url === media.url)) {
    detectedVideos.push(media);
    showOverlayButton();
  }
}

// ---------------------------------------------------------------------------
// Player global detection (JW Player, Video.js, Shaka Player)
// ---------------------------------------------------------------------------

function scanPlayerGlobals() {
  // JW Player.
  try {
    if (typeof jwplayer !== "undefined") {
      const players = jwplayer();
      if (players && players.getPlaylistItem) {
        const item = players.getPlaylistItem();
        if (item && item.file) {
          addVideo({
            url: item.file,
            type: guessType(item.file),
            title: item.title || document.title || "",
            quality: ""
          });
        }
      }
    }
  } catch (e) {}

  // Video.js.
  try {
    if (typeof videojs !== "undefined" && videojs.players) {
      Object.values(videojs.players).forEach((player) => {
        if (player && player.src) {
          const src = player.src();
          if (src) {
            addVideo({
              url: src,
              type: guessType(src),
              title: document.title || "",
              quality: ""
            });
          }
        }
      });
    }
  } catch (e) {}

  // Shaka Player.
  try {
    if (typeof shaka !== "undefined") {
      const players = shaka.Player;
      // Shaka doesn't expose a global registry easily, but we can check
      // for video elements that Shaka has attached to.
      document.querySelectorAll("video").forEach((v) => {
        if (v.src && v.src.includes(".m3u8") || v.src && v.src.includes(".mpd")) {
          addVideo({
            url: v.src,
            type: guessType(v.src),
            title: document.title || "",
            quality: ""
          });
        }
      });
    }
  } catch (e) {}
}

// ---------------------------------------------------------------------------
// Floating "Download Video" overlay button
// ---------------------------------------------------------------------------

function showOverlayButton() {
  if (overlayButton) return;  // Already shown.

  // Check if overlay is enabled in settings.
  chrome.storage.sync.get({showOverlay: true}, (settings) => {
    if (!settings.showOverlay) return;

    overlayButton = document.createElement("div");
    overlayButton.id = "deeptorrent-overlay-btn";
    overlayButton.innerHTML = "&#11015; Download Video";
    overlayButton.addEventListener("click", openDownloadMenu);
    document.body.appendChild(overlayButton);
  });
}

function openDownloadMenu() {
  if (detectedVideos.length === 0) {
    alert("No downloadable videos detected on this page.");
    return;
  }

  if (detectedVideos.length === 1) {
    // Single video — download directly.
    downloadVideo(detectedVideos[0]);
    return;
  }

  // Multiple videos — show a simple menu.
  const menu = document.createElement("div");
  menu.id = "deeptorrent-download-menu";
  menu.innerHTML = "<div class='dt-menu-title'>Download with DeepFlux</div>";

  detectedVideos.forEach((video, i) => {
    const item = document.createElement("div");
    item.className = "dt-menu-item";
    const label = video.title || `Video ${i + 1}`;
    const quality = video.quality ? ` (${video.quality})` : "";
    const type = video.type.toUpperCase();
    item.textContent = `${label}${quality} [${type}]`;
    item.addEventListener("click", () => {
      downloadVideo(video);
      menu.remove();
    });
    menu.appendChild(item);
  });

  // Close button.
  const closeBtn = document.createElement("div");
  closeBtn.className = "dt-menu-close";
  closeBtn.textContent = "Cancel";
  closeBtn.addEventListener("click", () => menu.remove());
  menu.appendChild(closeBtn);

  document.body.appendChild(menu);
}

function downloadVideo(video) {
  chrome.runtime.sendMessage({
    type: "download_video",
    url: video.url,
    mediaType: video.type,
    pageUrl: window.location.href
  }, (response) => {
    if (response && response.status === "sent") {
      // Show a brief notification.
      const notif = document.createElement("div");
      notif.id = "deeptorrent-notif";
      notif.textContent = "Sent to DeepFlux!";
      document.body.appendChild(notif);
      setTimeout(() => notif.remove(), 3000);
    }
  });
}

// ---------------------------------------------------------------------------
// Listen for media_detected messages from the background script
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "media_detected") {
    addVideo(message.media);
  }
});

// ---------------------------------------------------------------------------
// MutationObserver — catch dynamically loaded video elements
// ---------------------------------------------------------------------------

const observer = new MutationObserver((mutations) => {
  let shouldScan = false;
  mutations.forEach((mutation) => {
    mutation.addedNodes.forEach((node) => {
      if (node.nodeType === Node.ELEMENT_NODE) {
        if (node.tagName === "VIDEO" || node.tagName === "AUDIO" ||
            node.querySelector?.("video, audio, source")) {
          shouldScan = true;
        }
      }
    });
  });
  if (shouldScan) {
    scanForVideoElements();
  }
});

// document.body may not exist yet on early-run pages — observe the root
// element instead (always present) so detection never crashes.
observer.observe(document.documentElement, {childList: true, subtree: true});

// ---------------------------------------------------------------------------
// Initial scan
// ---------------------------------------------------------------------------

scanForVideoElements();
scanPlayerGlobals();

// Re-scan periodically (catches lazy-loaded content).
setInterval(() => {
  scanForVideoElements();
}, 5000);
