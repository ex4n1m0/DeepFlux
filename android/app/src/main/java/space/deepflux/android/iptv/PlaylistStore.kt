package space.deepflux.android.iptv

import android.content.Context
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/**
 * Persistent IPTV playlists (v1.3) — the desktop's IPTVSourceConfig list
 * (config.py `iptv.sources`) as an Android JSON sidecar: multiple saved
 * playlists (M3U URL or Xtream Codes login), an active-playlist pointer,
 * atomic writes like every other store in this app. Entries SURVIVE app
 * restarts and load failures (deleting never happens implicitly).
 */
class PlaylistStore(context: Context) {

    data class Playlist(
        val id: String,
        val name: String,
        /** [KIND_M3U] (playlist URL), [KIND_XTREAM] (server + user + password) or [KIND_LOCAL] (a SAF folder tree). */
        val kind: String,
        /** M3U: the playlist URL. Xtream: the server URL. Local: the SAF tree `content://` uri. */
        val url: String,
        val username: String = "",
        val password: String = "",
        val epgUrl: String = "",
        /**
         * What the user says this source carries: [CONTENT_AUTO] (the
         * heuristics decide per entry), [CONTENT_LIVE] (every entry is a
         * channel) or [CONTENT_VOD] (every entry is a video). The override
         * covers what no keyword rule can — a provider whose group names
         * are in Chinese, say (owner 2026-11-22, desktop parity).
         */
        val contentType: String = CONTENT_AUTO,
        val addedAt: Long = System.currentTimeMillis(),
    )

    private val file: File = File(context.filesDir, "iptv_playlists.json")
    private val lock = Any()

    private val _playlists = MutableStateFlow<List<Playlist>>(emptyList())
    val playlists: StateFlow<List<Playlist>> = _playlists.asStateFlow()

    private val _activeId = MutableStateFlow("")
    val activeId: StateFlow<String> = _activeId.asStateFlow()

    init {
        runCatching {
            if (file.exists()) {
                val o = JSONObject(file.readText())
                val arr = o.optJSONArray("playlists") ?: JSONArray()
                val out = mutableListOf<Playlist>()
                for (i in 0 until arr.length()) {
                    val p = arr.optJSONObject(i) ?: continue
                    out.add(fromJson(p))
                }
                _playlists.value = out
                _activeId.value = o.optString("active", "")
            }
        }.onFailure { Log.w(TAG, "load failed: ${it.message}") }
    }

    val active: Playlist? get() = _playlists.value.find { it.id == _activeId.value }

    fun add(playlist: Playlist): Playlist {
        val p = if (playlist.id.isBlank()) playlist.copy(id = UUID.randomUUID().toString()) else playlist
        synchronized(lock) {
            _playlists.value = _playlists.value + p
            _activeId.value = p.id
            persistLocked()
        }
        return p
    }

    /** Replace by id; returns true when the id existed. */
    fun update(playlist: Playlist): Boolean = synchronized(lock) {
        val idx = _playlists.value.indexOfFirst { it.id == playlist.id }
        if (idx < 0) return false
        _playlists.value = _playlists.value.toMutableList().apply { set(idx, playlist) }
        persistLocked()
        true
    }

    fun remove(id: String) {
        synchronized(lock) {
            _playlists.value = _playlists.value.filter { it.id != id }
            if (_activeId.value == id) {
                _activeId.value = _playlists.value.firstOrNull()?.id ?: ""
            }
            persistLocked()
        }
    }

    fun setActive(id: String) {
        synchronized(lock) {
            if (_playlists.value.any { it.id == id } && _activeId.value != id) {
                _activeId.value = id
                persistLocked()
            }
        }
    }

    private fun persistLocked() {
        runCatching {
            val o = JSONObject()
                .put("playlists", JSONArray().apply { _playlists.value.forEach { put(toJson(it)) } })
                .put("active", _activeId.value)
            val tmp = File(file.parentFile, file.name + ".tmp")
            tmp.writeText(o.toString())
            if (!tmp.renameTo(file)) {
                file.delete()
                tmp.renameTo(file)
            }
        }.onFailure { Log.w(TAG, "persist failed: ${it.message}") }
    }

    private fun toJson(p: Playlist) = JSONObject()
        .put("id", p.id)
        .put("name", p.name)
        .put("kind", p.kind)
        .put("url", p.url)
        .put("username", p.username)
        .put("password", p.password)
        .put("epgUrl", p.epgUrl)
        .put("ct", p.contentType)
        .put("at", p.addedAt)

    private fun fromJson(o: JSONObject) = Playlist(
        id = o.optString("id"),
        name = o.optString("name").ifBlank { o.optString("url").take(40) },
        // EVERY known kind must survive the round trip: "local" folder
        // sources used to come back as "m3u" and their content:// url was
        // then fetched as a playlist URL — the source broke on every restart.
        kind = o.optString("kind").takeIf { it in KINDS } ?: KIND_M3U,
        url = o.optString("url"),
        username = o.optString("username"),
        password = o.optString("password"),
        epgUrl = o.optString("epgUrl"),
        // A value this build does not know is no override at all.
        contentType = o.optString("ct").takeIf { it in CONTENT_TYPES } ?: CONTENT_AUTO,
        addedAt = o.optLong("at"),
    )

    companion object {
        const val KIND_M3U = "m3u"
        const val KIND_XTREAM = "xtream"
        const val KIND_LOCAL = "local"
        private val KINDS = setOf(KIND_M3U, KIND_XTREAM, KIND_LOCAL)

        const val CONTENT_AUTO = "auto"
        const val CONTENT_LIVE = "live"
        const val CONTENT_VOD = "vod"
        private val CONTENT_TYPES = setOf(CONTENT_AUTO, CONTENT_LIVE, CONTENT_VOD)
        private const val TAG = "PlaylistStore"
    }
}
