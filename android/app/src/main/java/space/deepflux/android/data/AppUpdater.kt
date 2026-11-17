package space.deepflux.android.data

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Log
import androidx.core.content.FileProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import space.deepflux.android.BuildConfig
import java.io.File
import java.security.MessageDigest

/** What latest-android.json says (strict subset — see [parseFeed]). */
data class UpdateInfo(
    val version: String,
    val versionCode: Int,
    val fileName: String,
    val size: Long,
    val sha256: String,
    val released: String,
    val notes: String,
)

/** Download/verify pipeline state for the update dialog. */
sealed class UpdateProgress {
    data object Idle : UpdateProgress()
    data class Downloading(val read: Long, val total: Long) : UpdateProgress()
    data object Verifying : UpdateProgress()
    data class Ready(val file: File) : UpdateProgress()
    data class Error(val message: String) : UpdateProgress()
}

/**
 * In-app self-update (plan §13 #7, unblocked 2026-11-16 when the release
 * lane went live). Mirrors the desktop updater's contract:
 *
 *  * checks the website feed (`latest-android.json`), daily-gated at
 *    startup plus a manual Settings row,
 *  * downloads the APK with the site feed's size + sha256 pinned (an
 *    HTTPS-only URL hardcoded to deepflux.space — never user- or
 *    agent-settable, and deliberately NOT an agent tool: prompt injection
 *    must never be able to trigger an installer run),
 *  * hands the verified file to the system package installer — Android
 *    never lets a sideloaded app install itself silently; the user
 *    confirms with one tap ("Install unknown apps" grant is requested on
 *    the first install).
 */
