package space.deepflux.android.browser

import android.content.Context
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Browser bookmarks + history (plan §13 #3) — the Android twin of the
 * desktop's bookmark popup / history page, executed the Android way: a
 * FLAT bookmark list (no folder tree — Chrome-on-Android reality; the
 * desktop's nested folders have no equivalent demand here) and a capped
 * history, both JSON sidecars with atomic writes like every other store
 * in this app. Importing Chrome's profile is impossible on Android —
 * manual bookmarks only.
 */
class BrowserStore(context: Context) {

    data class Bookmark(val url: String, val title: String, val addedAt: Long)
    data class HistoryEntry(val url: String, val title: String, val at: Long)

    private val file: File = File(context.filesDir, "browser_bookmarks.json")
    private val historyFile: File = File(context.filesDir, "browser_history.json")
    private val lock = Any()

    private val _bookmarks = MutableStateFlow<List<Bookmark>>(emptyList())
    val bookmarks: StateFlow<List<Bookmark>> = _bookmarks.asStateFlow()

    private val _history = MutableStateFlow<List<HistoryEntry>>(emptyList())
    val history: StateFlow<List<HistoryEntry>> = _history.asStateFlow()

    init {
        runCatching {
            if (file.exists()) {
                val arr = JSONArray(file.readText())
                val out = mutableListOf<Bookmark>()
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    val url = o.optString("url")
                    if (url.startsWith("http")) out.add(Bookmark(url, o.optString("title"), o.optLong("at")))
                }
                _bookmarks.value = out
            }
        }.onFailure { Log.w(TAG, "bookmarks load failed: ${it.message}") }
        runCatching {
            if (historyFile.exists()) {
                val arr = JSONArray(historyFile.readText())
                val out = mutableListOf<HistoryEntry>()
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    val url = o.optString("url")
                    if (url.startsWith("http")) out.add(HistoryEntry(url, o.optString("title"), o.optLong("at")))
                }
                _history.value = out
            }
        }.onFailure { Log.w(TAG, "history load failed: ${it.message}") }
    }

    fun isBookmarked(url: String): Boolean = _bookmarks.value.any { it.url == url }

    /** Add or remove the bookmark for `url`; returns the new state. */
    fun toggleBookmark(url: String, title: String): Boolean {
        if (url.isBlank() || !url.startsWith("http")) return false
        return synchronized(lock) {
            val existing = _bookmarks.value.filter { it.url != url }
            if (existing.size != _bookmarks.value.size) {
                _bookmarks.value = existing
                persistLocked()
                false
            } else {
                _bookmarks.value = existing + Bookmark(url, title.ifBlank { url }, System.currentTimeMillis())
                persistLocked()
                true
            }
        }
    }

    fun removeBookmark(url: String) {
        synchronized(lock) {
            val next = _bookmarks.value.filter { it.url != url }
            if (next.size != _bookmarks.value.size) {
                _bookmarks.value = next
                persistLocked()
            }
        }
    }

    /**
     * History record at page-finish time. Consecutive reloads/redirects of
     * the same URL collapse into the newest entry (dedup against the TOP
     * only — revisiting a page later keeps both visits, newest first).
     */
    fun recordVisit(url: String, title: String) {
        if (!url.startsWith("http")) return
        synchronized(lock) {
            val top = _history.value.firstOrNull()
            if (top?.url == url) {
                if (title.isNotBlank() && title != top.title) {
                    _history.value = listOf(top.copy(title = title)) + _history.value.drop(1)
                    persistHistoryLocked()
                }
                return
            }
            _history.value = listOf(HistoryEntry(url, title.ifBlank { url }, System.currentTimeMillis())) +
                _history.value.take(HISTORY_CAP - 1)
            persistHistoryLocked()
        }
    }

    /** The page title often arrives AFTER onPageFinished — patch the top entry. */
    fun updateTitleIfLatest(url: String, title: String) {
        if (title.isBlank()) return
        synchronized(lock) {
            val top = _history.value.firstOrNull() ?: return
            if (top.url != url || top.title == title) return
            _history.value = listOf(top.copy(title = title)) + _history.value.drop(1)
            persistHistoryLocked()
        }
    }

    fun removeHistoryAt(index: Int) {
        synchronized(lock) {
            if (index in _history.value.indices) {
                _history.value = _history.value.filterIndexed { i, _ -> i != index }
                persistHistoryLocked()
            }
        }
    }

    fun clearHistory() {
        synchronized(lock) {
            _history.value = emptyList()
            persistHistoryLocked()
        }
    }

    private fun persistLocked() {
        writeAtomic(file) { JSONArray().apply { _bookmarks.value.forEach { put(JSONObject().put("url", it.url).put("title", it.title).put("at", it.addedAt)) } }.toString() }
    }

    private fun persistHistoryLocked() {
        writeAtomic(historyFile) { JSONArray().apply { _history.value.forEach { put(JSONObject().put("url", it.url).put("title", it.title).put("at", it.at)) } }.toString() }
    }

    private fun writeAtomic(target: File, body: () -> String) {
        runCatching {
            val tmp = File(target.parentFile, target.name + ".tmp")
            tmp.writeText(body())
            if (!tmp.renameTo(target)) {
                target.delete()
                tmp.renameTo(target)
            }
        }.onFailure { Log.w(TAG, "persist failed: ${it.message}") }
    }

    companion object {
        private const val TAG = "BrowserStore"
        private const val HISTORY_CAP = 500
    }
}
