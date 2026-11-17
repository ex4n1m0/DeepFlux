package space.deepflux.android.browser

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.DownloadListener
import android.webkit.GeolocationPermissions
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import space.deepflux.android.dlmgr.HttpDownloadRepository
import space.deepflux.android.engine.TorrentRepository
import java.io.File
import java.io.InputStream
import java.net.URL
import java.net.URLDecoder

/**
 * In-app browser (ANDROID_STUDY.md §12 Phase A2) — the QtWebEngine tab's
 * Android stand-in on the system WebView.
 *
 * Architecture: ONE WebView PER TAB, all parked in a single FrameLayout with
 * visibility swapping. Compose's AndroidView would dispose (destroy) a
 * WebView whose node leaves composition — unacceptable on every tab switch —
 * so the container owns the views and Compose only embeds the container.
 *
 * Torrent interception rides the WebView DownloadListener: any URL the
 * renderer can't display (application/x-bittorrent, attachment
 * Content-Disposition, extension-less binaries) lands there with the real
 * server MIME type — so BOTH .torrent links and generic file downloads are
 * caught at one choke point with cookies + UA + referer preserved. Torrents
 * are fetched to cacheDir and handed to the engine; everything else goes to
 * the A1 HTTP download engine.
 */
class BrowserController(
    private val activityContext: Context,
    private val torrents: TorrentRepository,
    private val downloads: HttpDownloadRepository,
    // Bookmarks + history (plan §13 #3); owned by AppRoot so it outlives
    // this controller's composition.
    val store: BrowserStore,
    /** The configured homepage (Settings → Browser), read at newTab time. */
    private val homepageProvider: () -> String = { DEFAULT_HOMEPAGE },
) {

    data class TabInfo(
        val id: Int,
        val title: String,
        val url: String,
        val loading: Boolean = false,
        val progress: Int = 0,
        val canGoBack: Boolean = false,
        val canGoForward: Boolean = false,
        val secure: Boolean = false,
        /** Main-frame load error ("" = none). Cleared on the next navigation. */
        val error: String = "",
    )

    /** A video detected on the active page (A3 grabber). */
    data class GrabbedVideo(
        val url: String,
        val kind: Kind,
        val referer: String,
        /** The PAGE title at detection time — the display name (desktop
         *  browser_extension.py uses document.title the same way; raw URL
         *  filenames like "180640836_240p.m3u8" tell the user nothing). */
        val title: String = "",
        /** "720p" when derivable from the URL, else "". */
        val quality: String = "",
    ) {
        enum class Kind { PROGRESSIVE, HLS, DASH, BLOB }
    }

    /**
     * A downloadable/navigable link found by the page scan (§13 #9 — the
     * site-grabber concept at v1: magnet/.torrent anchors, direct video
     * links, and plain page links, lifted from the page's own DOM).
     */
    data class PageLink(
        val text: String,
        val url: String,
        val kind: Kind,
    ) {
        enum class Kind { TORRENT_MAGNET, TORRENT_FILE, VIDEO, PAGE }
    }

    private class Tab(
        val id: Int,
        val webView: WebView,
        var info: TabInfo,
    )

    private val _tabs = MutableStateFlow<List<TabInfo>>(emptyList())
    val tabs: StateFlow<List<TabInfo>> = _tabs.asStateFlow()

    private val _activeId = MutableStateFlow(-1)
    val activeId: StateFlow<Int> = _activeId.asStateFlow()

    val active: TabInfo? get() = _tabs.value.find { it.id == _activeId.value }

    /** The custom fullscreen <video> view while a page requests fullscreen. */
    private val _fullscreenView = MutableStateFlow<View?>(null)
    val fullscreenView: StateFlow<View?> = _fullscreenView.asStateFlow()

    // ---- A3 video grabber: videos found on the ACTIVE tab's page ----
    private val videosByTab = mutableMapOf<Int, MutableList<GrabbedVideo>>()

    private val _videos = MutableStateFlow<List<GrabbedVideo>>(emptyList())
    val videos: StateFlow<List<GrabbedVideo>> = _videos.asStateFlow()

    // ---- §13 #9: page-links scan per tab ----
    private val linksByTab = mutableMapOf<Int, MutableList<PageLink>>()

    private val _pageLinks = MutableStateFlow<List<PageLink>>(emptyList())
    val pageLinks: StateFlow<List<PageLink>> = _pageLinks.asStateFlow()

    private val mainHandler = Handler(Looper.getMainLooper())

    private val tabsInternal = mutableListOf<Tab>()
    private var nextId = 1
    private var fullscreenCallback: android.webkit.WebChromeClient.CustomViewCallback? = null

    /** Requests blocked by Adblock since the controller was created. */
    var blockedCount = 0
        private set

    /** Lazily inflated container holding every tab's WebView. */
    val container: FrameLayout by lazy {
        FrameLayout(activityContext).apply {
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            )
        }
    }

    // ------------------------------------------------------------------ tabs

    fun newTab(url: String? = null): Int {
        // WebView wants an Activity context for JS dialogs and <select> popups.
        val wv = WebView(activityContext)
        val id = nextId++
        configure(wv, id)
        // A tab opened without a target opens the HOMEPAGE (v1.3): the
        // browser must land on a real page, not a blank "type something".
        // An explicitly blank homepage ("") keeps the start page instead.
        val target = url ?: homepageProvider().trim().ifBlank { null }
        val tab = Tab(id, wv, TabInfo(id, "", target ?: ""))
        tabsInternal.add(tab)
        container.addView(
            wv,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            ),
        )
        // Activate BEFORE syncViews (bug fix 1.3.1): syncViews sets every
        // non-active WebView GONE, and with the OLD order the assignment
        // below came too late — the brand-new tab was left GONE at 0×0, so
        // pages loaded (titles, JS, link scans) but never painted. This was
        // the "browser shows nothing" bug on real phones.
        _activeId.value = id
        syncViews()
        publish()
        if (target != null) {
            wv.loadUrl(target)
        } else {
            updateInfo(id) { it.copy(title = "", url = "") }
        }
        return id
    }

    fun closeTab(id: Int) {
        val idx = tabsInternal.indexOfFirst { it.id == id }
        if (idx < 0) return
        val tab = tabsInternal.removeAt(idx)
        exitFullscreenIfShowing(tab.webView)
        container.removeView(tab.webView)
        tab.webView.stopLoading()
        tab.webView.destroy()
        videosByTab.remove(id)
        linksByTab.remove(id)
        if (_activeId.value == id) {
            val neighbor = tabsInternal.getOrNull(idx.coerceAtMost(tabsInternal.size - 1))
                ?: tabsInternal.lastOrNull()
            _activeId.value = neighbor?.id ?: -1
        }
        syncViews()
        publish()
        publishVideos()
        publishLinks()
    }

    fun select(id: Int) {
        if (tabsInternal.any { it.id == id }) {
            _activeId.value = id
            syncViews()
            publish()
            publishVideos()
            publishLinks()
        }
    }

    fun currentWebView(): WebView? = tabsInternal.find { it.id == _activeId.value }?.webView

    /** Load whatever is in the URL bar: a URL, a magnet, or a DuckDuckGo search. */
    fun loadInput(inputRaw: String) {
        val input = inputRaw.trim()
        if (input.isEmpty()) return
        if (input.startsWith("magnet:", true)) {
            // shouldOverrideUrlLoading never fires for programmatic loadUrl(),
            // so a magnet pasted in the bar is routed to the engine HERE; link
            // clicks on pages take the override path below.
            torrents.addMagnet(input)
            return
        }
        val wv = currentWebView() ?: run { newTab(); currentWebView() } ?: return
        val target = normalize(input)
        wv.loadUrl(target)
    }

    private fun normalize(input: String): String = when {
        input.startsWith("http://", true) || input.startsWith("https://", true) ||
            input.startsWith("about:", true) || input.startsWith("file://", true) ||
            input.startsWith("data:", true) -> input
        input.contains(' ') || !input.contains('.') ->
            "https://duckduckgo.com/?q=" + Uri.encode(input)
        else -> "https://$input"
    }

    fun goBack() { currentWebView()?.takeIf { it.canGoBack() }?.goBack() }
    fun goForward() { currentWebView()?.takeIf { it.canGoForward() }?.goForward() }
    fun reload() { currentWebView()?.reload() }
    fun stop() { currentWebView()?.stopLoading() }

    fun openInSystemBrowser() {
        val url = active?.url?.takeIf { it.startsWith("http") } ?: return
        runCatching {
            activityContext.startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        }.onFailure { torrents.postNotice("No external browser available") }
    }

    /** Pause media/timers when the browser screen leaves the foreground. */
    fun onHidden() {
        tabsInternal.forEach { it.webView.onPause() }
    }

    fun onShown() {
        tabsInternal.forEach { it.webView.onResume() }
    }

    fun destroy() {
        exitFullscreenIfShowing(null)
        tabsInternal.forEach {
            container.removeView(it.webView)
            it.webView.destroy()
        }
        tabsInternal.clear()
        videosByTab.clear()
        linksByTab.clear()
        publish()
        publishVideos()
        publishLinks()
    }

    fun exitFullscreen(): Boolean {
        val v = _fullscreenView.value ?: return false
        _fullscreenView.value = null
        (v.parent as? ViewGroup)?.removeView(v)
        fullscreenCallback?.onCustomViewHidden()
        fullscreenCallback = null
        return true
    }

    private fun exitFullscreenIfShowing(wv: WebView?) {
        if (fullscreenCallback == null) return
        if (wv != null && wv != currentWebView()) return // belongs to another tab
        exitFullscreen()
    }

    // ------------------------------------------------------------------ webview setup

    private fun configure(wv: WebView, tabId: Int) {
        wv.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            setSupportMultipleWindows(false) // target=_blank loads in the same tab (v1)
            builtInZoomControls = true
            displayZoomControls = false
            loadWithOverviewMode = true
            useWideViewPort = true
            mediaPlaybackRequiresUserGesture = false
            // Videos on https pages served over http are the norm on stream sites.
            mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
        }
        // Embedded players commonly need cross-site cookies (CDN + player host).
        CookieManager.getInstance().setAcceptThirdPartyCookies(wv, true)

        // A3: the page reports its <video>/<audio>/<source> elements to us.
        // The interface only ever receives a JSON string of URLs — nothing a
        // hostile page can usefully call.
        wv.addJavascriptInterface(GrabberInterface(this, tabId), "deepflux")

        wv.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                return when (url.scheme?.lowercase()) {
                    "http", "https", "about", "data", "blob", "javascript" -> false // WebView renders
                    "magnet" -> {
                        // The in-app version of the manifest's magnet: filter —
                        // one tap on any site adds the torrent directly.
                        torrents.addMagnet(url.toString())
                        true
                    }
                    "mailto", "tel" -> {
                        runCatching {
                            activityContext.startActivity(
                                Intent(Intent.ACTION_VIEW, url).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                            )
                        }
                        true
                    }
                    else -> true // intent:, market:, custom schemes: swallow (scheme-hijack guard)
                }
            }

            override fun shouldInterceptRequest(
                view: WebView,
                request: WebResourceRequest,
            ): WebResourceResponse? {
                // Adblock (desktop dlmgr/adblock.py concept): the blocklist
                // match happens before the request ever leaves the app.
                val host = request.url.host ?: ""
                if (request.method.equals("GET", true) && Adblock.isBlocked(host)) {
                    blockedCount++
                    Log.i(TAG, "adblock: $host")
                    return WebResourceResponse("text/plain", "utf-8", java.io.ByteArrayInputStream(ByteArray(0)))
                }
                // A3 sniffing: OBSERVE only (return null = WebView fetches it
                // itself — proxying would break streaming/seeking). Runs on
                // WebView IO threads: the record path hops to main.
                if (request.method.equals("GET", true)) {
                    sniffVideoUrl(tabId, request.url.toString())
                }
                return null
            }

            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                updateInfo(view) {
                    it.copy(
                        url = url,
                        loading = true,
                        progress = 5,
                        secure = url.startsWith("https"),
                        error = "",
                    )
                }
                // New page = fresh grabber state for this tab.
                mainHandler.post {
                    clearVideos(tabId)
                    linksByTab.remove(tabId)
                    publishLinks()
                }
            }

            override fun onReceivedError(
                view: WebView,
                request: WebResourceRequest,
                error: android.webkit.WebResourceError,
            ) {
                // MAIN-FRAME failures only: a dead ad CDN must not blank the
                // page. Surfaced as an in-app error page with Retry (v1.3) —
                // before this, DNS failures/airplane mode left the WebView's
                // bare gray error page with no way back.
                if (!request.isForMainFrame) return
                updateInfo(view) {
                    it.copy(
                        loading = false,
                        progress = 100,
                        error = (error.description?.toString() ?: "load failed").take(160),
                    )
                }
            }

            override fun onPageFinished(view: WebView, url: String) {
                updateInfo(view) { it.copy(loading = false, progress = 100, url = url) }
                // History record (§13 #3) — at finish time the title may
                // still be the URL; onReceivedTitle patches the top entry.
                store.recordVisit(url, tabsInternal.find { it.id == tabId }?.info?.title ?: "")
                scanForVideos(view, tabId, delayed = false)
                // Players attach <video> elements seconds after "finished" —
                // a second pass catches the late binders.
                scanForVideos(view, tabId, delayed = true)
                // §13 #9: page-links scan rides the same schedule.
                scanForLinks(view, tabId, delayed = false)
                scanForLinks(view, tabId, delayed = true)
            }
        }

        wv.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView, newProgress: Int) {
                updateInfo(view) { it.copy(progress = newProgress, loading = newProgress < 100) }
            }

            override fun onReceivedTitle(view: WebView, title: String?) {
                updateInfo(view) { it.copy(title = (title ?: "").take(60)) }
                // History entries recorded at page-finish often still carry
                // the URL as their title — patch the newest visit.
                store.updateTitleIfLatest(view.url ?: "", (title ?: "").take(120))
                // Videos sniffed before the title arrived are named by it —
                // backfill blank titles (desktop: document.title naming).
                val t = prettyTitle(title ?: "")
                if (t.isNotBlank()) {
                    videosByTab[tabId]?.let { vids ->
                        var changed = false
                        for (i in vids.indices) {
                            if (vids[i].title.isBlank()) {
                                vids[i] = vids[i].copy(title = t)
                                changed = true
                            }
                        }
                        if (changed && tabId == _activeId.value) publishVideos()
                    }
                }
            }

            override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                fullscreenCallback?.let { it.onCustomViewHidden() }
                _fullscreenView.value = view
                fullscreenCallback = callback
            }

            override fun onHideCustomView() {
                exitFullscreen()
            }

            override fun onGeolocationPermissionsShowPrompt(
                origin: String?,
                callback: GeolocationPermissions.Callback?,
            ) {
                // A torrent/media browser has no business geolocating.
                callback?.invoke(origin, false, false)
            }
        }

        wv.setDownloadListener(downloadListener)
    }

    /**
     * The single interception choke point: fires for everything the renderer
     * can't display — torrent responses AND generic file downloads — with
     * the real server MIME type and the browsing session's cookies/UA.
     */
    private val downloadListener = DownloadListener { url, userAgent, contentDisposition, mimetype, contentLength ->
        val referer = currentWebView()?.url ?: ""
        Log.i(TAG, "browser download: $mimetype $url")
        if (isTorrent(mimetype, url)) {
            // The listener fires on main; fetchTorrentToCache routes there
            // (WebView access) and spawns its own workers.
            fetchTorrentToCache(url, userAgent, referer)
        } else {
            // Plain file → the A1 engine, with the session's credentials so
            // authenticated CDNs don't 403 the retry.
            val headers = buildMap {
                put("User-Agent", userAgent.ifBlank { downloadsDefaultUa() })
                if (referer.startsWith("http")) put("Referer", referer)
                cookieFor(url, referer)?.let { put("Cookie", it) }
            }
            val name = parseDispositionName(contentDisposition)
            val added = downloads.add(url, headers, name)
            if (added == null) {
                torrents.postNotice("Unsupported download: $url")
            } else {
                torrents.postNotice("Downloading ${added.fileName}")
            }
        }
    }

    private fun isTorrent(mimetype: String?, url: String): Boolean =
        mimetype.equals("application/x-bittorrent", ignoreCase = true) ||
            mimetype.equals("application/torrent", ignoreCase = true) ||
            url.substringBefore('?').endsWith(".torrent", ignoreCase = true)

    /**
     * Fetch the .torrent bytes ourselves (the DownloadListener only gives a URL).
     * MAIN THREAD ONLY — it reads the active WebView to route (see the
     * links-torrent crash lesson).
     *
     * PRIMARY lane for same-site taps: an in-page `fetch()` — it rides the
     * WebView's OWN network stack, so the request carries the exact cookies
     * AND the browser's TLS fingerprint. That matters on Cloudflare-walled
     * trackers (iptorrents): CF binds cf_clearance to the fingerprint that
     * earned it, so replaying session cookies from our Java/OkHttp stack can
     * be challenged (owner-reported "logged in, still fails" 2026-11-16; the
     * surrit lesson measured the same class). Falls back to the OkHttp lane
     * when no page context exists or the JS fetch fails.
     */
    private fun fetchTorrentToCache(url: String, userAgent: String, referer: String) {
        val wv = currentWebView()
        val pageUrl = runCatching { wv?.url }.getOrNull()
        if (wv != null && pageUrl != null && pageUrl.startsWith("http") && originsRelated(url, pageUrl)) {
            fetchTorrentViaPage(wv, url) { fetchTorrentViaHttpAsync(url, userAgent, referer) }
        } else {
            fetchTorrentViaHttpAsync(url, userAgent, referer)
        }
    }

    private fun fetchTorrentViaHttpAsync(url: String, userAgent: String, referer: String) {
        Thread({
            fetchTorrentViaHttp(url, userAgent, referer)
        }, "browser-torrent").apply { isDaemon = true }.start()
    }

    /** Armed by [fetchTorrentViaPage]; delivered exactly once by the JS bridge. */
    private var pendingTorrentFetch: ((String?) -> Unit)? = null

    /** Marshals the bridge result (any thread) to the armed handler exactly once. */
    fun deliverTorrentFetchResult(raw: String?) {
        val cb = synchronized(this) {
            val c = pendingTorrentFetch
            pendingTorrentFetch = null
            c
        }
        cb?.invoke(raw)
    }

    /**
     * Same-origin fetch() inside the live page — browser-grade auth + TLS.
     * evaluateJavascript does NOT await promises (the first cut returned
     * "[object Promise]" instantly and always fell back — caught live on the
     * owner's real IPT login 2026-11-16), so the result rides the
     * `deepflux` JS bridge from the promise's .then, with a timeout.
     */
    private fun fetchTorrentViaPage(wv: WebView, url: String, fallback: () -> Unit) {
        val safeUrl = url.replace("'", "%27")
        val js = "(function(){try{function done(o){window.deepflux.torrentResult(JSON.stringify(o));}" +
            "fetch('$safeUrl',{credentials:'include'})" +
            ".then(function(r){return r.arrayBuffer().then(function(b){" +
            "if(!r.ok){done({status:r.status,url:r.url});return;}" +
            "if(b.byteLength<1||b.byteLength>${MAX_TORRENT_BYTES}){done({err:'bad size'});return;}" +
            "var s='';var u=new Uint8Array(b);var CH=0x8000;" +
            "for(var i=0;i<u.length;i+=CH){s+=String.fromCharCode.apply(null,u.subarray(i,i+CH));}" +
            "done({url:r.url,b64:btoa(s)});});})" +
            ".catch(function(e){done({err:''+e});});" +
            "}catch(e){window.deepflux.torrentResult(JSON.stringify({err:''+e}));}})()"
        val timeout = Runnable {
            Log.w(TAG, "in-page torrent fetch: timed out")
            deliverTorrentFetchResult(null)
        }
        synchronized(this) {
            pendingTorrentFetch = { raw ->
                mainHandler.removeCallbacks(timeout)
                handlePageFetchResult(raw, url, fallback)
            }
        }
        mainHandler.postDelayed(timeout, 20_000)
        mainHandler.post {
            runCatching {
                wv.evaluateJavascript(js) { /* result arrives via the bridge */ }
            }.onFailure {
                Log.w(TAG, "in-page torrent fetch failed to start: ${it.message}")
                deliverTorrentFetchResult(null)
            }
        }
    }

    /** Bridge result → the same accept/reject/notice flow as the HTTP lane. */
    private fun handlePageFetchResult(raw: String?, requestedUrl: String, fallback: () -> Unit) {
        if (raw == null) {
            fallback() // timed out / never started — network lane retries and reports
            return
        }
        val payload = runCatching { org.json.JSONObject(raw) }.getOrNull()
        if (payload == null) {
            Log.w(TAG, "in-page torrent fetch: bad payload")
            fallback()
            return
        }
        val err = payload.optString("err", "")
        if (err.isNotEmpty()) {
            Log.w(TAG, "in-page torrent fetch: $err")
            fallback()
            return
        }
        val b64 = payload.optString("b64", "")
        if (b64.isEmpty()) {
            val status = payload.optInt("status", 0)
            if (status >= 400) {
                // e.g. Cloudflare 403 with the session replayed cross-stack.
                torrents.postNotice("Torrent fetch failed: HTTP $status")
                return
            }
            Log.w(TAG, "in-page torrent fetch: empty payload")
            fallback()
            return
        }
        Thread({
            runCatching {
                val bytes = android.util.Base64.decode(b64, android.util.Base64.NO_WRAP)
                if (bytes.isEmpty() || bytes.size > MAX_TORRENT_BYTES) {
                    torrents.postNotice("Torrent file too large or empty")
                    return@Thread
                }
                if (bytes[0] != 'd'.code.toByte()) {
                    torrents.postNotice(notTorrentReason(payload.optString("url"), requestedUrl))
                    return@Thread
                }
                val tmp = File(activityContext.cacheDir, "web-${System.currentTimeMillis()}.torrent")
                tmp.writeBytes(bytes)
                torrents.addTorrentFile(tmp) // consumes/deletes the file
            }.onFailure {
                Log.w(TAG, "torrent fetch failed: ${it.message}")
                torrents.postNotice("Torrent download failed — ${it.message ?: "network error"}")
            }
        }, "page-torrent").apply { isDaemon = true }.start()
    }

    /** OkHttp (h2) lane — used when no same-site page context exists. */
    private fun fetchTorrentViaHttp(url: String, userAgent: String, referer: String) {
        runCatching {
            val resp = open(url, userAgent, referer)
            if (resp.code != 200) {
                torrents.postNotice("Torrent fetch failed: HTTP ${resp.code}")
                resp.close()
                return
            }
            val body = resp.body
            if (body == null) {
                torrents.postNotice("Torrent fetch failed: empty response")
                return
            }
            val bytes = body.use { readCapped(it.byteStream(), MAX_TORRENT_BYTES + 1) }
            if (bytes.isEmpty() || bytes.size > MAX_TORRENT_BYTES) {
                torrents.postNotice("Torrent file too large or empty")
                return
            }
            if (bytes[0] != 'd'.code.toByte()) { // bencode dict — quick sanity
                torrents.postNotice(notTorrentReason(resp.request.url.toString(), url))
                return
            }
            val tmp = File(activityContext.cacheDir, "web-${System.currentTimeMillis()}.torrent")
            tmp.writeBytes(bytes)
            torrents.addTorrentFile(tmp) // consumes/deletes the file
        }.onFailure {
            // Never a silent dead tap (found live 2026-11-16): URL/network
            // failures used to only hit logcat, looking like "nothing happens".
            Log.w(TAG, "torrent fetch failed: ${it.message}")
            torrents.postNotice("Torrent download failed — ${it.message ?: "network error"}")
        }
    }

    /**
     * Why did a non-bencode body come back? Private trackers (iptorrents)
     * bounce an unauthenticated .torrent fetch to their login page with a
     * 200 — "Not a valid .torrent file" hid the actual fix (log in inside
     * the Browser tab). Say that when the redirect chain landed on a
     * login-shaped URL.
     */
    private fun notTorrentReason(finalUrl: String, requestedUrl: String): String {
        val path = runCatching { URL(finalUrl).path.lowercase() }.getOrNull() ?: ""
        val bouncedToLogin = finalUrl != requestedUrl &&
            (path.contains("login") || path.contains("signin") || path.contains("auth"))
        return if (bouncedToLogin) {
            "Login required — sign in on the site in the Browser tab, then try again"
        } else {
            "Not a valid .torrent file"
        }
    }

    /**
     * Transport is OkHttp (1.3.4 lesson, applied to this lane 2026-11-16):
     * it negotiates HTTP/2, which a Chrome User-Agent is expected to speak —
     * HttpURLConnection's HTTP/1.1 + Conscrypt shape is a bot signal for
     * Cloudflare-walled sites (measured on surrit.com media CDNs; IPT is
     * behind the same wall). Redirects are followed MANUALLY so each hop
     * gets the correct per-host cookie gating.
     */
    private val torrentHttp: okhttp3.OkHttpClient = okhttp3.OkHttpClient.Builder()
        .connectTimeout(15, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .followRedirects(false)
        .build()

    private fun open(url: String, userAgent: String, referer: String): okhttp3.Response {
        // Defense-in-depth: a href with a RAW space is illegal for strict
        // clients (OkHttp throws); spaces are always meant encoded.
        var current = url.replace(" ", "%20")
        var hops = 0
        while (true) {
            val rb = okhttp3.Request.Builder().url(current)
                .header("User-Agent", userAgent.ifBlank { downloadsDefaultUa() })
            if (referer.startsWith("http")) rb.header("Referer", referer)
            cookieFor(current, referer)?.let { rb.header("Cookie", it) }
            val resp = torrentHttp.newCall(rb.build()).execute()
            if (resp.code in 300..399) {
                val loc = resp.header("Location")
                resp.close()
                if (loc.isNullOrBlank()) throw IllegalStateException("redirect without Location")
                current = URL(URL(current), loc).toString()
                if (++hops > 6) throw IllegalStateException("too many redirects")
                continue
            }
            return resp
        }
    }

    private fun readCapped(input: InputStream, cap: Int): ByteArray {
        val out = java.io.ByteArrayOutputStream()
        val buf = ByteArray(16 * 1024)
        input.use { src ->
            while (true) {
                val n = src.read(buf)
                if (n == -1) break
                if (out.size() + n > cap) return ByteArray(0) // over cap = fail closed
                out.write(buf, 0, n)
            }
        }
        return out.toByteArray()
    }

    private fun downloadsDefaultUa(): String = space.deepflux.android.dlmgr.HttpDownloadRepository.USER_AGENT

    /**
     * Desktop `_browser_origins_related` rule: attach the session's cookies
     * ONLY when the target is same-site with the page. Cross-site CDNs
     * (missav → surrit.com) must get a cookie-less request — replaying the
     * WebView's third-party Cloudflare cookies from our Java TLS stack
     * earns an instant 403, while a clean UA+Referer request passes
     * (measured live 2026-11-15; this was "every grabber download failed").
     */
    private fun originsRelated(urlA: String, urlB: String): Boolean {
        val a = runCatching { java.net.URI(urlA).host?.lowercase()?.removePrefix("www.") }.getOrNull() ?: return false
        val b = runCatching { java.net.URI(urlB).host?.lowercase()?.removePrefix("www.") }.getOrNull() ?: return false
        if (a.isBlank() || b.isBlank()) return false
        return a == b || a.endsWith(".$b") || b.endsWith(".$a")
    }

    /** Cookie header for [url] — only when same-site with [referer]. */
    private fun cookieFor(url: String, referer: String): String? =
        if (referer.startsWith("http") && originsRelated(url, referer)) {
            CookieManager.getInstance().getCookie(url)
        } else {
            null
        }

    /** One-tap actions from the links sheet (§13 #9), with the page session. */
    fun addMagnetFromPage(magnet: String) = torrents.addMagnet(magnet)

    fun downloadTorrentLink(url: String) {
        val wv = currentWebView() ?: return
        // WebView state (settings/url) must be read on the MAIN thread —
        // reading it inside the worker threw "A WebView method was called on
        // thread 'links-torrent'" and KILLED the whole app (FATAL, repro'd
        // 2026-11-16 on the iptorrents download link).
        val ua = wv.settings.userAgentString ?: ""
        val referer = wv.url ?: ""
        // Runs on main (sheet click) — the router reads the WebView here and
        // spawns its own workers.
        fetchTorrentToCache(url, ua, referer)
    }

    fun downloadFileLink(url: String) {
        val wv = currentWebView()
        val referer = wv?.url ?: ""
        val headers = buildMap {
            put("User-Agent", wv?.settings?.userAgentString ?: downloadsDefaultUa())
            if (referer.startsWith("http")) put("Referer", referer)
            cookieFor(url, referer)?.let { put("Cookie", it) }
        }
        val added = downloads.add(url, headers)
        if (added == null) torrents.postNotice("Unsupported download: $url")
        else torrents.postNotice("Downloading ${added.fileName}")
    }

    /** `filename="x.torrent"` and RFC 5987 forms, from a Content-Disposition header. */
    private fun parseDispositionName(header: String?): String? {
        if (header.isNullOrBlank()) return null
        Regex("filename\\*=UTF-8''([^;]+)", RegexOption.IGNORE_CASE).find(header)?.groupValues?.get(1)?.let {
            return runCatching { URLDecoder.decode(it.trim('\"'), "UTF-8") }.getOrNull()
        }
        Regex("filename=\"?([^\";]+)\"?", RegexOption.IGNORE_CASE).find(header)?.groupValues?.get(1)?.let {
            return it.trim().replace(Regex("[\\\\/:*?\"<>|]"), "_").ifBlank { null }
        }
        return null
    }

    // ------------------------------------------------------------------ A3 video grabber

    /** URL-shape sniff from shouldInterceptRequest (IO thread → hop to main). */
    private fun sniffVideoUrl(tabId: Int, url: String) {
        if (!url.startsWith("http", true)) return
        val path = url.substringBefore('?').substringBefore('#')
        // .ts is deliberately absent: HLS segments would flood the list —
        // the m3u8 manifest is the grabbable unit. Same for fMP4 SEGMENTS:
        // some CDNs (growcdn: 180640836_240p_h264_1434_<token>_<n>.mp4)
        // name their HLS fragments ".mp4" — each would spam the sheet.
        if (!SNIFF_EXT.containsMatchIn(path)) return
        if (isSegmentNoise(path)) return
        mainHandler.post { addVideo(tabId, url) }
    }

    /** fMP4 init/segment shapes — media fragments, not grabbable videos. */
    private fun isSegmentNoise(path: String): Boolean {
        val name = path.substringAfterLast('/')
        return name.contains("_init_") || SEGMENT_SHAPE.containsMatchIn(name)
    }

    /** Inject the DOM scan; `delayed` re-runs it for late-binding players. */
    private fun scanForVideos(view: WebView, tabId: Int, delayed: Boolean) {
        val run = Runnable {
            if (tabsInternal.none { it.id == tabId }) return@Runnable
            runCatching { view.evaluateJavascript(SCAN_JS, null) }
        }
        if (delayed) mainHandler.postDelayed(run, 3000) else run.run()
    }

    /** JS-bridge entry: a JSON array of media URLs found in the page DOM. */
    private fun reportVideos(tabId: Int, json: String) {
        runCatching {
            val arr = org.json.JSONArray(json)
            for (i in 0 until arr.length().coerceAtMost(20)) {
                val u = arr.optString(i)
                if (u.isNotBlank()) addVideo(tabId, u)
            }
        }
    }

    private fun addVideo(tabId: Int, url: String) {
        val tab = tabsInternal.find { it.id == tabId } ?: return
        val list = videosByTab.getOrPut(tabId) { mutableListOf() }
        val clean = url.substringBefore('#')
        if (list.size >= MAX_VIDEOS_PER_TAB) return
        val quality = qualityOf(clean)
        val title = prettyTitle(tab.info.title)
        val kind = classify(clean)
        // ONE entry per video (desktop rule): URLs that differ only by a
        // quality token (…/{uuid}/240p/… vs …/1080p/…) are the SAME video —
        // keep the better one (a master manifest, else the highest quality).
        val key = groupKey(clean)
        val idx = list.indexOfFirst { groupKey(it.url) == key }
        if (idx >= 0) {
            val existing = list[idx]
            if (!isBetterGrab(clean, quality, existing)) return
            list[idx] = GrabbedVideo(
                clean,
                kind,
                existing.referer.ifBlank { tab.webView.url ?: "" },
                title.ifBlank { existing.title },
                quality.ifBlank { existing.quality },
            )
        } else {
            list.add(GrabbedVideo(clean, kind, tab.webView.url ?: "", title, quality))
        }
        if (tabId == _activeId.value) publishVideos()
    }

    /** Quality token in the URL path/filename ("240p", "1080p", …). */
    private fun qualityOf(url: String): String =
        QUALITY_RE.find(url.substringBefore('?'))?.groupValues?.get(1)?.lowercase() ?: ""

    /**
     * URL identity used to collapse quality variants of ONE video:
     *  - quality-only path segments are dropped (`/720p/video.m3u8` → `/video.m3u8`)
     *  - quality tokens in the filename are masked (`180640836_240p.m3u8` → `…_#Q#.m3u8`)
     *  - generic manifest filenames (playlist/master/index/video/manifest.m3u8)
     *    are dropped, so a master and its sub-playlists land on the same key
     *    (missav/surrit: `{uuid}/playlist.m3u8` + `{uuid}/720p/video.m3u8`).
     * Real filenames that carry the video's identity are KEPT, so two
     * different files in one directory never merge.
     */
    private fun groupKey(url: String): String {
        val base = url.substringBefore('#').substringBefore('?')
        val segments = base.substringAfter("//").split('/')
        val host = segments.first()
        val out = mutableListOf<String>()
        for (i in 1 until segments.size) {
            val seg = segments[i]
            if (QUALITY_RE.matches(seg)) continue // bare "/720p/" segment
            if (i == segments.size - 1 && MANIFEST_NAME.matches(seg)) continue // trailing playlist.m3u8 etc.
            out.add(seg.replace(QUALITY_RE, "#Q#"))
        }
        return "$host/${out.joinToString("/")}"
    }

    private fun isBetterGrab(url: String, quality: String, existing: GrabbedVideo): Boolean {
        val newMaster = MASTER_HINT.containsMatchIn(url)
        val oldMaster = MASTER_HINT.containsMatchIn(existing.url)
        if (newMaster != oldMaster) return newMaster
        val n = quality.dropLast(1).toIntOrNull() ?: -1
        val o = existing.quality.dropLast(1).toIntOrNull() ?: -1
        return n > o
    }

    private fun clearVideos(tabId: Int) {
        videosByTab.remove(tabId)
        if (tabId == _activeId.value) publishVideos()
    }

    private fun publishVideos() {
        _videos.value = videosByTab[_activeId.value]?.toList() ?: emptyList()
    }

    /** Page title as a video name: drop trailing " | Site" / " - Site" tags. */
    private fun prettyTitle(raw: String): String {
        var t = raw.trim()
        while (true) {
            val pipe = t.lastIndexOf(" | ")
            val dash = t.lastIndexOf(" - ")
            val i = maxOf(pipe, dash)
            if (i <= 24) break // short titles / "Artist - Song" stay intact
            t = t.substring(0, i).trimEnd(' ', '|', '-')
        }
        return t.take(80)
    }

    private fun classify(url: String): GrabbedVideo.Kind {
        val path = url.substringBefore('?').lowercase()
        return when {
            url.startsWith("blob:", true) -> GrabbedVideo.Kind.BLOB
            path.endsWith(".m3u8") -> GrabbedVideo.Kind.HLS
            path.endsWith(".mpd") -> GrabbedVideo.Kind.DASH
            else -> GrabbedVideo.Kind.PROGRESSIVE
        }
    }

    /**
     * Hand a detected video to the download engine with the page's session
     * (Referer + cookies + the WebView's own UA) so hotlink-protected CDNs
     * accept the request. Direct files and HLS streams are downloadable;
     * DASH and in-page blobs are flagged only.
     */
    fun downloadVideo(video: GrabbedVideo) {
        val hls = video.kind == GrabbedVideo.Kind.HLS
        if (video.kind != GrabbedVideo.Kind.PROGRESSIVE && !hls) {
            torrents.postNotice("This kind of stream cannot be downloaded yet")
            return
        }
        val ua = currentWebView()?.settings?.userAgentString ?: downloadsDefaultUa()
        val headers = buildMap {
            put("User-Agent", ua)
            if (video.referer.startsWith("http")) put("Referer", video.referer)
            cookieFor(video.url, video.referer)?.let { put("Cookie", it) }
        }
        // Every site calls its playlist "master.m3u8" or "index.m3u8" — name
        // the file after the page instead. The engine corrects the extension
        // (.ts / .mp4) once it has read the playlist.
        val name = if (hls) pageFileStem() + ".ts" else null
        val added = downloads.add(video.url, headers, name, hls = hls)
        if (added == null) torrents.postNotice("Unsupported video URL")
        else torrents.postNotice("Downloading ${added.fileName}")
    }

    private fun pageFileStem(): String =
        (active?.title ?: "")
            .replace(Regex("[\\\\/:*?\"<>|\\p{Cntrl}]"), " ")
            .trim()
            .take(80)
            .ifBlank { "video" }

    private class GrabberInterface(private val controller: BrowserController, private val tabId: Int) {
        @JavascriptInterface
        fun report(json: String) {
            if (json.length > 64_000) return // a page spamming megabytes is not a video list
            controller.mainHandler.post { controller.reportVideos(tabId, json) }
        }

        /** In-page .torrent fetch delivery (evaluateJavascript can't await promises). */
        @JavascriptInterface
        fun torrentResult(json: String) {
            if (json.length > MAX_TORRENT_BYTES * 2) return // ~base64 of the cap
            controller.mainHandler.post { controller.deliverTorrentFetchResult(json) }
        }
    }

    // ------------------------------------------------------------------ §13 #9 page-links scan

    /** Inject the anchor scan; same delayed double-pass as the video scan. */
    private fun scanForLinks(view: WebView, tabId: Int, delayed: Boolean) {
        val run = Runnable {
            if (tabsInternal.none { it.id == tabId }) return@Runnable
            runCatching {
                view.evaluateJavascript(LINKS_JS) { json ->
                    mainHandler.post { reportLinks(tabId, json) }
                }
            }
        }
        if (delayed) mainHandler.postDelayed(run, 3200) else run.run()
    }

    /** Parse the scan's JSON string (a quoted JS string or null). */
    private fun reportLinks(tabId: Int, raw: String?) {
        if (raw.isNullOrBlank() || raw == "null") return
        val json = runCatching {
            org.json.JSONArray(org.json.JSONTokener(raw).nextValue().toString())
        }.getOrNull() ?: return
        val list = linksByTab.getOrPut(tabId) { mutableListOf() }
        list.clear()
        for (i in 0 until json.length().coerceAtMost(MAX_PAGE_LINKS)) {
            val o = json.optJSONObject(i) ?: continue
            val url = o.optString("u")
            if (!url.startsWith("http") && !url.startsWith("magnet:")) continue
            val kind = when {
                url.startsWith("magnet:", true) -> PageLink.Kind.TORRENT_MAGNET
                url.substringBefore('?').endsWith(".torrent", true) -> PageLink.Kind.TORRENT_FILE
                SNIFF_EXT.containsMatchIn(url.substringBefore('?')) -> PageLink.Kind.VIDEO
                else -> PageLink.Kind.PAGE
            }
            val text = o.optString("t").trim().take(100)
            if (kind == PageLink.Kind.PAGE && text.isEmpty()) continue
            if (list.any { it.url == url }) continue
            list.add(PageLink(text, url, kind))
        }
        if (tabId == _activeId.value) publishLinks()
    }

    /**
     * Synchronous scan for the agent's scan_page tool: evaluates the scan
     * in the ACTIVE tab on the main thread and waits (bounded) for the
     * result. Returns the formatted list or null when no page is showing.
     * EVERY WebView touch (including getUrl) must happen on main — reading
     * view.url on the calling thread throws (caught live by the agent
     * round 2026-11-15).
     */
    fun scanPageSync(): String? {
        val latch = java.util.concurrent.CountDownLatch(1)
        val out = arrayOfNulls<String>(1)
        mainHandler.post {
            try {
                val view = currentWebView()
                val pageUrl = view?.url
                if (view == null || pageUrl == null || !pageUrl.startsWith("http")) {
                    latch.countDown()
                    return@post
                }
                view.evaluateJavascript(LINKS_JS) { json ->
                    out[0] = json ?: "null"
                    latch.countDown()
                }
            } catch (e: Exception) {
                latch.countDown()
            }
        }
        if (!latch.await(6, java.util.concurrent.TimeUnit.SECONDS)) return "scan timed out"
        val raw = out[0] ?: return null
        val sb = StringBuilder()
        val torrent = mutableListOf<String>()
        val video = mutableListOf<String>()
        val page = mutableListOf<String>()
        runCatching {
            val arr = org.json.JSONArray(org.json.JSONTokener(raw).nextValue().toString())
            for (i in 0 until arr.length().coerceAtMost(MAX_PAGE_LINKS)) {
                val o = arr.optJSONObject(i) ?: continue
                val u = o.optString("u")
                val t = o.optString("t").trim().take(80)
                when {
                    u.startsWith("magnet:") -> torrent.add("- [magnet] $t\n  $u")
                    u.substringBefore('?').endsWith(".torrent", true) -> torrent.add("- [torrent file] $t\n  $u")
                    SNIFF_EXT.containsMatchIn(u.substringBefore('?')) -> video.add("- [video] $t\n  $u")
                    else -> page.add("- $t — $u")
                }
            }
        }
        if (torrent.isNotEmpty()) sb.append("TORRENT LINKS:\n").append(torrent.joinToString("\n")).append('\n')
        if (video.isNotEmpty()) sb.append("VIDEO LINKS:\n").append(video.joinToString("\n")).append('\n')
        if (page.isNotEmpty()) sb.append("PAGE LINKS (first 25):\n").append(page.take(25).joinToString("\n"))
        return sb.toString().ifBlank { "no interesting links found on the page" }
    }

    private fun publishLinks() {
        _pageLinks.value = linksByTab[_activeId.value]?.toList() ?: emptyList()
    }

    // ------------------------------------------------------------------ state plumbing

    private fun updateInfo(id: Int, mutate: (TabInfo) -> TabInfo) {
        val t = tabsInternal.find { it.id == id } ?: return
        t.info = mutate(t.info)
        publish()
    }

    private fun updateInfo(view: WebView, mutate: (TabInfo) -> TabInfo) {
        val t = tabsInternal.find { it.webView === view } ?: return
        t.info = mutate(t.info.copy(canGoBack = view.canGoBack(), canGoForward = view.canGoForward()))
        publish()
    }

    private fun publish() {
        _tabs.value = tabsInternal.map { it.info }
    }

    private fun syncViews() {
        tabsInternal.forEach { it.webView.visibility = if (it.id == _activeId.value) View.VISIBLE else View.GONE }
    }

    companion object {
        private const val TAG = "BrowserController"
        private const val MAX_TORRENT_BYTES = 32 * 1024 * 1024
        private const val MAX_VIDEOS_PER_TAB = 12
        private const val MAX_PAGE_LINKS = 140

        /** The default homepage — also the fallback for a blank setting. */
        const val DEFAULT_HOMEPAGE = "https://deepflux.space"

        // Anchor + loose-magnet scan (§13 #9): every <a href> as an absolute
        // URL with its trimmed text, plus magnets found anywhere in the HTML
        // (torrent sites often put them in buttons/scripts, not anchors).
        private const val LINKS_JS = "(function(){try{" +
            "var out=[];var seen={};" +
            "var as=document.querySelectorAll('a[href]');" +
            "for(var i=0;i<as.length&&out.length<120;i++){var a=as[i];" +
            "var h=a.getAttribute('href');if(!h||h.length>1000)continue;" +
            // Torrent sites often embed download hrefs with RAW spaces in the
            // name (".../Name S01.torrent") — the URL constructor THROWS on
            // those (verified live on an iptorrents MobLand link, 2026-11-16)
            // and the row silently vanished from the sheet. Retry with spaces
            // pre-encoded before giving up.
            "var u;try{u=new URL(h,location.href).href;}" +
            "catch(e){try{u=new URL(h.replace(/ /g,'%20'),location.href).href;}catch(e2){continue;}}" +
            "if(!u||seen[u])continue;seen[u]=1;" +
            "out.push({u:u,t:(a.textContent||'').replace(/\\s+/g,' ').trim().substring(0,100)});}" +
            "var m=document.documentElement.outerHTML.match(/magnet:\\?xt=urn:btih:[a-zA-Z0-9]+[^\\s\"'<>]*/g)||[];" +
            "for(var j=0;j<m.length&&out.length<140;j++){if(!seen[m[j]]){seen[m[j]]=1;out.push({u:m[j],t:'(magnet on page)'});}}" +
            "return JSON.stringify(out);" +
            "}catch(e){return '[]';}})()"

        private val SNIFF_EXT = Regex("\\.(mp4|m4v|webm|mkv|mov|avi|flv|m3u8|mpd)$", RegexOption.IGNORE_CASE)

        // Quality tokens in media URLs ("/720p/", "_240p", "x1080p"…).
        private val QUALITY_RE = Regex("(\\d{3,4}p)", RegexOption.IGNORE_CASE)

        // Generic manifest filenames that carry no video identity.
        private val MANIFEST_NAME = Regex("(playlist|master|index|video|manifest)\\.m3u8", RegexOption.IGNORE_CASE)

        // fMP4 segment filenames: <name>_<index>_<token8+>_<n>.mp4 (and .m4s).
        private val SEGMENT_SHAPE = Regex("_\\d+_[A-Za-z0-9]{8,}(_\\d+)?\\.(mp4|m4s|ts|aac)$")

        // Master-manifest hints — a master beats any per-quality variant.
        private val MASTER_HINT = Regex("(playlist|master|index)\\.m3u8", RegexOption.IGNORE_CASE)

        // DOM scan: <video> elements and <source> children of <video>.
        // currentSrc/src are absolute URLs; errors are swallowed in-page.
        private const val SCAN_JS = "(function(){try{" +
            "var out=[];var els=document.querySelectorAll('video,source');" +
            "for(var i=0;i<els.length;i++){var e=els[i];" +
            "if(e.tagName==='SOURCE'&&(!e.parentElement||e.parentElement.tagName!=='VIDEO'))continue;" +
            "var u=e.currentSrc||e.src||'';if(u&&u.length<2000)out.push(u);}" +
            "if(out.length&&window.deepflux)window.deepflux.report(JSON.stringify(out.slice(0,20)));" +
            "}catch(e){}})();"
    }
}
