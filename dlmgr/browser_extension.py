"""Browser extension script injected into the built-in Chromium browser.

This script is loaded as a QWebEngineScript on every page, providing
the same video detection and download overlay as the Chrome extension
but working directly within DeepFlux's built-in browser. No external
extension installation needed — it's always on.

Detects:
- <video>, <audio>, <source> elements
- HLS (.m3u8) and DASH (.mpd) manifest URLs
- Direct video/audio file URLs
- JW Player, Video.js, Shaka Player globals
- YouTube videos (parses ytInitialPlayerResponse + intercepts
  googlevideo.com network requests via PerformanceObserver)
- MissAV videos (extracts UUID from page scripts and constructs
  surrit.com HLS m3u8 URLs + intercepts surrit.com network requests)

Shows a persistent status badge (always visible) that reflects the
connection state of the DeepFlux control API and the number of
detected videos, plus a download menu. Sends download requests to the
local control API at 127.0.0.1:53742.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# The JavaScript that gets injected into every page.
# It's a self-contained IIFE that doesn't depend on any external libraries.
BROWSER_EXTENSION_JS = r"""
(function() {
  'use strict';

  // Guard against double-injection.
  if (window.__deeptorrentInjected) return;
  window.__deeptorrentInjected = true;

  var CONTROL_API_PORT = 53742;
  // The server falls back to the next port(s) if the default is taken,
  // so probe a small range when looking for it.
  var CONTROL_API_CANDIDATE_PORTS = [53742, 53743, 53744, 53745, 53746, 53747, 53748, 53749, 53750, 53751];
  var apiPortIndex = 0;
  function controlApiBase() { return 'http://127.0.0.1:' + CONTROL_API_PORT; }
  var detectedVideos = [];
  var overlayButton = null;
  var statusBadge = null;
  var apiConnected = null;  // null = unknown, true/false after health check
  var lastPageUrl = location.href;

  // ---------------------------------------------------------------------------
  // Utility: guess stream type from URL
  // ---------------------------------------------------------------------------
  function guessType(url) {
    var lower = (url || '').toLowerCase();
    if (lower.indexOf('youtube.com/watch') !== -1) return 'youtube';
    if (lower.indexOf('youtu.be/') !== -1) return 'youtube';
    if (lower.indexOf('.m3u8') !== -1) return 'hls';
    if (lower.indexOf('.mpd') !== -1) return 'dash';
    if (lower.match(/\.(mp4|webm|mkv|avi|mov|flv|m4v)(\?|$)/)) return 'file';
    if (lower.match(/\.(mp3|m4a|aac|ogg|wav)(\?|$)/)) return 'file';
    return 'file';
  }

  // ---------------------------------------------------------------------------
  // Send download to DeepFlux control API
  // ---------------------------------------------------------------------------
  // Make a page title safe to use as a filename on any OS.
  function sanitizeFilename(name) {
    var s = (name || '').replace(/[<>:"/\\|?*\x00-\x1f]/g, '').replace(/\s+/g, ' ').trim();
    if (s.length > 120) s = s.substring(0, 120).trim();
    return s;
  }

  function sendDownload(url, type, title) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', controlApiBase() + '/api/jobs', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = function() {
      if (xhr.readyState === 4) {
        showNotification(xhr.status === 201 ? 'Sent to DeepFlux!' : 'Download failed: ' + xhr.status);
      }
    };
    // Include referrer and cookies — important for sites like YouTube
    // where googlevideo.com URLs require a valid referer header.
    var payload = {
      url: url,
      type: type || guessType(url),
      source_url: window.location.href,
      referrer: window.location.href,
      headers: {
        'User-Agent': navigator.userAgent,
        'Origin': window.location.origin
      }
    };
    // Pass the video title so the download gets a meaningful filename
    // instead of the manifest/URL basename.
    var fn = sanitizeFilename(title);
    if (fn) payload.filename = fn;
    // Try to grab cookies for the current page (same-origin only).
    try {
      if (document.cookie) payload.cookies = document.cookie;
    } catch(e) {}
    xhr.send(JSON.stringify(payload));
  }

  // ---------------------------------------------------------------------------
  // Send stream to the in-app mpv player (POST /api/play)
  //
  // The built-in QtWebEngine browser lacks H.264/AAC codecs, so HTML5 players
  // can't decode most streaming sites. The app embeds mpv (full FFmpeg codec
  // support) — the extension hands the stream URL + headers over for playback.
  // ---------------------------------------------------------------------------
  function sendPlay(url, type, title) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', controlApiBase() + '/api/play', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = function() {
      if (xhr.readyState === 4) {
        showNotification(xhr.status === 200 ? 'Playing in DeepFlux' : 'Play failed: ' + xhr.status);
      }
    };
    var payload = {
      url: url,
      type: type || guessType(url),
      title: title || '',
      referrer: window.location.href,
      headers: {
        'User-Agent': navigator.userAgent,
        'Origin': window.location.origin
      }
    };
    try {
      if (document.cookie) payload.cookies = document.cookie;
    } catch(e) {}
    xhr.send(JSON.stringify(payload));
  }

  // ---------------------------------------------------------------------------
  // Video element detection
  // ---------------------------------------------------------------------------
  function scanForVideoElements() {
    var videos = document.querySelectorAll('video, source, audio');
    videos.forEach(function(el) {
      var src = el.src || el.currentSrc || el.getAttribute('src');
      if (src && src.indexOf('http') === 0) {
        addVideo({
          url: src,
          type: guessType(src),
          title: document.title || ''
        });
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Player global detection
  // ---------------------------------------------------------------------------
  function scanPlayerGlobals() {
    // JW Player
    try {
      if (typeof jwplayer !== 'undefined') {
        var p = jwplayer();
        if (p && p.getPlaylistItem) {
          var item = p.getPlaylistItem();
          if (item && item.file) {
            addVideo({ url: item.file, type: guessType(item.file), title: item.title || document.title || '' });
          }
        }
      }
    } catch(e) {}

    // Video.js
    try {
      if (typeof videojs !== 'undefined' && videojs.players) {
        Object.keys(videojs.players).forEach(function(id) {
          var player = videojs.players[id];
          if (player && player.src) {
            var src = player.src();
            if (src) addVideo({ url: src, type: guessType(src), title: document.title || '' });
          }
        });
      }
    } catch(e) {}
  }

  // ---------------------------------------------------------------------------
  // YouTube-specific detection
  //
  // YouTube uses <video src="blob:..."> (MediaSource) so the standard scanner
  // can't see the real media URLs. We use two strategies:
  //
  // 1. Parse ytInitialPlayerResponse from the page's <script> tags.
  //    This object contains streamingData.formats (progressive, up to 720p)
  //    and streamingData.adaptiveFormats (separate video/audio, higher quality).
  //    Some formats have a direct `url`; others use `signatureCipher` which
  //    requires deciphering (we extract what we can).
  //
  // 2. PerformanceObserver — intercepts actual network requests to
  //    *.googlevideo.com/videoplayback URLs. These are the already-signed,
  //    playable URLs the player fetches. This is how IDM detects YouTube
  //    videos — by watching the real traffic, not parsing the page.
  // ---------------------------------------------------------------------------
  function scanYouTube() {
    if (location.hostname.indexOf('youtube.com') === -1 &&
        location.hostname.indexOf('youtube-nocookie.com') === -1) return;

    // Strategy 0: On YouTube watch pages, add the page URL itself as a
    // downloadable video. Modern YouTube no longer embeds direct media URLs
    // in ytInitialPlayerResponse — the signed URLs are generated at runtime
    // by YouTube's JS player. The backend uses yt-dlp to resolve YouTube
    // watch URLs into actual media streams.
    var watchMatch = location.href.match(/[?&]v=([a-zA-Z0-9_-]{11})/);
    if (watchMatch) {
      var videoId = watchMatch[1];
      var watchUrl = 'https://www.youtube.com/watch?v=' + videoId;
      var ytTitle = document.title.replace(/\s*-\s*YouTube\s*$/i, '') || 'YouTube video';
      addVideo({
        url: watchUrl,
        type: 'youtube',
        title: ytTitle
      });
    }

    // Strategy 1: parse ytInitialPlayerResponse from script tags.
    // NOTE: Modern YouTube signs googlevideo.com URLs with a signature cipher
    // that requires deciphering via the JS player's transform functions. We
    // cannot decipher them in the extension, so sending these raw URLs as
    // type 'file' results in 403 Forbidden. Instead, we only use the watch
    // URL (Strategy 0) which the backend resolves via yt-dlp — yt-dlp handles
    // the signature cipher, adaptive format merging, and all other
    // complexities. The direct URLs from ytInitialPlayerResponse are skipped
    // to avoid showing the user download options that will fail.
    //
    // (Left here for reference — do NOT add direct googlevideo.com URLs to
    // the detected list. yt-dlp on the backend handles everything.)
  }

  function extractYouTubePlayerResponse() {
    // ytInitialPlayerResponse is a JS global on YouTube watch pages.
    if (typeof ytInitialPlayerResponse !== 'undefined' && ytInitialPlayerResponse) {
      return ytInitialPlayerResponse;
    }

    // Fallback: search <script> tags for the JSON assignment.
    var scripts = document.querySelectorAll('script');
    for (var i = 0; i < scripts.length; i++) {
      var text = scripts[i].textContent;
      if (!text || text.indexOf('ytInitialPlayerResponse') === -1) continue;

      // Find the assignment: ytInitialPlayerResponse = {...};
      var marker = 'ytInitialPlayerResponse = ';
      var idx = text.indexOf(marker);
      if (idx === -1) continue;

      var start = idx + marker.length;
      // Find the end of the JSON object by matching braces.
      var depth = 0;
      var inString = false;
      var escape = false;
      var end = -1;
      for (var j = start; j < text.length; j++) {
        var ch = text.charAt(j);
        if (escape) { escape = false; continue; }
        if (ch === '\\') { escape = true; continue; }
        if (ch === '"') { inString = !inString; continue; }
        if (inString) continue;
        if (ch === '{') depth++;
        else if (ch === '}') {
          depth--;
          if (depth === 0) { end = j + 1; break; }
        }
      }
      if (end === -1) continue;

      try {
        return JSON.parse(text.substring(start, end));
      } catch(e) { /* malformed JSON, try next script */ }
    }
    return null;
  }

  function extractUrlFromSignatureCipher(sc) {
    // signatureCipher is a query-string-like value:
    //   s=<ciphered_sig>&sp=<sig_param>&url=<base_url>
    try {
      var params = {};
      sc.split('&').forEach(function(pair) {
        var eq = pair.indexOf('=');
        if (eq !== -1) {
          params[pair.substring(0, eq)] = decodeURIComponent(pair.substring(eq + 1));
        }
      });
      // We can't decipher `s` without YouTube's player JS, so we return
      // the base URL. The download may fail if the signature is required,
      // but many progressive formats work without it.
      return params.url || '';
    } catch(e) { return ''; }
  }

  // Strategy 2: PerformanceObserver — catch real media network requests.
  // Works for YouTube (googlevideo.com/videoplayback) and MissAV
  // (surrit.com/{uuid}/playlist.m3u8 and /{quality}/video.m3u8).
  // These URLs already have valid signatures/cookies applied by the player.
  var mediaObserver = null;
  function startMediaNetworkInterceptor() {
    if (mediaObserver) return;
    try {
      mediaObserver = new PerformanceObserver(function(list) {
        list.getEntries().forEach(function(entry) {
          var name = entry.name || '';
          if (!name) return;

          // YouTube media URLs:
          //   https://rr{N}.---sn-...googlevideo.com/videoplayback?...
          if (name.indexOf('googlevideo.com/videoplayback') !== -1) {
            // Filter out audio-only segments (mime=audio) when possible.
            var isAudioOnly = name.indexOf('mime=audio') !== -1 &&
                              name.indexOf('mime=video') === -1;
            if (isAudioOnly) return;

            var gq = guessYouTubeQuality(name);
            var gtitle = (document.title || 'YouTube video').replace(/\s*-\s*YouTube\s*$/i, '');
            addVideo({
              url: name,
              type: 'file',
              title: gtitle + (gq ? ' [' + gq + ']' : ''),
              quality: gq
            });
            return;
          }

          // MissAV HLS URLs:
          //   https://surrit.com/{uuid}/playlist.m3u8
          //   https://surrit.com/{uuid}/{quality}/video.m3u8
          if (name.indexOf('surrit.com/') !== -1 && name.indexOf('.m3u8') !== -1) {
            // Only add the master playlist, not every quality sub-playlist.
            // If we already have the master playlist, skip sub-playlists.
            if (name.indexOf('/playlist.m3u8') !== -1) {
              addVideo({
                url: name,
                type: 'hls',
                title: document.title || '',
                quality: ''
              });
            } else if (name.indexOf('/video.m3u8') !== -1) {
              // Extract quality from URL path: /{uuid}/{quality}/video.m3u8
              var qMatch = name.match(/\/(\d+p)\/video\.m3u8/);
              addVideo({
                url: name,
                type: 'hls',
                title: document.title || '',
                quality: qMatch ? qMatch[1] : ''
              });
            }
            return;
          }
        });
      });
      mediaObserver.observe({ type: 'resource', buffered: true });
    } catch(e) { /* PerformanceObserver not supported */ }
  }

  function guessYouTubeQuality(url) {
    // YouTube encodes quality hints in the videoplayback URL params.
    var itagMatch = url.match(/[?&]itag=(\d+)/);
    if (itagMatch) {
      var itag = itagMatch[1];
      // Common YouTube itag -> quality mapping.
      var qualityMap = {
        '17': '144p', '18': '360p', '22': '720p', '43': '360p',
        '36': '240p', '135': '480p', '136': '720p', '137': '1080p',
        '138': '2160p', '160': '144p', '242': '240p', '243': '360p',
        '244': '480p', '247': '720p', '248': '1080p', '271': '1440p',
        '278': '144p', '298': '720p', '299': '1080p', '302': '720p',
        '303': '1080p', '308': '1440p', '313': '2160p', '315': '2160p',
        '333': '480p', '334': '720p', '335': '1080p', '336': '1440p',
        '337': '2160p'
      };
      if (qualityMap[itag]) return qualityMap[itag];
    }
    return '';
  }

  // ---------------------------------------------------------------------------
  // MissAV-specific detection (missav.ws, .ai, .live, .fans, .media, etc.)
  //
  // MissAV serves videos as HLS from surrit.com CDN:
  //   https://surrit.com/{uuid}/playlist.m3u8        (master playlist)
  //   https://surrit.com/{uuid}/{quality}/video.m3u8 (per-quality sub-playlist)
  //
  // The UUID is embedded in an obfuscated <script> tag on the page, near the
  // word "seek". It appears 38 characters before "seek" and is a standard
  // UUID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (36 chars).
  //
  // We scan all <script> tags for this pattern. The PerformanceObserver
  // (started above) also catches the actual surrit.com requests when the
  // user presses play — this active scan finds the URL without needing to.
  // ---------------------------------------------------------------------------
  var MISSAV_DOMAINS = [
    'missav.ws', 'missav.ai', 'missav.live', 'missav.fans',
    'missav.media', 'missav123.com', 'missav01.com'
  ];

  function isMissAVPage() {
    var host = location.hostname.toLowerCase();
    for (var i = 0; i < MISSAV_DOMAINS.length; i++) {
      if (host === MISSAV_DOMAINS[i] || host.indexOf('.' + MISSAV_DOMAINS[i]) !== -1) {
        return true;
      }
    }
    return false;
  }

  function scanMissAV() {
    if (!isMissAVPage()) return;

    // Only run on video pages — MissAV video pages have a <video> element
    // or the .order-first container. Skip listing/search pages.
    var hasVideo = document.querySelector('video') ||
                   document.querySelector('.order-first') ||
                   document.querySelector('#video-player');
    if (!hasVideo) return;

    // Extract UUID from <script> tags — look for "seek" preceded by a UUID.
    var uuid = extractMissAVUuid();
    if (!uuid) return;

    // Construct the master m3u8 URL.
    var masterUrl = 'https://surrit.com/' + uuid + '/playlist.m3u8';

    // Get the video title from the page.
    var title = '';
    var h1 = document.querySelector('.order-first h1') ||
             document.querySelector('h1');
    if (h1) title = h1.textContent.trim();
    if (!title) title = document.title || '';

    addVideo({
      url: masterUrl,
      type: 'hls',
      title: title,
      quality: ''
    });

    // Also fetch the master playlist to discover available qualities.
    // Each line that isn't a comment is a relative path like "720p/video.m3u8".
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', masterUrl, true);
      xhr.onreadystatechange = function() {
        if (xhr.readyState === 4 && xhr.status === 200) {
          var lines = xhr.responseText.split('\n');
          lines.forEach(function(line) {
            line = line.trim();
            if (!line || line.charAt(0) === '#') return;
            // Line is like "720p/video.m3u8"
            var quality = line.split('/')[0];
            var subUrl = 'https://surrit.com/' + uuid + '/' + line;
            addVideo({
              url: subUrl,
              type: 'hls',
              title: title + ' [' + quality + ']',
              quality: quality
            });
          });
        }
      };
      xhr.send();
    } catch(e) { /* CORS or network error — the PerformanceObserver will
                    still catch the URL when the user plays the video. */ }
  }

  function extractMissAVUuid() {
    // Strategy 1: scan all <script> tags for "seek" preceded by a UUID.
    var scripts = document.querySelectorAll('script');
    var uuidPattern = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;

    for (var i = 0; i < scripts.length; i++) {
      var text = scripts[i].textContent;
      if (!text || text.indexOf('seek') === -1) continue;

      // Find all occurrences of "seek" and check what's before each.
      var seekIdx = text.indexOf('seek');
      while (seekIdx !== -1) {
        // The UUID is 36 chars long, located at [seekIdx-38, seekIdx-2].
        if (seekIdx >= 38) {
          var candidate = text.substring(seekIdx - 38, seekIdx - 2);
          if (uuidPattern.test(candidate)) {
            return candidate;
          }
        }
        seekIdx = text.indexOf('seek', seekIdx + 1);
      }
    }

    // Strategy 2: fallback — search the entire page HTML for any UUID pattern.
    // This is less precise but catches cases where the script structure differs.
    var html = document.documentElement.innerHTML;
    var uuidRegex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/gi;
    var match;
    while ((match = uuidRegex.exec(html)) !== null) {
      // Only accept if there's a "seek" nearby (within 200 chars).
      var pos = match.index;
      var nearby = html.substring(Math.max(0, pos - 200), pos + 200 + 36);
      if (nearby.indexOf('seek') !== -1) {
        return match[1];
      }
    }

    // Strategy 3: last resort — just return the first UUID found on the page.
    // MissAV pages typically contain only one UUID (the video ID).
    uuidRegex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/i;
    match = uuidRegex.exec(html);
    if (match) return match[1];

    return null;
  }

  function addVideo(media) {
    for (var i = 0; i < detectedVideos.length; i++) {
      if (detectedVideos[i].url === media.url) return;
    }
    detectedVideos.push(media);
    showOverlayButton();
    updateStatusBadge();
  }

  // ---------------------------------------------------------------------------
  // Persistent status badge — always visible, shows extension is active
  // ---------------------------------------------------------------------------
  function createStatusBadge() {
    if (statusBadge) return;
    statusBadge = document.createElement('div');
    statusBadge.id = 'deeptorrent-status-badge';
    statusBadge.style.cssText = [
      'position: fixed',
      'bottom: 10px',
      'right: 10px',
      'z-index: 2147483647',
      'background: linear-gradient(135deg, #011d3e, #001431)',
      'color: #2a7abf',
      'border: 1px solid #2a7abf',
      'border-radius: 5px',
      'padding: 4px 7px',
      'font-family: Segoe UI, Arial, sans-serif',
      'font-size: 7px',
      'font-weight: 600',
      'cursor: pointer',
      'box-shadow: 0 2px 6px rgba(42, 122, 191, 0.3)',
      'transition: all 0.2s ease',
      'user-select: none',
      'display: flex',
      'align-items: center',
      'gap: 4px',
      'max-width: 160px'
    ].join(';');
    // Drag to move; a plain click (no drag) opens the download menu.
    var dragState = null;
    statusBadge.addEventListener('mousedown', function(e) {
      var rect = statusBadge.getBoundingClientRect();
      dragState = { dx: e.clientX - rect.left, dy: e.clientY - rect.top, moved: false };
      e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
      if (!dragState) return;
      if (!dragState.moved) {
        // Only start dragging after 4px of movement so plain clicks still work.
        var r0 = statusBadge.getBoundingClientRect();
        if (Math.abs(e.clientX - (dragState.dx + r0.left)) < 4 &&
            Math.abs(e.clientY - (dragState.dy + r0.top)) < 4) {
          return;
        }
        dragState.moved = true;
        // Switch from bottom/right anchoring to explicit left/top.
        statusBadge.style.left = r0.left + 'px';
        statusBadge.style.top = r0.top + 'px';
        statusBadge.style.right = 'auto';
        statusBadge.style.bottom = 'auto';
      }
      var x = Math.max(0, Math.min(window.innerWidth - statusBadge.offsetWidth, e.clientX - dragState.dx));
      var y = Math.max(0, Math.min(window.innerHeight - statusBadge.offsetHeight, e.clientY - dragState.dy));
      statusBadge.style.left = x + 'px';
      statusBadge.style.top = y + 'px';
    });
    document.addEventListener('mouseup', function() {
      var wasDrag = dragState && dragState.moved;
      dragState = null;
      statusBadge._suppressClick = wasDrag;
    });
    statusBadge.addEventListener('click', function() {
      if (statusBadge._suppressClick) { statusBadge._suppressClick = false; return; }
      openDownloadMenu();
    });
    statusBadge.addEventListener('mouseenter', function() {
      statusBadge.style.transform = 'scale(1.05)';
      statusBadge.style.boxShadow = '0 6px 18px rgba(0, 229, 255, 0.5)';
    });
    statusBadge.addEventListener('mouseleave', function() {
      statusBadge.style.transform = '';
      statusBadge.style.boxShadow = '0 4px 12px rgba(0, 229, 255, 0.3)';
    });
    if (document.body) {
      document.body.appendChild(statusBadge);
    } else {
      // Body not ready yet — wait for it.
      document.addEventListener('DOMContentLoaded', function() {
        if (statusBadge && statusBadge.parentNode !== document.body) {
          document.body.appendChild(statusBadge);
        }
      });
    }
    updateStatusBadge();
  }

  function updateStatusBadge() {
    if (!statusBadge) return;
    var color, label;

    if (apiConnected === null) {
      color = '#ffa500';
      label = 'DeepFlux: connecting...';
    } else if (apiConnected === false) {
      color = '#ff4444';
      label = 'DeepFlux: offline';
    } else if (detectedVideos.length === 0) {
      color = '#00e5ff';
      label = 'DeepFlux: watching for videos';
    } else {
      color = '#00ff9d';
      // Clear call-to-action when videos are available.
      label = '⬇ Download ' + detectedVideos.length + ' video' + (detectedVideos.length > 1 ? 's' : '') + ' — click here';
    }

    // Build badge content with DOM API (NOT innerHTML) — YouTube enforces
    // Trusted Types CSP (`require-trusted-types-for 'script'`), which makes
    // innerHTML throw a TypeError and aborts the whole addVideo() flow.
    while (statusBadge.firstChild) statusBadge.removeChild(statusBadge.firstChild);
    var dot = document.createElement('span');
    dot.style.cssText = 'display:inline-block;width:5px;height:5px;border-radius:50%;flex-shrink:0;background:' + color + ';box-shadow:0 0 3px ' + color + ';';
    var labelText = document.createElement('span');
    labelText.textContent = label;
    statusBadge.appendChild(dot);
    statusBadge.appendChild(labelText);

    // Highlight border when videos are available.
    if (detectedVideos.length > 0 && apiConnected) {
      statusBadge.style.borderColor = '#00ff9d';
      statusBadge.style.color = '#00ff9d';
      // Subtle pulse animation to draw attention.
      statusBadge.style.animation = 'deeptorrent-pulse 2s ease-in-out infinite';
    } else if (apiConnected === false) {
      statusBadge.style.borderColor = '#ff4444';
      statusBadge.style.color = '#ff8888';
      statusBadge.style.animation = '';
    } else {
      statusBadge.style.borderColor = '#00e5ff';
      statusBadge.style.color = '#00e5ff';
      statusBadge.style.animation = '';
    }
  }

  // Inject pulse keyframes once.
  function injectPulseStyle() {
    if (document.getElementById('deeptorrent-pulse-style')) return;
    var style = document.createElement('style');
    style.id = 'deeptorrent-pulse-style';
    style.textContent = '@keyframes deeptorrent-pulse { 0%,100% { box-shadow: 0 4px 12px rgba(0,255,157,0.3); } 50% { box-shadow: 0 4px 20px rgba(0,255,157,0.7); } }';
    var head = document.head || document.documentElement;
    if (head) head.appendChild(style);
  }

  // ---------------------------------------------------------------------------
  // Control API health check (with port probing)
  // ---------------------------------------------------------------------------
  // Guard: only one sweep over the candidate ports may run at a time.
  // Without it, overlapping sweeps corrupt the shared apiPortIndex.
  var healthSweepActive = false;

  function checkApiHealth() {
    if (healthSweepActive) return;
    healthSweepActive = true;
    probeHealthPort();
  }

  function probeHealthPort() {
    // CRITICAL: on a failed/blocked request Chromium fires BOTH
    // onreadystatechange (readyState 4, status 0) AND onerror for the same
    // XHR. Without the `settled` guard each failure used to schedule TWO
    // next-port probes, doubling the probe chains at every hop — a runaway
    // XHR storm that starved the renderer's frame scheduler down to ~10 Hz.
    var settled = false;
    var xhr = new XMLHttpRequest();
    xhr.open('GET', 'http://127.0.0.1:' + CONTROL_API_CANDIDATE_PORTS[apiPortIndex] + '/api/health', true);
    xhr.timeout = 3000;
    function fail() {
      if (settled) return;
      settled = true;
      handleHealthFailure();
    }
    xhr.onreadystatechange = function() {
      if (xhr.readyState !== 4) return;
      if (xhr.status === 200) {
        if (settled) return;
        settled = true;
        healthSweepActive = false;
        // Found the server — remember this port.
        CONTROL_API_PORT = CONTROL_API_CANDIDATE_PORTS[apiPortIndex];
        var wasConnected = apiConnected;
        apiConnected = true;
        if (apiConnected !== wasConnected) updateStatusBadge();
      } else {
        fail();
      }
    };
    xhr.onerror = fail;
    xhr.ontimeout = fail;
    try {
      xhr.send();
    } catch(e) {
      fail();
    }
  }

  function handleHealthFailure() {
    // Probe the next candidate port, deferred with setTimeout so a sweep
    // over dead ports never runs as a tight synchronous loop.
    if (apiPortIndex < CONTROL_API_CANDIDATE_PORTS.length - 1) {
      apiPortIndex++;
      setTimeout(probeHealthPort, 250);
    } else {
      // Exhausted all ports — server is really not running.
      apiPortIndex = 0;
      healthSweepActive = false;
      var wasConnected = apiConnected;
      apiConnected = false;
      if (apiConnected !== wasConnected) updateStatusBadge();
    }
  }

  // ---------------------------------------------------------------------------
  // Overlay UI — legacy button kept for compatibility, now driven by badge
  // ---------------------------------------------------------------------------
  function showOverlayButton() {
    // The persistent status badge replaces the old overlay button.
    // Videos are reflected in the badge count instead.
    if (!statusBadge) createStatusBadge();
    updateStatusBadge();
  }

  function openDownloadMenu() {
    // Remove existing menu (toggle closed).
    var existing = document.getElementById('deeptorrent-download-menu');
    if (existing) { existing.remove(); return; }

    // Single video + API online — download directly, no menu needed.
    if (detectedVideos.length === 1 && apiConnected) {
      sendDownload(detectedVideos[0].url, detectedVideos[0].type, detectedVideos[0].title);
      return;
    }

    var menu = document.createElement('div');
    menu.id = 'deeptorrent-download-menu';
    menu.style.cssText = [
      'position: fixed',
      'bottom: 60px',
      'right: 20px',
      'z-index: 2147483647',
      'background: #0a0a0f',
      'border: 1px solid #1a2a4a',
      'border-radius: 10px',
      'padding: 8px',
      'min-width: 280px',
      // Grow to fit long titles (shrink-to-fit expands toward this cap).
      'max-width: 720px',
      // Scroll vertically when there are many videos.
      'max-height: 60vh',
      'overflow-y: auto',
      'box-shadow: 0 8px 24px rgba(0, 0, 0, 0.6)',
      'font-family: Segoe UI, Arial, sans-serif'
    ].join(';');

    var title = document.createElement('div');
    title.textContent = 'Download with DeepFlux';
    title.style.cssText = 'color: #00e5ff; font-size: 13px; font-weight: 700; padding: 6px 10px; border-bottom: 1px solid #1a2a4a; margin-bottom: 4px;';
    menu.appendChild(title);

    if (apiConnected === false) {
      var offline = document.createElement('div');
      offline.textContent = 'DeepFlux app is not running. Start the desktop app to enable downloads.';
      offline.style.cssText = 'color: #ff8888; font-size: 12px; padding: 10px; line-height: 1.4;';
      menu.appendChild(offline);
    } else if (detectedVideos.length === 0) {
      var empty = document.createElement('div');
      empty.style.cssText = 'color: #8a9aaa; font-size: 12px; padding: 10px; line-height: 1.5;';
      // Use DOM nodes instead of innerHTML — YouTube enforces Trusted Types CSP.
      empty.appendChild(document.createTextNode('No downloadable videos detected on this page yet.'));
      empty.appendChild(document.createElement('br'));
      empty.appendChild(document.createElement('br'));
      empty.appendChild(document.createTextNode('DeepFlux scans for '));
      var c1 = document.createElement('code'); c1.textContent = '<video>';
      empty.appendChild(c1);
      empty.appendChild(document.createTextNode(', HLS (.m3u8), DASH (.mpd), and direct media files. Try playing the video first — some sites load the stream only after you press play.'));
      menu.appendChild(empty);

      // Manual URL entry option.
      var manualLabel = document.createElement('div');
      manualLabel.textContent = 'Or paste a video URL manually:';
      manualLabel.style.cssText = 'color: #c8d3e0; font-size: 12px; padding: 8px 10px 4px; border-top: 1px solid #1a2a4a; margin-top: 6px;';
      menu.appendChild(manualLabel);

      var inputRow = document.createElement('div');
      inputRow.style.cssText = 'display: flex; gap: 4px; padding: 4px 10px 8px;';
      var input = document.createElement('input');
      input.type = 'text';
      input.placeholder = 'https://.../video.m3u8';
      input.style.cssText = 'flex: 1; background: #1a1a2a; border: 1px solid #2a3a5a; border-radius: 4px; padding: 6px 8px; color: #e0e0e0; font-size: 12px; outline: none;';
      var submitBtn = document.createElement('button');
      submitBtn.textContent = 'Download';
      submitBtn.style.cssText = 'background: #00e5ff; color: #001431; border: none; border-radius: 4px; padding: 6px 12px; font-size: 12px; font-weight: 600; cursor: pointer;';
      submitBtn.addEventListener('click', function() {
        var url = input.value.trim();
        if (url) {
          sendDownload(url, guessType(url));
          menu.remove();
        }
      });
      input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { submitBtn.click(); }
      });
      inputRow.appendChild(input);
      inputRow.appendChild(submitBtn);
      menu.appendChild(inputRow);
    } else {
      detectedVideos.forEach(function(video, i) {
        var row = document.createElement('div');
        row.style.cssText = 'display: flex; align-items: center; gap: 6px; padding: 4px 6px; border-radius: 6px;';
        row.addEventListener('mouseenter', function() { row.style.background = '#1a2a4a'; });
        row.addEventListener('mouseleave', function() { row.style.background = ''; });

        var item = document.createElement('div');
        item.textContent = (video.title || 'Video ' + (i + 1)) + ' [' + video.type.toUpperCase() + ']';
        // Wrap long titles across lines instead of truncating with ellipsis.
        item.style.cssText = 'flex: 1; color: #c8d3e0; font-size: 13px; padding: 4px; cursor: pointer; white-space: normal; word-break: break-word; overflow-wrap: anywhere; line-height: 1.4;';
        item.addEventListener('mouseenter', function() { item.style.color = '#00e5ff'; });
        item.addEventListener('mouseleave', function() { item.style.color = '#c8d3e0'; });
        item.title = 'Download';
        item.addEventListener('click', function() {
          sendDownload(video.url, video.type, video.title);
          menu.remove();
        });
        row.appendChild(item);

        var playBtn = document.createElement('button');
        playBtn.textContent = '▶ Play';
        playBtn.title = 'Play in the DeepFlux player (mpv)';
        playBtn.style.cssText = 'flex-shrink: 0; background: transparent; color: #00ff9d; border: 1px solid #00ff9d; border-radius: 4px; padding: 4px 10px; font-size: 12px; font-weight: 600; cursor: pointer;';
        playBtn.addEventListener('click', function() {
          sendPlay(video.url, video.type, video.title);
          menu.remove();
        });
        row.appendChild(playBtn);

        menu.appendChild(row);
      });
    }

    var closeBtn = document.createElement('div');
    closeBtn.textContent = 'Cancel';
    closeBtn.style.cssText = 'color: #4a6a8a; font-size: 12px; padding: 6px 10px; text-align: center; cursor: pointer; margin-top: 4px; border-top: 1px solid #1a2a4a;';
    closeBtn.addEventListener('click', function() { menu.remove(); });
    menu.appendChild(closeBtn);

    document.body.appendChild(menu);

    // Clamp to viewport: if the menu grew past the left edge (narrow window),
    // pin it 8px from the left and let it shrink instead of clipping.
    var mRect = menu.getBoundingClientRect();
    if (mRect.left < 8) {
      menu.style.left = '8px';
      menu.style.right = '8px';
    }
  }

  function showNotification(text) {
    var notif = document.createElement('div');
    notif.textContent = text;
    notif.style.cssText = [
      'position: fixed',
      'top: 20px',
      'right: 20px',
      'z-index: 2147483647',
      'background: #0a0a0f',
      'color: #00ff9d',
      'border: 1px solid #00ff9d',
      'border-radius: 8px',
      'padding: 10px 18px',
      'font-family: Segoe UI, Arial, sans-serif',
      'font-size: 14px',
      'font-weight: 600',
      'box-shadow: 0 4px 12px rgba(0, 255, 157, 0.3)'
    ].join(';');
    document.body.appendChild(notif);
    setTimeout(function() {
      notif.style.transition = 'opacity 0.5s, transform 0.5s';
      notif.style.opacity = '0';
      notif.style.transform = 'translateY(-10px)';
      setTimeout(function() { notif.remove(); }, 500);
    }, 2500);
  }

  // ---------------------------------------------------------------------------
  // MutationObserver — catch dynamically loaded video elements
  // ---------------------------------------------------------------------------
  var observer = new MutationObserver(function(mutations) {
    var shouldScan = false;
    mutations.forEach(function(mutation) {
      mutation.addedNodes.forEach(function(node) {
        if (node.nodeType === 1) {
          if (node.tagName === 'VIDEO' || node.tagName === 'AUDIO' ||
              (node.querySelector && node.querySelector('video, audio, source'))) {
            shouldScan = true;
          }
        }
      });
    });
    if (shouldScan) scanForVideoElements();
  });

  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // ---------------------------------------------------------------------------
  // Intercept clicks on .m3u8/.mpd links (streams only).
  // NOTE: .torrent links are deliberately NOT intercepted — they go through
  // the browser's native download (with session cookies) and DeepFlux adds
  // them to the torrent engine automatically on completion.
  // ---------------------------------------------------------------------------
  document.addEventListener('click', function(e) {
    var link = e.target.closest ? e.target.closest('a[href]') : null;
    if (!link) return;
    var href = link.href;
    if (!href) return;
    var lower = href.toLowerCase();
    if (lower.indexOf('.m3u8') !== -1 || lower.indexOf('.mpd') !== -1) {
      e.preventDefault();
      e.stopPropagation();
      sendDownload(href, guessType(href));
    }
  }, true);

  // ---------------------------------------------------------------------------
  // Initial scan + periodic rescan
  // ---------------------------------------------------------------------------
  // Show the badge immediately so the user sees the extension is active,
  // even before any videos are detected.
  injectPulseStyle();
  createStatusBadge();
  checkApiHealth();
  // Re-check API health periodically (every 30s) in case the app restarts.
  setInterval(checkApiHealth, 30000);

  // Start the media network interceptor immediately — it passively watches
  // for googlevideo.com (YouTube) and surrit.com (MissAV) media requests
  // as the user plays videos.
  startMediaNetworkInterceptor();

  // ---------------------------------------------------------------------------
  // SPA navigation handling (YouTube, etc.)
  //
  // YouTube is a single-page app — clicking from one video to another does
  // NOT reload the page, so this script never re-runs. We poll for URL
  // changes; when the URL changes we clear the detected videos (which belong
  // to the previous page) and rescan. YouTube also fires yt-navigate-finish,
  // which we listen to for faster response.
  // ---------------------------------------------------------------------------
  function onNavigate() {
    lastPageUrl = location.href;
    detectedVideos.length = 0;
    updateStatusBadge();
    // Give the new page a moment to set up its player before scanning.
    setTimeout(function() {
      scanForVideoElements();
      scanYouTube();
      scanMissAV();
    }, 1500);
  }

  window.addEventListener('yt-navigate-finish', onNavigate);
  window.addEventListener('popstate', onNavigate);
  setInterval(function() {
    if (location.href !== lastPageUrl) onNavigate();
  }, 1000);

  scanForVideoElements();
  scanPlayerGlobals();
  scanYouTube();
  scanMissAV();
  // Rescan periodically — YouTube and MissAV load player data dynamically.
  setInterval(function() {
    scanForVideoElements();
    scanYouTube();
    scanMissAV();
  }, 5000);
})();
"""


def get_extension_js() -> str:
    """Return the browser extension JavaScript for injection."""
    return BROWSER_EXTENSION_JS


def inject_into_profile(profile, script_name: str = "deeptorrent_extension") -> None:
    """Inject the extension script into a QWebEngineProfile.

    The script runs on every page load at document idle, providing
    video detection and download overlay functionality — always on,
    no installation required.

    Args:
        profile: A QWebEngineProfile instance.
        script_name: Unique name for the script (for management).
    """
    from PySide6.QtWebEngineCore import QWebEngineScript

    script = QWebEngineScript()
    script.setName(script_name)
    script.setSourceCode(BROWSER_EXTENSION_JS)
    script.setInjectionPoint(QWebEngineScript.DocumentReady)
    script.setWorldId(QWebEngineScript.MainWorld)
    script.setRunsOnSubFrames(True)

    profile.scripts().insert(script)
    logger.info("DeepFlux browser extension injected into profile (always on)")
