package space.deepflux.android.engine

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import org.libtorrent4j.TorrentStatus
import org.libtorrent4j.alerts.Alert
import org.libtorrent4j.alerts.MetadataReceivedAlert
import org.libtorrent4j.alerts.TorrentErrorAlert
import org.libtorrent4j.alerts.TorrentFinishedAlert
import space.deepflux.android.DeepFluxApp
import space.deepflux.android.MainActivity
import space.deepflux.android.R
import space.deepflux.android.data.AppSettings
import space.deepflux.android.data.AppPrefs
import space.deepflux.android.service.TorrentService
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.UUID

/**
 * Single source of truth for the torrent list: owns the [TorrentEngine],
 * the UI-facing [torrents] StateFlow, magnet-first persistence across
 * restarts, the Wi-Fi/charging gates, finished/error notifications, and
 * the foreground-service lifecycle.
 *
 * Threading contract (mirrors the desktop's queue-based engine): every
 * engine call happens on [engineDispatcher] — a single-threaded view of
 * Dispatchers.IO. UI (Compose) and the alert thread only touch flows and
 * @Volatile fields.
 */
class TorrentRepository(private val context: Context) {

    private val engine = TorrentEngine()
    private val settings = AppSettings(context)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val engineDispatcher = Dispatchers.IO.limitedParallelism(1)

    private val _torrents = MutableStateFlow<List<TorrentItem>>(emptyList())
    val torrents: StateFlow<List<TorrentItem>> = _torrents.asStateFlow()

    /** One-shot messages surfaced as snackbars. */
    private val _notices = MutableSharedFlow<String>(extraBufferCapacity = 8)
    val notices: SharedFlow<String> = _notices.asSharedFlow()

    val prefs: StateFlow<AppPrefs> = settings.prefs
        .stateIn(scope, kotlinx.coroutines.flow.SharingStarted.Eagerly, AppPrefs())

    /** Persisted entries — magnet-first: metadata is re-fetched via DHT after restart. */
    data class Entry(
        val infoHash: String,
        val magnetUri: String,
        val name: String,
        val sequential: Boolean,
        val addedAt: Long,
    )

    private val entries = java.util.concurrent.CopyOnWriteArrayList<Entry>()

    /** Pause is app-level state (TorrentStatus has no paused flag in libtorrent4j). */
    private val pausedHashes = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    /** Info-hashes whose sequential piece order has been applied post-metadata. */
    private val appliedSequential = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    /** Cached absolute path of the first video file per torrent (v0.2 streaming). */
    private val videoPathByHash = java.util.concurrent.ConcurrentHashMap<String, String>()

    private val streamServer = space.deepflux.android.stream.LocalStreamServer()

