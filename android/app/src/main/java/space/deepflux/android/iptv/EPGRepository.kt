package space.deepflux.android.iptv

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.io.File
import java.io.FileReader
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicLong

/**
 * EPG repository — downloads the XMLTV guide ONCE into a disk cache, parses
 * that file, and exposes per-channel programme data.
 *
 * Boot (fix 2026-11-23): the cache used to be written but never read —
 * `restoreCache()` had no caller and the guide URL was only applied on
 * save/pick — so every restart lost the guide until the playlist was
 * re-selected. [ensure] now drives both paths, and a cache younger than
 * [MAX_AGE_MS] is served without touching the network. The guide also used
 * to be downloaded twice per refresh (once to parse, once to cache).
 *
 * The cache belongs to ONE guide (review finding 2026-11-24): its file name
 * is derived from the guide URL. There used to be a single `epg_cache.xml`
 * for whichever guide had been downloaded last, so switching to a playlist
 * with another guide showed the PREVIOUS playlist's listings — for as long
 * as that cache was fresh (6 h), without ever asking for the new guide.
 * Now the guide in memory is dropped the moment the URL changes, a fetch
 * that was started for an earlier URL is closed and can no longer install
 * or cache anything, and a failed fetch only ever falls back to a cache of
 * the SAME guide.
 */
