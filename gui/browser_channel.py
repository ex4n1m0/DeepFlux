"""QWebChannel bridge between the built-in browser and DeepFlux.

Provides a direct JavaScript ↔ Python communication path as an alternative
to the control-API XHR loopback (127.0.0.1:53742) used by the browser
extension. The channel is installed on every page via a QWebEngineScript
that loads ``qwebchannel.js`` and exposes a ``window.deepflux`` object.

The existing browser extension (dlmgr/browser_extension.py) continues to
use the XHR path — it is large, tested, and works. This channel is the
idiomatic Qt way for *new* page-side code to talk to the app without HTTP
overhead or port probing.

Python side
-----------
``BrowserChannelBridge`` is a ``QObject`` registered under the name
``deepflux`` on a ``QWebChannel``. Each ``@Slot`` becomes a callable
method on ``window.deepflux`` from JavaScript::

    new QWebChannel(qt.webChannelTransport, function(channel) {
        var df = channel.objects.deepflux;
        df.sendDownload("https://example.com/video.m3u8", "hls", "Title");
    });

The bridge forwards calls to the MainWindow's existing handlers
(tools.call, control API, etc.) on the GUI thread — it has the same
affinity as the MainWindow that owns it.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import QFile, QIODevice, QObject, Slot
from PySide6.QtWebChannel import QWebChannel

logger = logging.getLogger(__name__)

# Minimal qwebchannel.js — the standard Qt loader, trimmed to the essentials.
# Injected before the extension so window.deepflux is available by the time
# the extension's IIFE runs.
_QWEBCHANNEL_JS = r"""
"use strict";
(function(){
  function QWebChannel(transport, callback) {
    this.transport = transport;
    this.objects = {};
    var channel = this;
    transport.send(JSON.stringify({type: 0}));
    transport.onmessage = function(event) {
      var data = JSON.parse(event.data);
      switch (data.type) {
        case 0:
          for (var name in data.data) {
            var obj = new QObject(name, data.data[name], channel);
            channel.objects[name] = obj;
          }
          if (typeof callback === 'function') callback(channel);
          break;
        case 1:
          if (channel.objects[data.object]) {
            channel.objects[data.object]._handleSignal(data);
          }
          break;
        case 2:
          if (channel.objects[data.object]) {
            channel.objects[data.object]._handleProperty(data);
          }
          break;
      }
    };
  }
  function QObject(name, data, webChannel) {
    this.__id__ = data.id;
    webChannel.objects[name] = this;
    this.__objectName__ = name;
    var props = data.properties || {};
    for (var p in props) this[p] = props[p];
    var methods = data.methods || [];
    methods.forEach(function(m){
      this[m] = function() {
        var args = [];
        var callback;
        for (var i = 0; i < arguments.length; i++) {
          if (typeof arguments[i] === 'function') callback = arguments[i];
          else args.push(arguments[i]);
        }
        webChannel.transport.send(JSON.stringify({
          type: 1, object: this.__id__, method: m, args: args
        }));
        if (callback) {
          this['__sig_' + m] = callback;
        }
      }.bind(this);
    }, this);
    var sigs = data.signals || [];
    sigs.forEach(function(s){
      this[s] = function(callback) { this['__sig_' + s] = callback; };
    }, this);
    this._handleSignal = function(msg) {
      if (this['__sig_' + msg.signal]) {
        this['__sig_' + msg.signal].apply(this, msg.args);
      }
    };
    this._handleProperty = function(msg) {
      this[msg.property] = msg.value;
    };
  }
  window.QWebChannel = QWebChannel;
})();
"""

# Bootstrap script: create the channel and expose window.deepflux.
# Pages that don't have qt.webChannelTransport (non-Qt pages) silently skip.
_CHANNEL_BOOTSTRAP_JS = r"""
(function bootDeepFluxChannel(attempt){
  if (window.__deepfluxChannelReady) return;
  if (typeof qt === 'undefined' || !qt.webChannelTransport) {
    if (attempt < 20) setTimeout(function(){ bootDeepFluxChannel(attempt + 1); }, 25);
    return;
  }
  window.__deepfluxChannelReady = true;
  new QWebChannel(qt.webChannelTransport, function(channel) {
    window.deepflux = channel.objects.deepflux;
    window.dispatchEvent(new CustomEvent('deepflux-ready'));
  });
})(0);
"""


def channel_injection_script() -> str:
    """Return the official Qt qwebchannel.js plus the DeepFlux bootstrap."""
    source = QFile(":/qtwebchannel/qwebchannel.js")
    if not source.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("Qt qwebchannel.js resource is unavailable")
    try:
        loader = bytes(source.readAll()).decode("utf-8")
    finally:
        source.close()
    return loader + "\n" + _CHANNEL_BOOTSTRAP_JS


class BrowserChannelBridge(QObject):
    """Python-side bridge object exposed to JavaScript as ``window.deepflux``.

    Every ``@Slot`` is callable from JS as ``window.deepflux.<method>(...)``.
    Calls arrive on the GUI thread (the channel uses the transport's thread
    affinity, which is the page's, i.e. the GUI thread).
    """

    def __init__(self, window: Any, parent: Optional[QObject] = None) -> None:
        super().__init__(parent if parent is not None else window)
        self._window = window

    @Slot(str, result=str)
    def ping(self, msg: str) -> str:
        """Echo — lets JS verify the channel is alive."""
        return f"pong:{msg}"

    @Slot(str, str, str, result=bool)
    def sendDownload(self, url: str, stream_type: str, title: str) -> bool:
        """Send a URL to the internal download manager.

        Equivalent to POST /api/jobs on the control API, but without the
        HTTP loopback. Returns True if the job was accepted."""
        try:
            source_url = self._window._current_browser_view().url().toString()
            if not self._window._confirm_browser_action("download", url, title, source_url):
                return False
            cookies = self._window._browser_cookies_for(url, source_url)
            referrer = source_url
            low = url.lower().split("?", 1)[0]
            if low.endswith((".m3u8", ".mpd")):
                job = self._window._dl_engine.add_stream_job(
                    url=url, filename=title or "", cookies=cookies,
                    referrer=referrer, source_url=referrer,
                )
            else:
                job = self._window._dl_engine.add_job(
                    url=url, filename=title or "", cookies=cookies,
                    referrer=referrer, source_url=referrer,
                )
            self._window._notify("Download started", job.filename, path=job.save_path)
            self._window.main_tabs.setCurrentWidget(self._window._torrents_tab)
            return True
        except Exception as exc:
            logger.warning("WebChannel sendDownload failed: %s", exc)
            return False

    @Slot(str, str, str, result=bool)
    def sendPlay(self, url: str, stream_type: str, title: str) -> bool:
        """Send a stream URL to the in-app mpv player.

        Equivalent to POST /api/play on the control API."""
        try:
            if not self._window._confirm_browser_action("play", url, title):
                return False
            self._window._play_signals.play_stream.emit({
                "url": url,
                "type": stream_type or "",
                "title": title or "",
            })
            return True
        except Exception as exc:
            logger.warning("WebChannel sendPlay failed: %s", exc)
            return False

    @Slot(str, result=bool)
    def addMagnet(self, uri: str) -> bool:
        """Add a magnet URI to the torrent engine directly from page JS."""
        try:
            if not self._window._confirm_browser_action("magnet", uri):
                return False
            result = self._window.tools.call("add_magnet", {
                "uri": uri,
                "save_path": self._window.config.default_save_path,
                "category": "Other",
            })
            return bool(result.get("success"))
        except Exception as exc:
            logger.warning("WebChannel addMagnet failed: %s", exc)
            return False

    @Slot(result=str)
    def getVersion(self) -> str:
        """Return the app version string (for the status badge)."""
        try:
            return getattr(self._window, "_app_version", "unknown")
        except Exception:
            return "unknown"


def create_channel(window: Any) -> "tuple[QWebChannel, BrowserChannelBridge]":
    """Create a QWebChannel + bridge pair wired to the MainWindow.

    The channel must be set on every QWebEnginePage via
    ``page.setWebChannel(channel)``. The injection script
    (channel_injection_script) must be loaded as a QWebEngineScript
    on the profile."""
    channel = QWebChannel()
    bridge = BrowserChannelBridge(window)
    channel.registerObject("deepflux", bridge)
    return channel, bridge