    /** Opens the torrent's first video file in the player — works mid-download. */
    /** Returns true when the player was actually launched. */
    fun playVideo(item: TorrentItem, context: android.content.Context): Boolean {
        val path = item.firstVideoPath.ifBlank {
            withContextBlocking { engine.firstVideoFile(item.infoHash, saveDir) }?.also {
                videoPathByHash[item.infoHash] = it
            }
        }
        if (path.isNullOrBlank() || !java.io.File(path).exists()) {
            _notices.tryEmit("No playable video in this torrent yet")
            return false
        }
        val url = streamServer.registerAndGetUrl(java.io.File(path))
        if (url == null) {
            _notices.tryEmit("Could not start the stream server")
            return false
        }
        val mime = mimeForPath(path)
        val intent = android.content.Intent(context, space.deepflux.android.player.PlayerActivity::class.java)
            .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_URL, url)
            .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_TITLE, item.name)
            .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_MIME, mime)
            // Harmless from an Activity context; REQUIRED when the agent's
            // play_torrent tool calls this with the application context.
            .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
        return true
    }

    /** The save location every download lane shares (torrents + plain HTTP). */
    val downloadsDir: File get() = saveDir

    /**
     * Play a MediaStore content URI (Commander's public Downloads/Videos
     * roots) through the loopback server — same player, same ✕ bar. Returns
     * true when the player launched.
     */
    fun playContentUri(uri: android.net.Uri, title: String, mime: String, context: android.content.Context): Boolean {
        val resolver = context.contentResolver
        val length = runCatching {
            resolver.openAssetFileDescriptor(uri, "r")?.use { it.length } ?: -1L
        }.getOrDefault(-1L)
        if (length <= 0) {
            _notices.tryEmit("Cannot open this item")
            return false
        }
        val url = streamServer.registerStream(
            space.deepflux.android.stream.LocalStreamServer.StreamSource(title, length, mime.ifBlank { "video/mp4" }) { start ->
                val s = resolver.openInputStream(uri)
                    ?: throw java.io.IOException("content provider went away")
                if (start > 0) {
                    var toSkip = start
                    while (toSkip > 0) {
                        val n = s.skip(toSkip)
                        if (n <= 0) break
                        toSkip -= n
                    }
                }
                s
            }
        ) ?: run {
            _notices.tryEmit("Could not start the stream server")
            return false
        }
        val intent = android.content.Intent(context, space.deepflux.android.player.PlayerActivity::class.java)
            .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_URL, url)
            .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_TITLE, title)
            .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
        return true
    }

    /**
     * Container hint for the player, or null to let it sniff the bytes.
     * Only extensions whose container is certain get a hint: the A4 format
     * matrix (2026-11-14) showed AVI/FLV/3GP/audio files all play when
     * sniffed, so labelling everything else "video/mp4" was just wrong.
     */
    private fun mimeForPath(path: String): String? = when {
        path.endsWith(".webm", true) -> "video/webm"
        path.endsWith(".mkv", true) -> "video/x-matroska"
        path.endsWith(".ts", true) -> "video/mp2t"
        path.endsWith(".mp4", true) || path.endsWith(".m4v", true) ||
            path.endsWith(".mov", true) -> "video/mp4"
        else -> null
    }

    /** Snackbar sink for sibling repositories (the HTTP download engine). */
    fun postNotice(message: String) {
        _notices.tryEmit(message)
    }

    /** Network gate for the HTTP download engine — same wifi/charging policy. */
    fun networkGateOpen(): Boolean = gatesOk()

    /**
     * Play ANY already-downloaded file (plain-HTTP downloads land here;
     * torrents use playVideo which resolves their video file first) —
     * registers the loopback stream + launches the player. Returns true
     * when the player launched.
     */
    fun playFile(path: String, title: String, context: android.content.Context): Boolean {
        val file = java.io.File(path)
        if (!file.exists()) {
            _notices.tryEmit("File is gone: $path")
            return false
        }
        val url = streamServer.registerAndGetUrl(file)
            ?: run {
                _notices.tryEmit("Could not start the stream server")
                return false
            }
        val mime = mimeForPath(path)
        val intent = android.content.Intent(context, space.deepflux.android.player.PlayerActivity::class.java)
            .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_URL, url)
            .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_TITLE, title)
            .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_MIME, mime)
            // §13 #8: the real path lets subtitle search use the exact file hash.
            .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_FILE_PATH, file.absolutePath)
            .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
        return true
    }

    private fun <T> withContextBlocking(block: () -> T): T? {
        // Tiny helper: run an engine call on its dispatcher synchronously from
        // a UI click path. Engine calls are fast (handle lookups); acceptable.
        val holder = arrayOfNulls<Any?>(1)
        val latch = java.util.concurrent.CountDownLatch(1)
        scope.launch {
            holder[0] = withContext(engineDispatcher) { runCatching(block).getOrNull() }
            latch.countDown()
        }
        latch.await(3, java.util.concurrent.TimeUnit.SECONDS)
        @Suppress("UNCHECKED_CAST")
        return holder[0] as? T
    }

    @Volatile
    private var lastGateOk: Boolean? = null

    private var eventId = 2000

    private val saveDir: File
        get() = File(context.filesDir, "DeepFlux")
    // ALWAYS internal storage (2026-11-16, owner-reported "torrents don't
    // move"): the scoped-storage FUSE MediaProvider wedges/aborts on
    // libtorrent's file pattern — the 2026-11-12 NodeTracker abort
    // ("active_nodes_.find(node) != active_nodes_.end()", SIGABRT in
    // com.android.providers.media.module) recurred live on a RELEASE build
    // (external files dir) while the same magnet on internal storage pulled
    // 145 seeds / 5 MB/s on the identical emulator+network minutes later.
    // The user-facing destination is preserved by exportFinishedFiles()
    // (MediaStore copy to public Downloads on completion).

    private val stateFile: File
        get() = File(context.filesDir, "torrents_state.json")

    /**
     * Opens once the saved entry list has been read. No mutation may persist
     * before then: an add() racing ahead of the load would overwrite — and
     * erase — every saved entry (same class of loss the HTTP engine hit and
     * fixed on 2026-11-14; the torrent file is already written atomically).
     */
    private val loadedLatch = java.util.concurrent.CountDownLatch(1)

    private fun awaitLoaded() {
        loadedLatch.await(3, java.util.concurrent.TimeUnit.SECONDS)
    }

    // ------------------------------------------------------------------ lifecycle

    fun start() {
        engine.listener = alertListener
        scope.launch {
            try {
                withContext(engineDispatcher) {
                    runCatching { engine.start() }
                        .onFailure { Log.e(TAG, "engine start failed: ${it.message}") }
                }
                loadState()
                withContext(engineDispatcher) {
                    entries.forEach { e ->
                        // Sequential preference is (re)applied post-metadata by the poller.
                        // Prefer the saved .torrent (metadata intact) — private-tracker
                        // torrents cannot re-fetch metadata from DHT (disabled by design).
                        val saved = File(File(context.filesDir, "torrents"), "${e.infoHash}.torrent")
                        val added = if (saved.isFile) {
                            runCatching { engine.addTorrentFile(saved.absolutePath, saveDir) }.getOrNull()
                        } else {
                            null
                        }
                        if (added == null) {
                            runCatching { engine.addMagnet(e.magnetUri, saveDir) }
                        }
                    }
                }
                refreshNow()
                poller()
            } finally {
                loadedLatch.countDown()
            }
        }
    }

    private suspend fun poller() {
        while (scope.isActive) {
            delay(POLL_MS)
            refreshNow()
        }
    }

    // ------------------------------------------------------------------ public API

    fun addMagnet(text: String, sequentialOverride: Boolean? = null) {
        awaitLoaded() // no mutation may persist before the saved list loads
        val magnet = MagnetParser.extractMagnet(text.trim()) ?: text.trim()
        val hash = MagnetParser.infoHashHex(magnet)
        if (hash == null) {
            _notices.tryEmit("Not a valid magnet link")
            return
        }
        if (entries.any { it.infoHash == hash }) {
            _notices.tryEmit("Already in the list")
            return
        }
        val sequential = sequentialOverride ?: prefs.value.sequentialDefault
        scope.launch {
            // Check + add INSIDE the single-threaded dispatcher: two rapid
            // addMagnet calls (e.g. the VIEW intent redelivered on activity
            // recreation) used to both pass the dedupe check and add the same
            // hash twice — a duplicate LazyColumn key that crashed the app.
            val added = withContext(engineDispatcher) {
                if (entries.any { it.infoHash == hash }) {
                    null
                } else {
                    val h = runCatching { engine.addMagnet(magnet, saveDir) }.getOrNull()
                    if (h != null) {
                        entries.add(Entry(h, magnet, "", sequential, System.currentTimeMillis()))
                        h
                    } else {
                        null
                    }
                }
            }
            if (added == null) {
                _notices.tryEmit("Could not add magnet")
            } else {
                persist()
                refreshNow()
                ensureService()
                _notices.tryEmit("Added — fetching metadata")
            }
        }
    }

    fun addTorrentFromUri(uri: Uri) {
        awaitLoaded() // no mutation may persist before the saved list loads
        scope.launch {
            val tmp = File(context.cacheDir, "import-${UUID.randomUUID()}.torrent")
            val ok = runCatching {
                val input = context.contentResolver.openInputStream(uri) ?: return@runCatching false
                input.use { src ->
                    tmp.outputStream().use { dst ->
                        src.copyTo(dst)
                        true
                    }
                }
            }.getOrDefault(false)
            if (!ok) {
                _notices.tryEmit("Could not read the .torrent file")
                return@launch
            }
            importTorrentFile(tmp)
        }
    }

    /**
     * Add a .torrent already on disk (the in-app browser saves intercepted
     * torrent downloads into cacheDir and hands them here). The file is
     * consumed (deleted) either way.
     */
    fun addTorrentFile(file: File) {
        awaitLoaded() // no mutation may persist before the saved list loads
        scope.launch { importTorrentFile(file) }
    }

    private suspend fun importTorrentFile(tmp: File) {
        run {
            val sequential = prefs.value.sequentialDefault
            val hash = withContext(engineDispatcher) {
                runCatching { engine.addTorrentFile(tmp.absolutePath, saveDir) }.getOrNull()
            }
            if (hash != null) {
                // Restart-proof .torrent store (private-tracker fix, 2026-11-16):
                // keep the bytes under filesDir/torrents/<hash>.torrent so start()
                // can re-add WITH metadata — a bare magnet can't recover it when
                // the torrent is PRIVATE (DHT disabled by design; the restored
                // zombie stays "Fetching info" forever).
                val saved = File(File(context.filesDir, "torrents").apply { mkdirs() }, "$hash.torrent")
                runCatching { tmp.copyTo(saved, overwrite = true) }
            }
            tmp.delete()
            if (hash == null || entries.any { it.infoHash == hash }) {
                if (hash != null) _notices.tryEmit("Already in the list")
                else _notices.tryEmit("Not a valid .torrent file")
                return
            }
            // Persist a hash-magnet so restarts can re-add the torrent (an
            // empty magnetUri silently never came back — zombie rows stuck
            // at 0 peers); start() prefers the saved .torrent file above.
            entries.add(
                Entry(
                    hash,
                    "magnet:?xt=urn:btih:$hash",
                    hash,
                    sequential,
                    System.currentTimeMillis(),
                ),
            )
            persist()
            refreshNow()
            ensureService()
            _notices.tryEmit("Torrent added")
        }
    }

    /** Entry point for VIEW (magnet:/content) and SEND intents. */
    fun addFromIntent(intent: Intent?) {
        if (intent == null) return
        when (intent.action) {
            Intent.ACTION_VIEW -> {
                val data = intent.data ?: return
                if (data.scheme.equals("magnet", ignoreCase = true)) {
                    addMagnet(data.toString())
                } else {
                    addTorrentFromUri(data)
                }
            }
            Intent.ACTION_SEND -> {
                val text = intent.getStringExtra(Intent.EXTRA_TEXT)
                if (!text.isNullOrBlank() && text.contains("magnet:", ignoreCase = true)) {
                    addMagnet(text)
                } else {
                    @Suppress("DEPRECATION")
                    val stream = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
                    if (stream != null) addTorrentFromUri(stream)
                }
            }
        }
    }

    fun pause(infoHash: String) {
        awaitLoaded() // no mutation may persist before the saved list loads
        pausedHashes.add(infoHash)
        torrentOp(infoHash, "Paused") { engine.pauseTorrent(it) }
    }

    fun resume(infoHash: String) {
        awaitLoaded() // no mutation may persist before the saved list loads
        pausedHashes.remove(infoHash)
        torrentOp(infoHash, "Resumed") { engine.resumeTorrent(it) }
    }

    fun remove(infoHash: String) {
        awaitLoaded() // no mutation may persist before the saved list loads
        val entry = entries.firstOrNull { it.infoHash == infoHash } ?: return
        scope.launch {
            withContext(engineDispatcher) {
                runCatching { engine.removeTorrent(infoHash) }
            }
            entries.remove(entry)
            pausedHashes.remove(infoHash)
            appliedSequential.remove(infoHash)
            runCatching { File(File(context.filesDir, "torrents"), "$infoHash.torrent").delete() }
            persist()
            _torrents.value = _torrents.value.filterNot { it.infoHash == infoHash }
            _notices.tryEmit("Removed “${entry.name.ifBlank { entry.infoHash.take(12) }}”")
        }
    }

    private fun torrentOp(infoHash: String, notice: String, op: (String) -> Unit) {
        scope.launch {
            withContext(engineDispatcher) { runCatching { op(infoHash) } }
            refreshNow()
            ensureService()
            _notices.tryEmit(notice)
        }
    }

    /** Start the FGS from a user-gesture path (add/pause/resume/UI start). */
    fun ensureService() {
        runCatching {
            ContextCompat.startForegroundService(context, TorrentService.startIntent(context))
        }.onFailure { Log.w(TAG, "FGS start failed: ${it.message}") }
    }

    // ------------------------------------------------------------------ settings passthrough (UI)

    fun setWifiOnly(value: Boolean) = scope.launch { settings.setWifiOnly(value) }

    fun setChargingOnly(value: Boolean) = scope.launch { settings.setChargingOnly(value) }

    fun setSequentialDefault(value: Boolean) = scope.launch { settings.setSequentialDefault(value) }

    fun setCustomApiKey(value: String) = scope.launch { settings.setCustomApiKey(value) }

    fun setOpensubtitlesApiKey(value: String) = scope.launch { settings.setOpensubtitlesApiKey(value) }

    fun setHomepage(value: String) = scope.launch { settings.setHomepage(value) }

    fun setUiMode(value: String) = scope.launch { settings.setUiMode(value) }

    fun setUpdatesAutoCheck(value: Boolean) = scope.launch { settings.setUpdatesAutoCheck(value) }

    fun setUpdateSkipVersion(value: String) = scope.launch { settings.setUpdateSkipVersion(value) }

    /** Direct DataStore access for the backup card (plan §13 #6). */
    val appSettings: AppSettings get() = settings

    fun engineVersion(): String = engine.libtorrentVersion()

    /** Tracker announce URLs (agent diagnostics, §13 #10) — engine thread. */
    fun torrentTrackers(infoHash: String): List<String> =
        withContextBlocking { engine.trackerUrls(infoHash) }.orEmpty()

    fun loadChatHistory(): String = settings.loadChatHistory()

    fun setLanguage(chinese: Boolean) =
        scope.launch { settings.setLanguage(if (chinese) "zh" else "en") }

    fun loadLanguageIsChinese(): Boolean = settings.loadLanguage() == "zh"

    fun setCustomSaveDir(uri: String) = scope.launch { settings.setCustomSaveDir(uri) }

    /** Callback the activity sets to open the SAF folder picker. */
    @Volatile
    var pickFolder: (() -> Unit)? = null

    fun saveChatHistory(json: String) = scope.launch { settings.saveChatHistory(json) }

    // ------------------------------------------------------------------ refresh + gates

    private suspend fun refreshNow() {
        val snapshot = withContext(engineDispatcher) {
            val gateOk = gatesOk()
            if (gateOk != lastGateOk) {
                lastGateOk = gateOk
                if (gateOk) engine.resumeSession() else engine.pauseSession()
                Log.i(TAG, "network gate -> ${if (gateOk) "open" else "paused (wifi/charging policy)"}")
            }
            // Retry sequential application until metadata exists (once per torrent).
            entries.filter { it.sequential && infoHashPending(it.infoHash) }
                .forEach { e -> if (engine.applySequential(e.infoHash)) appliedSequential.add(e.infoHash) }
            engine.snapshot(entries.map { it.infoHash })
        }

        val items = snapshot.mapNotNull { (hex, st) ->
            val entry = entries.firstOrNull { it.infoHash == hex } ?: return@mapNotNull null
            st ?: return@mapNotNull null
            entry.toItem(st)
        }
        _torrents.value = items
    }

    private fun gatesOk(): Boolean {
        // Public via networkGateOpen() below — shared with the HTTP download
        // engine so both lanes obey the SAME wifi/charging policy.
        val prefsNow = prefs.value
        if (prefsNow.wifiOnly) {
            val cm = context.getSystemService(ConnectivityManager::class.java) ?: return true
            if (cm.isActiveNetworkMetered) return false
        }
        if (prefsNow.chargingOnly && !isCharging()) return false
        return true
    }

    private fun isCharging(): Boolean {
        val bm = context.getSystemService(BatteryManager::class.java) ?: return true
        val status = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_STATUS)
        return status == BatteryManager.BATTERY_STATUS_CHARGING ||
            status == BatteryManager.BATTERY_STATUS_FULL
    }

    private fun infoHashPending(hash: String): Boolean = !appliedSequential.contains(hash)

    /** Video path per torrent — cached once found (metadata needed, checked on the engine thread). */
    private fun videoPathFor(hash: String): String {
        videoPathByHash[hash]?.let { return it }
        scope.launch {
            val p = withContext(engineDispatcher) {
                runCatching { engine.firstVideoFile(hash, saveDir) }.getOrNull()
            }
            if (!p.isNullOrBlank()) videoPathByHash[hash] = p
        }
        return videoPathByHash[hash] ?: ""
    }

    private fun Entry.toItem(st: TorrentStatus): TorrentItem {
        var state = mapState(st)
        if (state != TorrentState.ERROR && pausedHashes.contains(infoHash)) {
            state = TorrentState.PAUSED
        }
        val dnName = magnetUri.substringAfter("dn=", "").substringBefore('&').takeIf { it.isNotBlank() }
        val name = runCatching { st.name() }.getOrNull().orIfBlank(name).orIfBlank(dnName ?: "Fetching…")
        val ec = runCatching { st.errorCode() }.getOrNull()
        return TorrentItem(
            infoHash = infoHash,
            name = name,
            magnetUri = magnetUri,
            sequential = sequential,
            addedAt = addedAt,
            state = state,
            progress = runCatching { st.progressPpm() / 1_000_000f }.getOrDefault(0f),
            totalBytes = runCatching { st.totalWanted() }.getOrDefault(0L),
            doneBytes = runCatching { st.totalWantedDone() }.getOrDefault(0L),
            downloadRate = runCatching { st.downloadRate().toLong() }.getOrDefault(0L),
            uploadRate = runCatching { st.uploadRate().toLong() }.getOrDefault(0L),
            peers = runCatching { st.numPeers() }.getOrDefault(0),
            seeds = runCatching { st.numSeeds() }.getOrDefault(0),
            errorMessage = if (ec != null && ec.isError) (ec.message ?: "") else "",
            firstVideoPath = videoPathFor(infoHash),
        )
    }

    private fun mapState(st: TorrentStatus): TorrentState = when (st.state()) {
        TorrentStatus.State.CHECKING_FILES -> TorrentState.CHECKING
        TorrentStatus.State.CHECKING_RESUME_DATA -> TorrentState.CHECKING
        TorrentStatus.State.DOWNLOADING_METADATA -> TorrentState.METADATA
        TorrentStatus.State.DOWNLOADING -> TorrentState.DOWNLOADING
        TorrentStatus.State.FINISHED -> TorrentState.FINISHED
        TorrentStatus.State.SEEDING -> TorrentState.SEEDING
        else -> TorrentState.UNKNOWN
    }

    private fun String?.orIfBlank(fallback: String): String =
        if (isNullOrBlank()) fallback else this

    // ------------------------------------------------------------------ alerts

    private val alertListener = object : org.libtorrent4j.AlertListener {
        override fun types(): IntArray? = null
        override fun alert(alert: Alert<*>) {
            when (alert) {
                is TorrentFinishedAlert -> {
                    val name = alertTorrentName(alert)
                    notifyEvent("Download finished", name.ifBlank { "Torrent" })
                    scope.launch { refreshNow() }
                    // v0.9: export completed video files to public Downloads
                    exportFinishedFiles(alert)
                }
                is TorrentErrorAlert -> {
                    val name = alertTorrentName(alert)
                    val msg = runCatching { alert.error()?.message }.getOrNull().orEmpty()
                    notifyEvent("Download error", "${name.ifBlank { "Torrent" }}: $msg")
                    scope.launch { refreshNow() }
                }
                is MetadataReceivedAlert -> {
                    // Metadata arrived — re-poll so the name shows up immediately.
                    scope.launch { refreshNow() }
                }
                else -> Unit
            }
        }
    }

    /** libtorrent4j alerts carry no torrentName() — read it from the handle's status, guarded. */
    /** Export completed video files to the public Downloads folder (MediaStore). */
    private fun exportFinishedFiles(alert: org.libtorrent4j.alerts.TorrentFinishedAlert) {
        scope.launch(Dispatchers.IO) {
            runCatching {
                val handle = alert.handle()
                val info = handle.torrentFile() ?: return@launch
                val files = info.files()
                for (i in 0 until files.numFiles()) {
                    val path = files.filePath(i) ?: continue
                    if (VIDEO_EXPORT_EXTENSIONS.any { path.endsWith(it, true) }) {
                        val absFile = java.io.File(saveDir, path)
                        if (absFile.exists()) {
                            space.deepflux.android.dlmgr.MediaStoreExport.exportToDownloads(context, absFile)
                        }
                    }
                }
            }.onFailure { Log.w(TAG, "export failed: " + it.message) }
        }
    }

    private fun alertTorrentName(alert: Alert<*>): String =
        runCatching {
            (alert as? org.libtorrent4j.alerts.TorrentAlert<*>)?.handle()?.status()?.name()
        }.getOrNull().orEmpty()

    private fun notifyEvent(title: String, text: String) {
        val intent = PendingIntent.getActivity(
            context,
            0,
            Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(context, DeepFluxApp.CHANNEL_EVENTS)
            .setSmallIcon(R.drawable.ic_stat_deepflux)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(intent)
            .build()
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        runCatching { NotificationManagerCompat.from(context).notify(eventId++, notification) }
    }

    // ------------------------------------------------------------------ persistence

    private fun loadState() {
        val f = stateFile
        if (!f.exists()) return
        runCatching {
            val arr = JSONArray(f.readText())
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                entries.add(
                    Entry(
                        infoHash = o.getString("h"),
                        // HEAL pre-1.4.6 rows written with an empty magnet
                        // (file-added torrents) — a hash-magnet lets start()
                        // re-add them instead of leaving zombie rows.
                        magnetUri = o.optString("m", "").ifBlank {
                            "magnet:?xt=urn:btih:" + o.getString("h")
                        },
                        name = o.optString("n", ""),
                        sequential = o.optBoolean("s", false),
                        addedAt = o.optLong("a", 0L),
                    ),
                )
            }
        }.onFailure { Log.w(TAG, "state load failed: ${it.message}") }
    }

    /** Atomic (temp + move), mirroring the desktop config.to_file lesson. */
    private fun persist() {
        runCatching {
            val arr = JSONArray()
            entries.forEach { e ->
                arr.put(
                    JSONObject()
                        .put("h", e.infoHash)
                        .put("m", e.magnetUri)
                        .put("n", e.name)
                        .put("s", e.sequential)
                        .put("a", e.addedAt),
                )
            }
            val tmp = File(context.filesDir, "torrents_state.json.tmp")
            tmp.writeText(arr.toString())
            Files.move(tmp.toPath(), stateFile.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }.onFailure { Log.w(TAG, "state persist failed: ${it.message}") }
    }

    private companion object {
        const val TAG = "TorrentRepository"
        val VIDEO_EXPORT_EXTENSIONS = listOf(".mp4", ".mkv", ".webm", ".avi", ".mov", ".iso")
        const val POLL_MS = 1_000L
    }
}