class EPGRepository(
    private val context: Context,
    /** Test seam: the clock behind "how fresh is the cache" and the retry back-off. */
    private val clock: () -> Long = System::currentTimeMillis,
) {

    private val _state = MutableStateFlow(EPGParser.EPGState())
    val state: StateFlow<EPGParser.EPGState> = _state.asStateFlow()

    private val _epgUrl = MutableStateFlow("")
    val epgUrl: StateFlow<String> = _epgUrl.asStateFlow()

    /** True while a network fetch for the CURRENT guide is running. */
    private val _loading = MutableStateFlow(false)
    val loading: StateFlow<Boolean> = _loading.asStateFlow()

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /**
     * One fetch that is in flight. Only the CURRENT one may install or
     * cache a guide; a superseded one is closed and, should it still finish,
     * thrown away. Two fetches run at the same time only in that moment of
     * hand-over — a slow guide must never delay the one switched to.
     */
    private class Load(val wanted: Long, val url: String) {
        @Volatile
        var connection: HttpURLConnection? = null
    }

    @Volatile
    private var currentLoad: Load? = null

    /**
     * Bumped whenever the wanted guide CHANGES (another URL, a refresh,
     * none). A load carries the value it started under.
     */
    private val generation = AtomicLong(0)

    /**
     * Makes "still the wanted load? → install" one step. A load parses on an
     * IO thread while [ensure] switches guides on the main one: checked and
     * set separately, the previous guide could land in state just after the
     * switch had cleared it.
     */
    private val installLock = Any()

    /** The guide URL [state] holds ("" = nothing loaded). Guarded by [installLock]. */
    @Volatile
    private var loadedUrl = ""

    /** After a failed fetch, auto-refresh of THAT guide backs off for [RETRY_BACKOFF_MS]. */
    @Volatile
    private var failedUrl = ""
    @Volatile
    private var lastFailureAt = 0L

    /**
     * Point the guide at [url] ("" = no guide). Safe to call on every
     * recomposition: the same URL with its guide loaded is a no-op, and the
     * same URL with a fetch on its way adds nothing. Changing the URL drops
     * the previous guide at once, so a previous playlist's listings never
     * leak into the new one.
     */
    fun ensure(url: String) {
        val changed = _epgUrl.value != url
        if (!changed && (url.isBlank() || loadedUrl == url)) return
        _epgUrl.value = url
        val wanted = if (changed) generation.incrementAndGet() else generation.get()
        val previous = if (changed) {
            synchronized(installLock) {
                val old = currentLoad
                currentLoad = null
                loadedUrl = ""
                _state.value = EPGParser.EPGState()
                old
            }
        } else {
            currentLoad
        }
        if (changed) previous?.connection?.disconnect()
        if (url.isBlank()) {
            _loading.value = false
            return
        }
        if (!changed && previous != null) return
        val load = Load(wanted, url)
        synchronized(installLock) {
            if (generation.get() != wanted) return
            currentLoad = load
        }
        scope.launch { run(load, force = false) }
    }

    /** Kept name from before the fix — same as [ensure]. */
    fun setEpgUrl(url: String) = ensure(url)

    /** Manual refresh: always hits the network for the current URL. */
    fun refresh() {
        val url = _epgUrl.value
        if (url.isBlank()) return
        val load = Load(generation.incrementAndGet(), url)
        val previous = synchronized(installLock) { currentLoad.also { currentLoad = load } }
        previous?.connection?.disconnect()
        scope.launch { run(load, force = true) }
    }

    private fun isCurrent(load: Load): Boolean = generation.get() == load.wanted

    private fun install(load: Load, guide: EPGParser.EPGState): Boolean = synchronized(installLock) {
        if (!isCurrent(load)) return@synchronized false
        if (guide.channelCount > 0) {
            _state.value = guide
            loadedUrl = load.url
        }
        true
    }

    private fun run(load: Load, force: Boolean) {
        // Its own partial file. Two loads of the SAME guide — switch away and
        // back — used to share one tmp: the second truncated the file the
        // first was still parsing, and the first's cleanup deleted it.
        val cache = cacheFileFor(load.url)
        val tmp = File(cache.parentFile, cache.name + "." + java.util.UUID.randomUUID().toString().take(8) + ".tmp")
        try {
            if (!isCurrent(load)) return
            if (!force) {
                if (serveFromCache(load, MAX_AGE_MS)) return
                if (failedUrl == load.url && clock() - lastFailureAt < RETRY_BACKOFF_MS) return
            }
            // Parsing a large cache takes a while — the guide may have moved on.
            if (!isCurrent(load)) return
            _loading.value = true
            download(load, tmp)
            // Superseded while downloading: not a byte of it is parsed.
            if (!isCurrent(load)) return
            val parsed = FileReader(tmp).use { EPGParser.parse(EPGParser.newParser(it)) }
            if (parsed.channelCount == 0) throw IOException("guide has no channels")
            // Never cache a guide that is no longer the selected one.
            if (!isCurrent(load)) return
            if (!tmp.renameTo(cache)) {
                cache.delete()
                if (!tmp.renameTo(cache)) throw IOException("could not replace the guide cache")
            }
            trimCaches(keep = cache)
            if (!install(load, parsed)) return
            Log.i(TAG, "EPG refreshed: ${parsed.channelCount} channels")
        } catch (e: Exception) {
            fetchFailed(load, e.message, tryCache = true)
        } catch (e: OutOfMemoryError) {
            // Not an Exception: this used to kill the app. The parser bounds
            // what it keeps, but a device that is short of memory anyway
            // must not die over a guide — and no cache is tried, since
            // parsing that could run into the same wall.
            fetchFailed(load, "out of memory", tryCache = false)
        } finally {
            tmp.delete()
            settle(load)
        }
    }

    /** A current load's failure counts for its guide; a superseded one's does not. */
    private fun fetchFailed(load: Load, reason: String?, tryCache: Boolean) {
        if (!isCurrent(load)) return
        failedUrl = load.url
        lastFailureAt = clock()
        Log.w(TAG, "EPG fetch failed: $reason")
        // A stale cache of THIS guide beats no guide at all — never another guide's.
        if (tryCache) serveFromCache(load, maxAgeMs = Long.MAX_VALUE)
    }

    /**
     * EVERY way out of [run] ends here, so the guide can never be left with
     * a load that is "in flight" forever. The exits for a served cache and
     * for the retry back-off used to skip it: a second `ensure()` inside the
     * 5-minute window then found that finished load, concluded one was on
     * its way, and the guide was never loaded again in that process (second
     * review, 2026-11-24).
     */
    private fun settle(load: Load) {
        synchronized(installLock) {
            if (isCurrent(load)) {
                currentLoad = null
                _loading.value = false
            }
        }
    }

    /** Installs [load]'s cached guide when there is one no older than [maxAgeMs]. */
    private fun serveFromCache(load: Load, maxAgeMs: Long): Boolean {
        synchronized(installLock) { if (loadedUrl == load.url) return true }
        val cache = cacheFileFor(load.url)
        if (!cache.isFile) return false
        val age = clock() - cache.lastModified()
        if (age < 0 || age > maxAgeMs) return false
        val cached = runCatching { FileReader(cache).use { EPGParser.parse(EPGParser.newParser(it)) } }
            .getOrElse {
                Log.w(TAG, "EPG cache unreadable: ${it.message}")
                return false
            }
        // An empty or unusable cache is no guide — fall through to the network.
        if (cached.channelCount == 0) return false
        return install(load, cached)
    }

    /** Stream the guide to [dest] with a hard size cap (never buffered in memory). */
    private fun download(load: Load, dest: File) {
        val conn = URL(load.url).openConnection() as HttpURLConnection
        load.connection = conn
        try {
            conn.connectTimeout = 15_000
            conn.readTimeout = 30_000
            conn.setRequestProperty("User-Agent", USER_AGENT)
            if (conn.responseCode != 200) throw IOException("EPG HTTP ${conn.responseCode}")
            // Superseded while connecting: the close above may have come first.
            if (!isCurrent(load)) throw IOException("superseded")
            conn.inputStream.use { input ->
                dest.outputStream().use { out ->
                    val buf = ByteArray(64 * 1024)
                    var total = 0L
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        total += n
                        if (total > MAX_GUIDE_BYTES) throw IOException("guide larger than ${MAX_GUIDE_BYTES / 1048576} MB")
                        out.write(buf, 0, n)
                    }
                }
            }
        } finally {
            load.connection = null
            conn.disconnect()
        }
    }

    /**
     * Guides reach 100+ MB, so only the most recently downloaded few stay
     * cached (switching between a handful of playlists then costs no
     * re-download). The rest goes, along with the single-name cache of older
     * builds — it belonged to whichever guide had been downloaded last.
     */
    private fun trimCaches(keep: File) {
        val dir = keep.parentFile ?: return
        val caches = dir.listFiles { f -> f.name.startsWith(CACHE_PREFIX) && f.name.endsWith(".xml") }
            ?.sortedByDescending { it.lastModified() }.orEmpty()
        caches.filter { it != keep }.drop(KEEP_CACHES - 1).forEach { it.delete() }
        File(dir, "epg_cache.xml").delete()
        // Partial downloads of runs that died mid-guide. An hour old, so a
        // load in flight elsewhere is never touched.
        dir.listFiles { f -> f.name.startsWith(CACHE_PREFIX) && f.name.endsWith(".tmp") }
            ?.filter { clock() - it.lastModified() > STALE_PART_MS }
            ?.forEach { it.delete() }
    }

    private fun cacheFileFor(url: String): File = File(context.filesDir, cacheNameFor(url))

    /** The cache file's name for a guide URL (unit tests write a cache through it). */
    internal fun cacheNameFor(url: String): String = CACHE_PREFIX + cacheKey(url) + ".xml"

    private fun cacheKey(url: String): String =
        MessageDigest.getInstance("SHA-256").digest(url.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }.take(24)

    companion object {
        private const val TAG = "EPGRepository"

        /** A cached guide this young is served without a network round trip. */
        private const val MAX_AGE_MS = 6 * 60 * 60 * 1000L

        /** After a failed fetch, auto-refresh of THAT guide backs off for this long. Internal: a test moves its clock past it. */
        internal const val RETRY_BACKOFF_MS = 5 * 60 * 1000L

        /** A partial download this old belongs to a run that died; it goes at the next successful install. */
        private const val STALE_PART_MS = 60 * 60 * 1000L

        /** Provider guides reach 100+ MB; past this something is wrong. */
        private const val MAX_GUIDE_BYTES = 256L * 1024 * 1024

        private const val KEEP_CACHES = 3

        private const val CACHE_PREFIX = "epg_"

        private const val USER_AGENT = "Mozilla/5.0 (Linux; Android 14) DeepFlux/1.4"
    }
}
