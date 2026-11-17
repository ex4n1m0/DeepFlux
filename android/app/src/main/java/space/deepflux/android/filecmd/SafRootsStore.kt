package space.deepflux.android.filecmd

import android.content.Context
import android.net.Uri
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * User-chosen SAF folders (v1.3) — the "broadest file access legitimately
 * available" lane: any folder the user picks via ACTION_OPEN_DOCUMENT_TREE
 * (persisted permission, survives reboots) is browsable in Commander.
 * JSON sidecar, atomic writes — same pattern as every other store.
 */
class SafRootsStore(context: Context) {

    data class SafRoot(
        /** Tree uri string (content://…/tree/…) — the persisted grant. */
        val uriString: String,
        /** Display name (usually the folder's own name at pick time). */
        val name: String,
        val addedAt: Long,
    ) {
        val uri: Uri get() = Uri.parse(uriString)
    }

    private val file: File = File(context.filesDir, "commander_saf_roots.json")
    private val lock = Any()

    private val _roots = MutableStateFlow<List<SafRoot>>(emptyList())
    val roots: StateFlow<List<SafRoot>> = _roots.asStateFlow()

    init {
        runCatching {
            if (file.exists()) {
                val arr = JSONArray(file.readText())
                val out = mutableListOf<SafRoot>()
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    val u = o.optString("uri")
                    if (u.startsWith("content://")) out.add(SafRoot(u, o.optString("name"), o.optLong("at")))
                }
                _roots.value = out
            }
        }.onFailure { Log.w(TAG, "load failed: ${it.message}") }
    }

    fun add(uriString: String, name: String) {
        synchronized(lock) {
            if (_roots.value.any { it.uriString == uriString }) return
            _roots.value = _roots.value + SafRoot(uriString, name, System.currentTimeMillis())
            persistLocked()
        }
    }

    fun remove(uriString: String) {
        synchronized(lock) {
            _roots.value = _roots.value.filter { it.uriString != uriString }
            persistLocked()
        }
    }

    private fun persistLocked() {
        runCatching {
            val arr = JSONArray()
            _roots.value.forEach { arr.put(JSONObject().put("uri", it.uriString).put("name", it.name).put("at", it.addedAt)) }
            val tmp = File(file.parentFile, file.name + ".tmp")
            tmp.writeText(arr.toString())
            if (!tmp.renameTo(file)) {
                file.delete()
                tmp.renameTo(file)
            }
        }.onFailure { Log.w(TAG, "persist failed: ${it.message}") }
    }

    companion object {
        private const val TAG = "SafRootsStore"
    }
}
