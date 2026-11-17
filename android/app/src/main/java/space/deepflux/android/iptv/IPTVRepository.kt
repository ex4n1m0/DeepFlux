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
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * IPTV playlist repository (v1.3 multi-playlist rework) — the Android twin
 * of iptv/manager.py's source list: SAVED playlists ([PlaylistStore]),
 * per-playlist disk caches, one active playlist at a time. Switching,
 * editing or deleting a playlist never touches the others; a failed load
 * keeps the saved entry (and the last good channel list) and surfaces the
 * error instead.
 */
class IPTVRepository(
    private val context: Context,
    /** The app's downloads dir — the built-in local video source (1.3.5). */
    private val downloadsDir: File = File(context.filesDir, "downloads"),
) {

    data class IPTVState(
        val playlist: PlaylistStore.Playlist? = null,
        val channels: List<M3UParser.Channel> = emptyList(),
        val loading: Boolean = false,
        val error: String = "",
    )

    private val _state = MutableStateFlow(IPTVState())
    val state: StateFlow<IPTVState> = _state.asStateFlow()

    val store = PlaylistStore(context)
    val playlists: StateFlow<List<PlaylistStore.Playlist>> = store.playlists
    val activeId: StateFlow<String> = store.activeId

    /** Starred entries (owner 2026-11-15) — the ⭐ source. */
    val favorites = FavoritesStore(context)

    /**
     * Built-in pseudo source (never persisted): every video under the app's
     * downloads dir, listed like a playlist — downloaded videos appear in
     * the player tab (owner request 2026-11-15).
     */
    val downloadsSource = PlaylistStore.Playlist(
        id = LocalSources.DOWNLOADS_ID,
        name = "Downloads",
        kind = "local",
        url = "",
    )

    /** Built-in favorites pseudo source (not persisted). */
    val favoritesSource = PlaylistStore.Playlist(
        id = FAVORITES_ID,
        name = "Favorites",
        kind = "local",
        url = "",
    )

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    init {
        // Boot: point at the persisted active playlist, restore its cache
        // instantly, then refresh in the background.
        val boot = store.active
        if (boot != null) {
            _state.value = _state.value.copy(playlist = boot)
            scope.launch {
                restoreCache(boot)
                load(boot)
            }
        }
        // Another surface (manage dialog) changed the active pointer.
        scope.launch {
            store.activeId.collect { id ->
                if (id.isNotBlank() && id != _state.value.playlist?.id) {
                    val p = store.playlists.value.find { it.id == id } ?: return@collect
                    _state.value = IPTVState(playlist = p, loading = true)
                    restoreCache(p)
                    load(p)
                }
            }
        }
    }

    fun select(id: String) {
        when (id) {
            LocalSources.DOWNLOADS_ID -> {
                _state.value = IPTVState(playlist = downloadsSource, loading = true)
                scope.launch { load(downloadsSource) }
            }
            FAVORITES_ID -> {
                _state.value = IPTVState(playlist = favoritesSource, loading = true)
                scope.launch { load(favoritesSource) }
            }
            else -> store.setActive(id)
        }
    }

    /** Add (or replace, when the id exists) + activate + load. */
    fun save(playlist: PlaylistStore.Playlist) {
        val isNew = store.playlists.value.none { it.id == playlist.id }
        val p = if (isNew) store.add(playlist) else playlist.also { store.update(it) }
        if (!isNew) {
            // Edited in place: re-run the collector by resetting state.
            _state.value = IPTVState(playlist = p, loading = true)
            scope.launch {
                load(p)
            }
        }
    }

    fun remove(id: String) {
        val wasActive = store.activeId.value == id
        store.remove(id)
        cacheFile(id, "m3u").delete()
        cacheFile(id, "json").delete()
        if (wasActive) {
            val next = store.active
            _state.value = if (next == null) IPTVState() else IPTVState(playlist = next, loading = true)
            next?.let {
                scope.launch {
                    restoreCache(it)
                    load(it)
                }
            }
        }
    }

    fun refresh() {
        val p = _state.value.playlist ?: return
        scope.launch { load(p) }
    }

    /** Xtream login path (blocking, on the caller's thread). */
    private fun loadXtream(p: PlaylistStore.Playlist): List<M3UParser.Channel> {
        val client = XtreamClient(p.url, p.username, p.password)
        val auth = client.authenticate()
        if (auth.auth != 1) throw IllegalStateException("Xtream login failed: ${auth.status}")
        return client.toM3UChannels(client.getLiveChannels())
    }

    private fun load(p: PlaylistStore.Playlist) {
        _state.value = _state.value.copy(playlist = p, loading = true, error = "")
        try {
            val channels = when {
                p.kind == "xtream" -> loadXtream(p)
                p.kind == "local" -> loadLocal(p)
                else -> fetchM3uStreamed(p)
            }
            if (p.kind == "xtream") writeXtreamCache(p, channels)
            _state.value = _state.value.copy(playlist = p, channels = channels, loading = false)
        } catch (e: OutOfMemoryError) {
            // A pathological playlist must never kill the app (owner crash
            // 2026-11-15: 131k channels OOM'd a whole-string read).
            Log.e(TAG, "playlist OOM: ${p.url.take(60)}")
            _state.value = _state.value.copy(
                playlist = p,
                loading = false,
                error = "playlist too large for this device",
            )
        } catch (e: Exception) {
            Log.w(TAG, "playlist load failed: ${e.message}")
            // KEEP the saved playlist and its cached channels — only flag it.
            _state.value = _state.value.copy(
                playlist = p,
                loading = false,
                error = e.message?.take(150) ?: "network error",
            )
        }
    }

    /**
     * M3U lane, OOM-proof (1.3.6): stream the playlist straight to the
     * cache file (capped), then STREAM-PARSE from the file. The old
     * readText() materialized a 2× copy of a 36 MB list — a single 73 MB
     * allocation against a ~200 MB heap limit → OutOfMemoryError → crash.
     */
    private fun fetchM3uStreamed(p: PlaylistStore.Playlist): List<M3UParser.Channel> {
        val cache = cacheFile(p.id, "m3u")
        val tmp = File(cache.path + ".tmp")
        tmp.parentFile?.mkdirs()
        val req = okhttp3.Request.Builder()
            .url(p.url)
            .header("User-Agent", "Mozilla/5.0 (Linux; Android 14) DeepFlux/1.3")
            .build()
        http.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw IllegalStateException("HTTP ${resp.code}")
            val body = resp.body ?: throw IllegalStateException("empty response")
            java.io.FileOutputStream(tmp).use { sink ->
                val input = body.byteStream()
                val buf = ByteArray(64 * 1024)
                var total = 0L
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    total += n
                    if (total > MAX_PLAYLIST_BYTES) throw IllegalStateException("playlist larger than ${MAX_PLAYLIST_BYTES / 1048576} MB")
                    sink.write(buf, 0, n)
                }
            }
        }
        if (!tmp.renameTo(cache)) {
            cache.delete()
            tmp.renameTo(cache)
        }
        return cache.bufferedReader(Charsets.UTF_8).use { M3UParser.parseStream(it) }
    }

    private val http = okhttp3.OkHttpClient.Builder()
        .connectTimeout(15, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
        .build()

    /** Local folder sources: the downloads dir (File walk) or a SAF tree. */
    private fun loadLocal(p: PlaylistStore.Playlist): List<M3UParser.Channel> {
        if (p.id == FAVORITES_ID) return favorites.items.value.map { it.toChannel() }
        if (p.id == LocalSources.DOWNLOADS_ID || p.url.isBlank()) {
            return LocalSources.scanDownloads(downloadsDir)
        }
        return LocalSources.scanSafTree(context, android.net.Uri.parse(p.url))
    }

    // ------------------------------------------------------------------ caches

    private fun cacheFile(id: String, ext: String) = File(context.filesDir, "iptv_cache_${id.replace(Regex("[^a-zA-Z0-9_-]"), "")}.$ext")

    private fun writeXtreamCache(p: PlaylistStore.Playlist, channels: List<M3UParser.Channel>) {
        withContextSafe {
            val arr = JSONArray()
            channels.take(20_000).forEach { c ->
                arr.put(
                    JSONObject()
                        .put("n", c.name).put("u", c.url).put("l", c.logoUrl)
                        .put("g", c.groupId).put("ua", c.httpUserAgent),
                )
            }
            cacheFile(p.id, "json").writeText(arr.toString())
        }
    }

    private suspend fun restoreCache(p: PlaylistStore.Playlist) {
        withContext(Dispatchers.IO) {
            runCatching {
                val channels = when {
                    p.kind == "m3u" ->
                        cacheFile(p.id, "m3u").takeIf { it.exists() }
                            ?.bufferedReader(Charsets.UTF_8)?.use { M3UParser.parseStream(it) }
                    else -> cacheFile(p.id, "json").takeIf { it.exists() }?.readText()?.let { txt ->
                        val arr = JSONArray(txt)
                        (0 until arr.length()).map { i ->
                            val o = arr.getJSONObject(i)
                            M3UParser.Channel(
                                name = o.optString("n"),
                                url = o.optString("u"),
                                logoUrl = o.optString("l"),
                                groupId = o.optString("g"),
                                httpUserAgent = o.optString("ua"),
                            )
                        }
                    }
                }.orEmpty()
                if (channels.isNotEmpty()) {
                    _state.value = _state.value.copy(channels = channels)
                    Log.i(TAG, "restored ${channels.size} channels from cache")
                }
            }
        }
    }

    private fun withContextSafe(block: () -> Unit) {
        // Cache writes are best-effort; the network load is the source of truth.
        runCatching { scope.launch { withContext(Dispatchers.IO) { block() } } }
    }

    companion object {
        private const val TAG = "IPTVRepository"

        /** Built-in favorites source id. */
        const val FAVORITES_ID = "__favorites__"

        /** Largest cached playlist (measured: a 131k-channel sub ≈ 36 MB). */
        private const val MAX_PLAYLIST_BYTES = 96L * 1024 * 1024
    }
}
