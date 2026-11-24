package space.deepflux.android.data

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
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
import space.deepflux.android.ui.Strings
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
    /**
     * @param waitingForNetwork the download was not started at all because the
     * Wi-Fi-only / charging-only gate is closed — not an error, "Update" retries.
     */
    data class Error(val message: String, val waitingForNetwork: Boolean = false) : UpdateProgress()
}

/** Outcome of a feed check. A check that FAILED must never read as "up to date" (it used to). */
sealed class CheckResult {
    data class Available(val info: UpdateInfo) : CheckResult()

    /** The feed was read and this install is current (or the user skipped that version). */
    data object UpToDate : CheckResult()

    /** The feed could not be read or is malformed. */
    data class Failed(val reason: String) : CheckResult()

    /** This install came from Google Play, which delivers its own updates. */
    data object ManagedByStore : CheckResult()
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
    /** The app's Wi-Fi / charging gate — an update is a ~500 MB download on mobile data otherwise. */
    private val networkGateOpen: () -> Boolean = { true },
    /** Feed + APK base URL; only tests point this at a local server. */
    private val baseUrl: String = BASE_URL,
    /**
     * The update file's address for the installer — the manifest's FileProvider.
     * A parameter so the installer's path can be tested on any host: the real
     * call refuses every Windows path (it compares separators).
     */
    private val installAddress: (File) -> Uri = { f -> FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", f) },
    /** Starts the system installer; false when it could not be. A parameter for the same reason. */
    private val startInstaller: (Intent) -> Boolean = { i -> runCatching { context.startActivity(i) }.isSuccess },
    /** The clock the installer's double-tap guard reads — a parameter so that a test can let time pass. */
    private val elapsedRealtime: () -> Long = { android.os.SystemClock.elapsedRealtime() },
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /**
     * This install came from Google Play. Play then delivers the updates:
     * the website APK is signed with a different key than Play's, so a
     * sideloaded update could never install over it — it would download
     * hundreds of MB and then fail. Both check paths do nothing here.
     */
    val installedByPlayStore: Boolean = isInstalledByPlayStore(context)

    /** A failed check is retried no sooner than this — and never stamps the daily gate (it used to, silencing checks for 20 h after one failure). */
    @Volatile private var lastFailureAt = 0L

    /** Non-null ⇒ the update dialog is shown. Cleared by Later/Skip. */
    private val _available = MutableStateFlow<UpdateInfo?>(null)
    val available: StateFlow<UpdateInfo?> = _available.asStateFlow()

    /**
     * An update trouble that no dialog can show — in a process started again
     * for the consent answer there is none. The app's own notice flow keeps
     * nothing for a listener that is not there yet, so it is HELD here until
     * the app has shown it ([undialoggedShown]). A state, so whoever comes
     * later still finds it.
     */
    private val _undialogged = MutableStateFlow<String?>(null)
    val undialogged: StateFlow<String?> = _undialogged.asStateFlow()

    fun undialoggedShown() {
        _undialogged.value = null
    }

    private val _progress = MutableStateFlow<UpdateProgress>(UpdateProgress.Idle)
    val progress: StateFlow<UpdateProgress> = _progress.asStateFlow()

    /**
     * Which download is THE one. Every start takes the next number; a
     * download that is still winding down (blocked in a read when it was
     * cancelled) notices it is no longer current and then owns nothing — not
     * the payload file, not [progress]. There used to be one shared "cancel"
     * flag: a restart reset it, and the old download carried on writing
     * into the file the new one was writing, and reported its own outcome
     * over the new one's.
     */
    private val currentDownload = java.util.concurrent.atomic.AtomicLong(0)

    /** The download the user cancelled (0 = none). */
    private val cancelledDownload = java.util.concurrent.atomic.AtomicLong(0)

    private fun isLiveDownload(gen: Long): Boolean =
        currentDownload.get() == gen && cancelledDownload.get() != gen

    private val http = okhttp3.OkHttpClient.Builder()
        .connectTimeout(20, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
        .followRedirects(true) // the site 302s /api/download → the file; CDN hops are fine here
        .build()

    /** Startup check — silently does nothing when not due yet. */
    fun autoCheckIfDue() {
        if (installedByPlayStore) return
        scope.launch {
            val prefsNow = settings.prefsSnapshot()
            if (!prefsNow.updatesAutoCheck) return@launch
            if (System.currentTimeMillis() - prefsNow.lastUpdateCheck < CHECK_INTERVAL_MS) return@launch
            if (System.currentTimeMillis() - lastFailureAt < FAILURE_BACKOFF_MS) return@launch
            // An available update also opens the dialog: AppRoot watches `available`.
            checkFeed(force = false)
        }
    }

    /** Settings → "Check for updates": always runs, and says what actually happened. */
    fun checkNow(onResult: (CheckResult) -> Unit) {
        if (installedByPlayStore) {
            onResult(CheckResult.ManagedByStore)
            return
        }
        scope.launch { onResult(checkFeed(force = true)) }
    }

    /** One feed fetch + parse — the only place the network is touched for a check. */
    private fun fetchFeed(): FeedFetch = try {
        http.newCall(
            okhttp3.Request.Builder()
                .url("$baseUrl/latest-android.json")
                .header("Cache-Control", "no-cache")
                .build()
        ).execute().use { resp ->
            if (!resp.isSuccessful) FeedFetch.Failed("HTTP ${resp.code}")
            else FeedFetch.Read(resp.body?.string()?.let(::parseFeed))
        }
    } catch (e: Exception) {
        Log.w(TAG, "feed fetch failed: ${e.message}")
        FeedFetch.Failed(e.message?.take(120) ?: e.javaClass.simpleName)
    }

    private sealed class FeedFetch {
        /** [info] is null when the feed was read but is malformed. */
        class Read(val info: UpdateInfo?) : FeedFetch()
        class Failed(val reason: String) : FeedFetch()
    }

    private suspend fun checkFeed(force: Boolean): CheckResult {
        val prefsNow = settings.prefsSnapshot()
        return when (val fetch = withContext(Dispatchers.IO) { fetchFeed() }) {
            is FeedFetch.Failed -> {
                lastFailureAt = System.currentTimeMillis()
                CheckResult.Failed(fetch.reason)
            }
            is FeedFetch.Read -> {
                val info = fetch.info
                if (info == null) {
                    lastFailureAt = System.currentTimeMillis()
                    return CheckResult.Failed("feed is invalid")
                }
                // Stamped only once the feed was really read. It used to be
                // stamped BEFORE the probe, so one offline launch silenced
                // the auto-check for the next 20 hours.
                // The stamp is bookkeeping: one that cannot be written (a full disk) must
                // neither fail the check that just succeeded nor escape, as on a phone
                // an error escaping a coroutine ends the process.
                runCatching { settings.setLastUpdateCheck(System.currentTimeMillis()) }
                    .onFailure { Log.w(TAG, "could not stamp the update check: ${it.message}") }
                lastFailureAt = 0
                when {
                    !force && info.version == prefsNow.updateSkipVersion -> CheckResult.UpToDate
                    isNewer(info) -> {
                        // AppRoot watches `available` and opens the dialog.
                        _available.value = info
                        CheckResult.Available(info)
                    }
                    else -> CheckResult.UpToDate
                }
            }
        }
    }

    /** Download + verify; success lands in [progress] as [UpdateProgress.Ready]. */
    fun startDownload(info: UpdateInfo) {
        // The same Wi-Fi / charging policy as every other download. The
        // dialog says so and keeps "Update" as the retry.
        if (!networkGateOpen()) {
            _progress.value = UpdateProgress.Error("", waitingForNetwork = true)
            return
        }
        val gen = currentDownload.incrementAndGet()
        installerStartedFor = null
        _progress.value = UpdateProgress.Downloading(0, info.size)
        scope.launch {
            val result = runCatching { downloadLocked(info, gen) }
            // Superseded while it ran: a newer download owns the state now.
            if (currentDownload.get() != gen) return@launch
            _progress.value = result.fold(
                onSuccess = { UpdateProgress.Ready(it) },
                onFailure = {
                    if (cancelledDownload.get() == gen) UpdateProgress.Idle
                    else UpdateProgress.Error(it.message ?: "download failed")
                },
            )
        }
    }

    /** The request of the download in flight — cancel tears its socket down at once. */
    @Volatile
    private var currentCall: okhttp3.Call? = null

    /**
     * The update file the system installer was started for. It is started once
     * per download however often the dialog asks: asked both by the consent
     * screen's answer and by the dialog coming back with the activity, two
     * installer screens ended up on top of each other (review, 2026-11-25).
     */
    @Volatile
    private var installerStartedFor: File? = null

    /** When the installer was last started for it: a tap twice in a heartbeat must not stack two screens. */
    @Volatile
    private var installerStartedAt = 0L

    fun cancelDownload() {
        cancelledDownload.set(currentDownload.get())
        currentCall?.cancel()
    }

    private suspend fun downloadLocked(info: UpdateInfo, gen: Long): File = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        dir.listFiles()?.forEach { it.delete() } // one update payload at a time
        // Named for THIS download. A cancelled one can still be winding down
        // (waiting for a slow server's answer): sharing one ".part", its
        // late arrival truncated the file the new download was writing, and
        // the digest — computed from the stream, not the file — still passed.
        val part = File(dir, info.fileName + ".$gen.part")
        try {
            val call = http.newCall(okhttp3.Request.Builder().url("$baseUrl/${info.fileName}").build())
            currentCall = call
            call.execute().use { resp ->
                if (currentCall === call) currentCall = null
                // Cancelled or restarted while the answer was being awaited: not a byte is written.
                if (!isLiveDownload(gen)) throw InterruptedException()
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
                                if (!isLiveDownload(gen)) throw InterruptedException()
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
                    // Cancelled or restarted while the last bytes were arriving.
                    if (!isLiveDownload(gen)) throw InterruptedException()
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
            // Only the current download may remove the payload: for a
            // superseded one the file on disk is the new download's.
            if (currentDownload.get() == gen) part.delete()
            throw e
        } finally {
            currentCall = null
        }
    }

    /** True once Android lets THIS app start the installer (Settings → "Install unknown apps" → DeepFlux). */
    fun canInstallApps(): Boolean =
        runCatching { context.packageManager.canRequestPackageInstalls() }.getOrDefault(false)

    /**
     * The system screen where the user allows installs from DeepFlux. It is
     * asked for FIRST, for a result — then the installer is the last thing
     * on top of the app, and "Done" / "Open" on its final screen lead back
     * into it. (Asked by the installer itself, that screen stayed in the
     * back stack: the user ended up in Settings, or on the home screen,
     * with the app closed — emulator, 2026-11-24.)
     */
    fun consentIntent(): Intent = Intent(
        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
        Uri.parse("package:${context.packageName}"),
    )

    /** The user came back from [consentIntent] without allowing installs. */
    fun installsNotAllowed() = tell(Strings.t.updatePermissionDenied)

    /**
     * Says an update trouble where it can be seen. While the update dialog is
     * up, in the app; with no dialog — a process started again for the consent
     * answer — held in [undialogged] for the app to say once it is up. (Sent
     * to the notice flow there, it was dropped: nobody was listening yet.)
     */
    private fun tell(text: String) {
        if (_available.value == null) _undialogged.value = text else notify(text)
    }

    /**
     * Fire the system installer for a verified APK (one-tap user confirm).
     * Does nothing while installs from DeepFlux are not allowed — and says
     * so; the consent screen comes first (see [consentIntent]).
     * [expectedSize]: the offered update's size, when the caller kept it
     * across the consent screen — in a process that was started again for
     * that answer, the offer itself is no longer in memory.
     * [userTap]: the user's own tap on "Update now". It starts the installer
     * again although one was started for this file already — that one may
     * have been cancelled, or Android may have said "not installed" — but not
     * twice in a heartbeat ([RETAP_DEBOUNCE_MS]). The automatic asks (the
     * dialog's effect, the consent screen's answer) stay once per download,
     * so two installer screens never stack up.
     */
    fun install(file: File, expectedSize: Long? = null, userTap: Boolean = false) {
        if (!canInstallApps()) {
            installsNotAllowed()
            return
        }
        if (installerStartedFor == file && (!userTap || elapsedRealtime() - installerStartedAt < RETAP_DEBOUNCE_MS)) return
        // Looked at again at the last moment: the system's consent screen may
        // have kept the app away for minutes, and what is handed to the
        // installer must still be the whole verified download.
        val expected = expectedSize ?: _available.value?.size
        if (expected != null && file.length() != expected) {
            Log.w(TAG, "the update file is no longer whole: ${file.length()} of $expected bytes")
            file.delete()
            _progress.value = UpdateProgress.Error(Strings.t.updateFileIncomplete)
            // With no dialog to show it (a process started again for the consent
            // answer), it is held for the app to say — see [undialogged].
            tell(Strings.t.updateFileIncomplete)
            return
        }
        val uri: Uri = runCatching { installAddress(file) }
            .getOrElse {
                Log.w(TAG, "the update file could not be handed to the installer: ${it.message}")
                tell(Strings.t.noticeInstallerFailed)
                return
            }
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        if (startInstaller(intent)) {
            installerStartedFor = file
            installerStartedAt = elapsedRealtime()
        } else {
            Log.w(TAG, "the installer could not be started")
            tell(Strings.t.noticeInstallerFailed)
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

        /**
         * A second tap on "Update now" this soon after the installer was started
         * starts nothing. A tap that comes later is the user's own second try —
         * the first installer was cancelled, or Android said "not installed".
         */
        private const val RETAP_DEBOUNCE_MS = 800L
        const val BASE_URL = "https://deepflux.space/deepflux"
        const val FEED_URL = "$BASE_URL/latest-android.json"
        private const val CHECK_INTERVAL_MS = 20L * 60 * 60 * 1000 // ~daily, fuzzed under midnight relaunches

        /** After a failed check the STARTUP check waits this long before trying again (a manual check always runs). */
        private const val FAILURE_BACKOFF_MS = 60 * 60 * 1000L

        /** Google Play's package — an install it made is updated by Play, never by a sideload. */
        const val PLAY_STORE_PACKAGE = "com.android.vending"
        private const val PROGRESS_EVERY_BYTES = 1L * 1024 * 1024

        /** Who installed this app: Play Store installs are updated by Play itself. */
        fun isInstalledByPlayStore(context: Context): Boolean = runCatching {
            val installer = if (android.os.Build.VERSION.SDK_INT >= 30) {
                context.packageManager.getInstallSourceInfo(context.packageName).installingPackageName
            } else {
                @Suppress("DEPRECATION")
                context.packageManager.getInstallerPackageName(context.packageName)
            }
            installer == PLAY_STORE_PACKAGE
        }.getOrDefault(false)

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
