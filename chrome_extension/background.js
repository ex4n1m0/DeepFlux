// DeepFlux Integration Module — Background Service Worker
//
// Handles:
// - Native messaging connection to DeepFlux desktop app
// - Download interception (chrome.downloads API)
// - Network sniffing for .m3u8/.mpd/video content types
// - Context menu "Download with DeepFlux"
// - Badge update with active download count
// - Message relay between content scripts and native host

const NATIVE_HOST = "com.deeptorrent.integration";
let nativePort = null;
let activeDownloads = 0;
let detectedMedia = {};  // tabId -> [{url, type, title, quality}]

// ---------------------------------------------------------------------------
// Native messaging connection
// ---------------------------------------------------------------------------

function connectNative() {
  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
    nativePort.onMessage.addListener((msg) => {
      handleNativeMessage(msg);
    });
    nativePort.onDisconnect.addListener(() => {
      console.log("Native host disconnected:", chrome.runtime.lastError);
      nativePort = null;
      // Reconnect after a delay.
      setTimeout(connectNative, 5000);
    });
    console.log("Connected to DeepFlux native host");
  } catch (e) {
    console.error("Failed to connect to native host:", e);
  }
}

function sendNative(message) {
  if (!nativePort) {
    connectNative();
  }
  if (nativePort) {
    try {
      nativePort.postMessage(message);
    } catch (e) {
      console.error("Failed to send native message:", e);
      nativePort = null;
    }
  }
}

// Request/response correlation: the native host echoes our message id back
// in its reply, so callers can await a specific response.
let nativeMsgCounter = 0;
const pendingNative = new Map();  // id -> resolve

function sendNativeWithResponse(message, timeoutMs = 5000) {
  return new Promise((resolve) => {
    const id = "req_" + (++nativeMsgCounter);
    pendingNative.set(id, resolve);
    setTimeout(() => {
      if (pendingNative.delete(id)) resolve(null);  // timeout
    }, timeoutMs);
    sendNative({...message, id: id});
  });
}

function cookiesFor(url, cb) {
  chrome.cookies.getAll({url: url}, (cookies) => {
    const cookieStr = (cookies || []).map(c => `${c.name}=${c.value}`).join("; ");
    cb(cookieStr);
  });
}

function handleNativeMessage(msg) {
  // Correlated response to a sendNativeWithResponse call.
  if (msg.id && pendingNative.has(msg.id)) {
    const resolve = pendingNative.get(msg.id);
    pendingNative.delete(msg.id);
    if (resolve) resolve(msg.result !== undefined ? msg.result : msg);
    return;
  }
  if (msg.type === "status_update") {
    activeDownloads = msg.active_count || 0;
    updateBadge();
    // Broadcast to any open popups.
    chrome.runtime.sendMessage({type: "status_update", data: msg}).catch(() => {});
  }
}

function updateBadge() {
  const text = activeDownloads > 0 ? String(activeDownloads) : "";
  chrome.action.setBadgeText({text: text});
  chrome.action.setBadgeBackgroundColor({color: "#00e5ff"});
}

// ---------------------------------------------------------------------------
// Download interception
// ---------------------------------------------------------------------------

// Size threshold for intercepting native downloads (default 10 MB).
const DEFAULT_SIZE_THRESHOLD = 10 * 1024 * 1024;

chrome.downloads.onDeterminingFilename.addListener((downloadItem, suggest) => {
  chrome.storage.sync.get({
    autoCapture: false,
    sizeThreshold: DEFAULT_SIZE_THRESHOLD
  }, (settings) => {
    if (!settings.autoCapture) {
      suggest();
      return;
    }
    // Check file size (filesize is -1 until determined, so check after).
    if (downloadItem.fileSize > 0 && downloadItem.fileSize < settings.sizeThreshold) {
      suggest();
      return;
    }
    // Intercept: cancel the native download and send to DeepFlux.
    chrome.downloads.cancel(downloadItem.id, () => {
      if (chrome.runtime.lastError) {
        // Cancel failed — let Chrome keep the download rather than
        // downloading it twice.
        console.warn("Could not cancel Chrome download:", chrome.runtime.lastError);
        suggest();
        return;
      }
      cookiesFor(downloadItem.url, (cookieStr) => {
        sendNative({
          action: "submit_download",
          url: downloadItem.url,
          filename: downloadItem.filename || "",
          cookies: cookieStr,
          referrer: downloadItem.referrer || "",
          type: "file",
          source_url: downloadItem.url,
          headers: {"User-Agent": navigator.userAgent}
        });
      });
    });
    // Don't call suggest — we cancelled the download.
  });
});

// ---------------------------------------------------------------------------
// Network sniffing — detect .m3u8, .mpd, and video/audio content types
// ---------------------------------------------------------------------------

