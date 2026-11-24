package space.deepflux.android.rss

import android.content.Context
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import space.deepflux.android.dlmgr.HttpDownloadRepository
import space.deepflux.android.engine.MagnetParser
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.ui.Strings
import java.io.File
import java.net.URL
import java.security.MessageDigest

/**
 * RSS feeds + auto-download (plan §13 #4) — the Android twin of the
 * desktop's RSS tooling. Feeds live in a JSON sidecar; items are fetched
 * on demand and by the service poll loop; an item whose title matches a
 * feed's keywords is handed to the right engine (magnet → torrent engine,
 * .torrent link → fetched + added, other http → the A1 HTTP engine).
 *
 * Desktop rules kept verbatim:
 *  * an item is marked SEEN only when it was actually handed to an engine
 *    (a failed auto-download retries on the next poll);
 *  * ONE worker at a time (overlap guard) — polls never pile up;
 *  * feed bodies are read with a hard 5 MiB cap (RssClient).
 *
 * Android reality differences: no always-on process — polling happens
 * while the app/service is alive (TorrentService loop + app start) and on
 * demand (UI refresh, agent tools). Adding a feed BASELINES the current
 * items as seen, so auto-download applies only to items published from
 * then on — no surprise backlog mass-download.
 */
class RssRepository(
    private val context: Context,
    private val torrents: TorrentRepository,
    private val http: HttpDownloadRepository,
    /** Public-address policy for feeds the agent added (tests inject a fake resolver). */
    private val feedUrlPolicy: (String) -> String? = space.deepflux.android.agent.PublicUrls::rejection,
) {

    data class RssFeed(
        val id: String,
        val url: String,
        val title: String,
        val autoDownload: Boolean = false,
        /** Title must contain one of these (case-insensitive) for auto-download. */
        val keywords: List<String> = emptyList(),
        val addedAt: Long = 0,
        val lastFetchAt: Long = 0,
        val lastError: String = "",
        /**
         * Added on the agent's say-so. The feed's address is then held to
         * the public internet on every poll — every redirect hop included
         * — exactly like a download the agent added. A feed the USER typed
         * is their own choice and may come from anywhere.
         */
        val publicOnly: Boolean = false,
    )

    private val file = File(context.filesDir, "rss_feeds.json")
    private val lock = Any()
    private val polling = java.util.concurrent.atomic.AtomicBoolean(false)

    private val _feeds = MutableStateFlow<List<RssFeed>>(emptyList())
    val feeds: StateFlow<List<RssFeed>> = _feeds.asStateFlow()

    /** Latest fetched items per feed id (empty until first refresh). */
    private val _itemsByFeed = MutableStateFlow<Map<String, List<RssClient.RssItem>>>(emptyMap())
    val itemsByFeed: StateFlow<Map<String, List<RssClient.RssItem>>> = _itemsByFeed.asStateFlow()

    private val _refreshing = MutableStateFlow<Set<String>>(emptySet())
    val refreshing: StateFlow<Set<String>> = _refreshing.asStateFlow()

    /** Seen item guids per feed id, capped to the newest SEEN_CAP. */
    private val seen = mutableMapOf<String, MutableSet<String>>()
    @Volatile private var lastPollAt = 0L

    /** User-visible notice (snackbar) — the app's shared channel. */
    fun postNotice(message: String) = torrents.postNotice(message)

    init {
        runCatching {
            if (file.exists()) {
                val arr = JSONArray(file.readText())
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    val id = o.optString("id")
                    if (id.isBlank()) continue
                    val feed = RssFeed(
                        id = id,
                        url = o.optString("url"),
                        title = o.optString("title"),
                        autoDownload = o.optBoolean("auto", false),
                        keywords = o.optJSONArray("keywords")?.let { ks ->
                            List(ks.length()) { ks.optString(it) }.filter { it.isNotBlank() }
                        } ?: emptyList(),
                        addedAt = o.optLong("at", 0),
                        lastFetchAt = o.optLong("fetched", 0),
                        lastError = o.optString("err", ""),
                        publicOnly = o.optBoolean("po", false),
                    )
                    seen[id] = o.optJSONArray("seen")?.let { ss ->
                        val linked = LinkedHashSet<String>()
                        for (k in 0 until ss.length()) linked.add(ss.optString(k))
                        linked
                    } ?: LinkedHashSet()
                    _feeds.value = _feeds.value + feed
                }
            }
        }.onFailure { Log.w(TAG, "load failed: ${it.message}") }
    }

    /** Fetch once to validate + title the feed, baseline everything as seen. */
    fun addFeed(
        rawUrl: String,
        keywords: List<String> = emptyList(),
        autoDownload: Boolean = false,
        publicOnly: Boolean = false,
    ): Result<RssFeed> {
        val url = rawUrl.trim()
        if (!url.startsWith("http://", true) && !url.startsWith("https://", true)) {
            return Result.failure(IllegalArgumentException("not an http(s) URL"))
        }
        if (_feeds.value.any { it.url == url }) return Result.failure(IllegalArgumentException("feed already added"))
        return runCatching {
            val content = RssClient.fetch(url, publicOnly, feedUrlPolicy)
            synchronized(lock) {
            val feed = RssFeed(
                id = idFor(url),
                url = url,
                title = content.title.ifBlank { URL(url).host ?: url },
                autoDownload = autoDownload,
                keywords = keywords.map { it.trim().lowercase() }.filter { it.isNotBlank() },
                addedAt = System.currentTimeMillis(),
                lastFetchAt = System.currentTimeMillis(),
                publicOnly = publicOnly,
            )
            _feeds.value = _feeds.value + feed
            _itemsByFeed.value = _itemsByFeed.value + (feed.id to content.items)
            // Baseline: everything currently in the feed is old news.
            seen[feed.id] = LinkedHashSet(content.items.map { it.guid }.takeLast(SEEN_CAP))
            persistLocked()
            feed
            }
        }
    }

    fun removeFeed(id: String) = synchronized(lock) {
        _feeds.value = _feeds.value.filterNot { it.id == id }
        seen.remove(id)
        _itemsByFeed.value = _itemsByFeed.value - id
        persistLocked()
    }

    fun setFeedConfig(id: String, autoDownload: Boolean, keywords: List<String>) = synchronized(lock) {
        val next = _feeds.value.map {
            if (it.id == id) it.copy(
                autoDownload = autoDownload,
                keywords = keywords.map { k -> k.trim().lowercase() }.filter { k -> k.isNotBlank() },
            ) else it
        }
        if (next != _feeds.value) {
            _feeds.value = next
            persistLocked()
        }
    }

    /** Fetch one feed now; updates the item cache, the feed's error line,
     *  and applies auto-download to newly-seen keyword matches. */
    fun refresh(id: String): Result<Int> {
        val feed = _feeds.value.find { it.id == id } ?: return Result.failure(IllegalArgumentException("no such feed"))
        _refreshing.value = _refreshing.value + id
        return try {
            val content = RssClient.fetch(feed.url, feed.publicOnly, feedUrlPolicy)
            synchronized(lock) {
                _itemsByFeed.value = _itemsByFeed.value + (id to content.items)
                _feeds.value = _feeds.value.map {
                    if (it.id == id) it.copy(
                        lastFetchAt = System.currentTimeMillis(),
                        lastError = "",
                        // Keep the feed title honest if the source renames itself.
                        title = it.title.ifBlank { content.title },
                    ) else it
                }
                persistLocked()
            }
            // Manual refresh = an explicit "check now": apply the watcher.
            if (feed.autoDownload && feed.keywords.isNotEmpty()) autoDownloadNew(feed)
            Result.success(content.items.size)
        } catch (e: Exception) {
            synchronized(lock) {
                _feeds.value = _feeds.value.map { if (it.id == id) it.copy(lastError = (e.message ?: "error").take(80)) else it }
                persistLocked()
            }
            Result.failure(e)
        } finally {
            _refreshing.value = _refreshing.value - id
        }
    }

    /**
     * The poll loop entry (TorrentService every 15 min + app start):
     * refresh every feed, auto-download keyword matches. Overlap-guarded
     * and rate-limited — a call inside the interval only runs MANUAL
     * refreshes' bookkeeping, no fetch storm.
     */
    fun pollIfDue(force: Boolean = false) {
        val now = System.currentTimeMillis()
        if (!force && now - lastPollAt < POLL_INTERVAL_MS) return
        if (!polling.compareAndSet(false, true)) return
        try {
            lastPollAt = now
            // refresh() itself applies auto-download for matched new items.
            _feeds.value.forEach { feed -> refresh(feed.id) }
        } finally {
            polling.set(false)
        }
    }

    private fun autoDownloadNew(feed: RssFeed) {
        val items = _itemsByFeed.value[feed.id] ?: return
        val known = seen[feed.id] ?: LinkedHashSet()
        items.filter { it.guid !in known && matches(it.title, feed.keywords) }
            .forEach { item ->
                // Marked seen only once an engine has really taken it. On the
                // first byte of the file alone, a .torrent whose import then
                // failed was never tried again (second review).
                handToEngine(item) { ok ->
                    if (ok) {
                        markSeen(feed.id, item.guid)
                        Log.i(TAG, "rss auto-download: ${item.title.take(60)}")
                        torrents.postNotice("RSS: added ${item.title.take(60)}")
                    }
                }
            }
    }

    private fun markSeen(feedId: String, guid: String) = synchronized(lock) {
        seen.getOrPut(feedId) { LinkedHashSet() }.add(guid)
        trimSeenLocked(feedId)
        persistLocked()
    }

    /**
     * Manual download (UI row / agent tool). True = handed to an engine;
     * [done] then says whether it really got in — at once for a plain file,
     * and for a torrent once the engine has it. Marked seen only then.
     */
    fun downloadItem(feedId: String, guid: String, done: (Boolean) -> Unit = {}): Boolean {
        val item = _itemsByFeed.value[feedId]?.find { it.guid == guid } ?: return false
        return handToEngine(item) { ok ->
            if (ok) markSeen(feedId, guid)
            done(ok)
        }.also { accepted ->
            // Refused on the spot — its link leads somewhere the app will
            // not fetch from, or it is no magnet at all. The button used to
            // do nothing at all (emulator, 2026-11-24).
            if (!accepted) torrents.postNotice(Strings.t.noticeRssItemRefused)
        }
    }

    /**
     * Route one item to the right engine. True = accepted (the outcome
     * follows through [done]); false = refused on the spot, which must NOT
     * mark the item seen.
     */
    private fun handToEngine(item: RssClient.RssItem, done: (Boolean) -> Unit): Boolean {
        val target = item.magnet ?: item.link
        return when {
            target.startsWith("magnet:", true) -> {
                // Pre-validate with the same parser addMagnet uses — an
                // immediate rejection must NOT mark the item seen.
                if (MagnetParser.infoHashHex(target) == null) {
                    false
                } else {
                    torrents.addMagnet(target, onDone = done)
                    true
                }
            }
            target.substringBefore('?').endsWith(".torrent", true) -> {
                val staged = stageTorrent(target) ?: return false
                torrents.addTorrentFile(staged, onDone = done) // consumes/deletes the file
                true
            }
            target.startsWith("http://", true) || target.startsWith("https://", true) -> {
                // The link is the FEED's, not the user's — and auto-download
                // follows it without anyone watching. Every address it leads
                // to must be public, like a download the agent added.
                val ok = http.add(target, publicOnly = true) != null
                done(ok)
                ok
            }
            else -> false
        }
    }

    /**
     * The browser's torrent fetch, with the download engine's own guarded
     * fetch: capped, bencode-sanity-checked, and public-internet only on
     * every redirect hop (a .torrent link used to be fetched with plain
     * HttpURLConnection, redirects followed unchecked). Staged under a name
     * of its own: "rss-<ms>" let two items fetched in the same millisecond
     * share one file.
     */
    private fun stageTorrent(url: String): File? = runCatching {
        val bytes = http.fetchPublic(url, MAX_TORRENT_BYTES + 1) ?: return@runCatching null
        if (bytes.isEmpty() || bytes.size > MAX_TORRENT_BYTES) return@runCatching null
        if (bytes[0] != 'd'.code.toByte()) return@runCatching null
        val tmp = File(context.cacheDir, "rss-${java.util.UUID.randomUUID()}.torrent")
        tmp.writeBytes(bytes)
        tmp
    }.getOrNull()

    private fun matches(title: String, keywords: List<String>): Boolean {
        val t = title.lowercase()
        return keywords.any { t.contains(it) }
    }

    private fun trimSeenLocked(id: String) {
        val s = seen[id] ?: return
        if (s.size > SEEN_CAP) {
            val keep = s.toList().takeLast(SEEN_CAP)
            seen[id] = LinkedHashSet(keep)
        }
    }

    private fun persistLocked() {
        runCatching {
            val arr = JSONArray()
            _feeds.value.forEach { f ->
                arr.put(
                    JSONObject()
                        .put("id", f.id).put("url", f.url).put("title", f.title)
                        .put("auto", f.autoDownload)
                        .put("keywords", JSONArray(f.keywords))
                        .put("at", f.addedAt).put("fetched", f.lastFetchAt).put("err", f.lastError)
                        .put("seen", JSONArray(seen[f.id].orEmpty().toList()))
                        .put("po", f.publicOnly)
                )
            }
            val tmp = File(file.parentFile, file.name + ".tmp")
            tmp.writeText(arr.toString())
            if (!tmp.renameTo(file)) {
                file.delete()
                tmp.renameTo(file)
            }
        }.onFailure { Log.w(TAG, "persist failed: ${it.message}") }
    }

    companion object {
        private const val TAG = "RssRepository"
        private const val POLL_INTERVAL_MS = 15 * 60 * 1000L
        private const val SEEN_CAP = 500
        private const val MAX_TORRENT_BYTES = 32 * 1024 * 1024

        fun idFor(url: String): String {
            val d = MessageDigest.getInstance("SHA-1").digest(url.trim().toByteArray())
            return d.joinToString("") { "%02x".format(it) }.take(8)
        }
    }
}