class AppUpdater(
    private val context: Context,
    private val settings: AppSettings,
    private val notify: (String) -> Unit,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** Non-null ⇒ the update dialog is shown. Cleared by Later/Skip. */
    private val _available = MutableStateFlow<UpdateInfo?>(null)
    val available: StateFlow<UpdateInfo?> = _available.asStateFlow()

    private val _progress = MutableStateFlow<UpdateProgress>(UpdateProgress.Idle)
    val progress: StateFlow<UpdateProgress> = _progress.asStateFlow()

    @Volatile private var cancelRequested = false

    private val http = okhttp3.OkHttpClient.Builder()
        .connectTimeout(20, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
        .followRedirects(true) // the site 302s /api/download → the file; CDN hops are fine here
        .build()

    /** Startup check — silently does nothing when not due yet. */
    fun autoCheckIfDue() {
        scope.launch {
            val prefsNow = settings.prefsSnapshot()
            if (!prefsNow.updatesAutoCheck) return@launch
            if (System.currentTimeMillis() - prefsNow.lastUpdateCheck < CHECK_INTERVAL_MS) return@launch
            val info = runCatching { checkLocked(force = false) }.getOrNull()
            if (info != null) {
                _available.value = info
                notify("Update available — v${info.version}")
            }
        }
    }

    /** Settings → "Check for updates": always runs, honest about the result. */
    fun checkNow(onResult: (UpdateInfo?) -> Unit) {
        scope.launch {
            val info = runCatching { checkLocked(force = true) }.getOrNull()
            // A found update must ALSO open the dialog (AppRoot watches
            // `available`) — not just answer this callback (found live
            // 2026-11-16: the manual check was silent when an update existed).
            if (info != null) _available.value = info
            onResult(info)
        }
    }

    private suspend fun checkLocked(force: Boolean): UpdateInfo? {
        settings.setLastUpdateCheck(System.currentTimeMillis())
        val prefsNow = settings.prefsSnapshot()
        val info = withContext(Dispatchers.IO) {
            runCatching {
                http.newCall(
                    okhttp3.Request.Builder()
                        .url(FEED_URL)
                        .header("Cache-Control", "no-cache")
                        .build()
                ).execute().use { resp ->
                    if (!resp.isSuccessful) return@use null
                    parseFeed(resp.body?.string() ?: return@use null)
                }
            }.onFailure { Log.w(TAG, "feed fetch failed: ${it.message}") }.getOrNull()
        } ?: return null
        if (!force && info.version == prefsNow.updateSkipVersion) return null
        return if (isNewer(info)) info else null
    }

    /** Download + verify; success lands in [progress] as [UpdateProgress.Ready]. */
    fun startDownload(info: UpdateInfo) {
        cancelRequested = false
        _progress.value = UpdateProgress.Downloading(0, info.size)
        scope.launch {
            val result = runCatching { downloadLocked(info) }
            _progress.value = result.fold(
                onSuccess = { UpdateProgress.Ready(it) },
                onFailure = {
                    if (cancelRequested) UpdateProgress.Idle
                    else UpdateProgress.Error(it.message ?: "download failed")
                },
            )
        }
    }

    fun cancelDownload() {
        cancelRequested = true
    }

    private suspend fun downloadLocked(info: UpdateInfo): File = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        dir.listFiles()?.forEach { it.delete() } // one update payload at a time
        val part = File(dir, info.fileName + ".part")
        try {
            http.newCall(okhttp3.Request.Builder().url("$BASE_URL/${info.fileName}").build())
                .execute().use { resp ->
                    if (!resp.isSuccessful) throw IllegalStateException("HTTP ${resp.code}")
                    val body = resp.body ?: throw IllegalStateException("empty response")
                    val declared = resp.header("Content-Length")?.toLongOrNull()
                    if (declared != null && declared != info.size) {
                        throw IllegalStateException("feed is stale (size mismatch)")
                    }
                    val digest = MessageDigest.getInstance("SHA-256")
                    part.outputStream().use { out ->
                        body.byteStream().use { src ->
                            val buf = ByteArray(64 * 1024)
                            var read = 0L
                            var lastPosted = 0L
                            while (true) {
                                if (cancelRequested) throw InterruptedException()
                                val n = src.read(buf)
                                if (n == -1) break
                                out.write(buf, 0, n)
                                digest.update(buf, 0, n)
                                read += n
                                if (read - lastPosted >= PROGRESS_EVERY_BYTES) {
                                    lastPosted = read
                                    _progress.value = UpdateProgress.Downloading(read, info.size)
                                }
                            }
                        }
                    }
                    _progress.value = UpdateProgress.Verifying
                    if (part.length() != info.size) throw IllegalStateException("incomplete download")
                    val hex = digest.digest().joinToString("") { "%02x".format(it) }
                    if (!hex.equals(info.sha256, ignoreCase = true)) {
                        throw IllegalStateException("checksum mismatch")
                    }
                }
            val final = File(dir, info.fileName)
            if (!part.renameTo(final)) throw IllegalStateException("could not finalize file")
            final
        } catch (e: Exception) {
            part.delete()
            throw e
        }
    }

    /** Fire the system installer for a verified APK (one-tap user confirm). */
    fun install(file: File) {
        val uri: Uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        runCatching { context.startActivity(intent) }
            .onFailure {
                Log.w(TAG, "install intent failed: ${it.message}")
                notify("Could not open the installer")
            }
    }

    /** versionCode wins when the feed carries it; dotted-name fallback otherwise. */
    private fun isNewer(info: UpdateInfo): Boolean = when {
        info.versionCode > 0 -> info.versionCode > BuildConfig.VERSION_CODE
        else -> tupleNewer(tuple(info.version), tuple(BuildConfig.VERSION_NAME))
    }

    private fun tuple(v: String): List<Int> = v.split('.').map { it.toIntOrNull() ?: 0 }

    /** Dotted-version lexicographic compare (1.10.0 > 1.9.9; missing parts = 0). */
    private fun tupleNewer(a: List<Int>, b: List<Int>): Boolean {
        for (i in 0 until maxOf(a.size, b.size)) {
            val x = a.getOrElse(i) { 0 }
            val y = b.getOrElse(i) { 0 }
            if (x != y) return x > y
        }
        return false
    }

    fun dismiss() {
        _available.value = null
        _progress.value = UpdateProgress.Idle
    }

    fun skip(version: String, persist: (String) -> Unit) {
        persist(version)
        dismiss()
    }

    companion object {
        private const val TAG = "AppUpdater"
        const val BASE_URL = "https://deepflux.space/deepflux"
        const val FEED_URL = "$BASE_URL/latest-android.json"
        private const val CHECK_INTERVAL_MS = 20L * 60 * 60 * 1000 // ~daily, fuzzed under midnight relaunches
        private const val PROGRESS_EVERY_BYTES = 1L * 1024 * 1024

        private val VERSION_RE = Regex("^\\d+\\.\\d+\\.\\d+$")
        private val FILE_RE = Regex("^DeepFlux-[0-9.]+-Android\\.apk$")
        private val SHA_RE = Regex("^[0-9a-fA-F]{64}$")

        /**
         * STRICT parser — anything malformed returns null (never crashes on,
         * never half-applies a bad feed). Mirrors the desktop parse_feed
         * stance: basename-only file, hex sha256, size sanity-bounded.
         */
        fun parseFeed(text: String): UpdateInfo? = runCatching {
            val obj = org.json.JSONObject(text)
            val version = obj.optString("version")
            val file = obj.optString("file")
            val sha = obj.optString("sha256")
            val size = obj.optLong("size", -1)
            if (!VERSION_RE.matches(version)) return null
            if (!FILE_RE.matches(file)) return null
            if (!SHA_RE.matches(sha)) return null
            if (size !in 10_000_000..600_000_000) return null
            UpdateInfo(
                version = version,
                versionCode = obj.optInt("versionCode", 0),
                fileName = file,
                size = size,
                sha256 = sha.lowercase(),
                released = obj.optString("released"),
                notes = obj.optString("notes"),
            )
        }.getOrNull()
    }
}