const VIDEO_MIME_TYPES = [
  "video/mp4", "video/webm", "video/ogg", "video/x-msvideo",
  "video/x-flv", "video/quicktime", "video/x-matroska",
  "audio/mpeg", "audio/mp4", "audio/ogg", "audio/aac", "audio/wav"
];

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return;  // Service worker / background requests.

    const contentType = details.responseHeaders?.find(
      h => h.name.toLowerCase() === "content-type"
    )?.value?.toLowerCase() || "";

    const url = details.url;

    // Detect HLS.
    if (url.includes(".m3u8") || contentType.includes("mpegurl") || contentType.includes("vnd.apple.mpegurl")) {
      addDetectedMedia(details.tabId, {
        url: url,
        type: "hls",
        title: "",
        quality: "",
        contentType: contentType
      });
    }
    // Detect DASH.
    else if (url.includes(".mpd") || contentType.includes("dash+xml")) {
      addDetectedMedia(details.tabId, {
        url: url,
        type: "dash",
        title: "",
        quality: "",
        contentType: contentType
      });
    }
    // Detect direct video/audio.
    else if (VIDEO_MIME_TYPES.some(t => contentType.startsWith(t))) {
      // Only tag if it looks like a full file (not a range request segment).
      const contentLength = details.responseHeaders?.find(
        h => h.name.toLowerCase() === "content-length"
      )?.value;
      addDetectedMedia(details.tabId, {
        url: url,
        type: "file",
        title: "",
        quality: "",
        contentType: contentType,
        size: contentLength ? parseInt(contentLength) : 0
      });
    }
  },
  {urls: ["<all_urls>"]},
  ["responseHeaders"]
);

function addDetectedMedia(tabId, media) {
  if (!detectedMedia[tabId]) {
    detectedMedia[tabId] = [];
  }
  // Avoid duplicates.
  const existing = detectedMedia[tabId].find(m => m.url === media.url);
  if (!existing) {
    detectedMedia[tabId].push(media);
    // Notify the content script (if any) that new media was found.
    chrome.tabs.sendMessage(tabId, {type: "media_detected", media: media}).catch(() => {});
  }
}

// Clean up detected media when a tab is closed.
chrome.tabs.onRemoved.addListener((tabId) => {
  delete detectedMedia[tabId];
});

// ---------------------------------------------------------------------------
// Context menu — "Download with DeepFlux"
// ---------------------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "download-with-deeptorrent",
    title: "Download with DeepFlux",
    contexts: ["link", "video", "audio", "image"]
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== "download-with-deeptorrent") return;

  let url = info.linkUrl || info.srcUrl || info.pageUrl;
  if (!url) return;

  // Get cookies for the URL.
  cookiesFor(url, (cookieStr) => {
    const type = url.includes(".m3u8") ? "hls" : url.includes(".mpd") ? "dash" : "file";
    sendNative({
      action: "submit_download",
      url: url,
      cookies: cookieStr,
      referrer: tab?.url || "",
      type: type,
      source_url: tab?.url || url,
      headers: {"User-Agent": navigator.userAgent}
    });
  });
});

// ---------------------------------------------------------------------------
// Message relay — from content scripts and popup
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "download_video") {
    // From content script — download a detected video/stream.
    cookiesFor(message.url, (cookieStr) => {
      sendNative({
        action: "submit_download",
        url: message.url,
        cookies: cookieStr,
        referrer: message.pageUrl || sender.tab?.url || "",
        type: message.mediaType || "file",
        source_url: sender.tab?.url || message.url,
        headers: {"User-Agent": (message.userAgent || navigator.userAgent)}
      });
      sendResponse({status: "sent"});
    });
    return true;  // async sendResponse
  }

  if (message.type === "get_detected_media") {
    // From popup — get detected media for the current tab.
    const tabId = message.tabId;
    sendResponse({media: detectedMedia[tabId] || []});
    return true;
  }

  if (message.type === "get_status") {
    // From popup — get current download status. Wait for the native host's
    // list_jobs response instead of replying with stale local state.
    sendNativeWithResponse({action: "list_jobs"}).then((result) => {
      if (result && result.jobs) {
        const active = result.jobs.filter(j =>
          j.status === "downloading" || j.status === "queued").length;
        sendResponse({activeDownloads: active, jobs: result.jobs});
      } else {
        sendResponse({activeDownloads: activeDownloads});  // fallback: last push
      }
    }).catch(() => {
      // Never leave the popup hanging on a rejected native call.
      sendResponse({activeDownloads: activeDownloads});
    });
    return true;  // async sendResponse
  }

  if (message.type === "ping_native") {
    sendNative({action: "ping"});
    sendResponse({status: "ping_sent"});
    return true;
  }
});

// ---------------------------------------------------------------------------
// Initialize
// ---------------------------------------------------------------------------

connectNative();
updateBadge();
