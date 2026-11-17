package space.deepflux.android.iptv

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Favorite Live TV / VOD entries (owner request 2026-11-15): starred
 * channels persist across restarts as a JSON sidecar (same atomic-write
 * ritual as every store) and surface as the built-in "⭐ Favorites"
 * source in the playlist dropdown. Keyed by stream URL.
 */
class FavoritesStore(context: Context) {

    data class Fav(
        val url: String,
        val name: String,
        val groupId: String = "",
        val logoUrl: String = "",
        val httpUserAgent: String = "",
        val addedAt: Long = System.currentTimeMillis(),
    ) {
        fun toChannel() = M3UParser.Channel(
            name = name,
            url = url,
            logoUrl = logoUrl,
            groupId = groupId,
            httpUserAgent = httpUserAgent,
        )
    }

    private val file: File = File(context.filesDir, "iptv_favorites.json")
    private val lock = Any()

    private val _items = MutableStateFlow<List<Fav>>(emptyList())
    val items: StateFlow<List<Fav>> = _items.asStateFlow()

    init {
        runCatching {
            if (file.exists()) {
                val arr = JSONArray(file.readText())
                val out = ArrayList<Fav>(arr.length())
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    val url = o.optString("url")
                    if (url.isNotBlank()) {
                        out.add(
                            Fav(
                                url = url,
                                name = o.optString("name").ifBlank { url },
                                groupId = o.optString("group"),
                                logoUrl = o.optString("logo"),
                                httpUserAgent = o.optString("ua"),
                                addedAt = o.optLong("at"),
                            ),
                        )
                    }
                }
                _items.value = out
            }
        }
    }

    fun isFavorite(url: String): Boolean = _items.value.any { it.url == url }

    /** Toggle; returns the new favorite state. */
    fun toggle(ch: M3UParser.Channel): Boolean = synchronized(lock) {
        val existing = _items.value.filter { it.url != ch.url }
        val nowFav = existing.size != _items.value.size
        _items.value = if (nowFav) {
            existing
        } else {
            listOf(Fav(ch.url, ch.name, ch.groupId, ch.logoUrl, ch.httpUserAgent)) + existing
        }
        persistLocked()
        !nowFav
    }

    private fun persistLocked() {
        runCatching {
            val arr = JSONArray()
            _items.value.forEach {
                arr.put(
                    JSONObject()
                        .put("url", it.url)
                        .put("name", it.name)
                        .put("group", it.groupId)
                        .put("logo", it.logoUrl)
                        .put("ua", it.httpUserAgent)
                        .put("at", it.addedAt),
                )
            }
            val tmp = File(file.parentFile, file.name + ".tmp")
            tmp.writeText(arr.toString())
            if (!tmp.renameTo(file)) {
                file.delete()
                tmp.renameTo(file)
            }
        }
    }
}
