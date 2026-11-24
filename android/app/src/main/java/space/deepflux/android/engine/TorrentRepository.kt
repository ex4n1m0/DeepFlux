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
import kotlinx.coroutines.cancel
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
import space.deepflux.android.ui.Strings
import java.io.File
import java.io.InputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

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
class TorrentRepository(
    private val context: Context,
    /**
     * The BitTorrent engine. The default is the real libtorrent4j wrapper;
     * tests pass a fake ([TorrentEngineApi]) — the real one needs its native
     * session, which cannot run on the JVM.
     */
    private val engine: TorrentEngineApi = TorrentEngine(),
    /**
     * Test seam: replaces the Wi-Fi / charging reading (the real one needs a
     * metered network or a charger). Null in the app.
     */
    private val gateOverride: (() -> Boolean?)? = null,
    /** Test seam: the clock behind "declined a moment ago" and the staged-file sweep. */
    private val clock: () -> Long = System::currentTimeMillis,
    /** Test seam: the pause between engine start attempts (1.5 s x attempt in the app). */
    private val engineStartRetryMs: Long = ENGINE_START_RETRY_MS,
    /** Test seam: reads the .torrent staged behind a confirmation ([readStagedTorrentCapped]). */
    private val readStagedTorrent: (File) -> TorrentMeta.TorrentInfo? = ::readStagedTorrentCapped,
    /**
     * Test seam: says whether any torrent is active, in place of the engine
     * snapshot (the fake engine has no torrent statuses to report, so
     * "the service starts when torrents become active" could not be tested).
     */
    private val activeOverride: (() -> Boolean?)? = null,
) {

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

    /** Pause is app-level state (TorrentStatus has no paused flag in libtorrent4j); saved with the entry by [persist]. */
    private val pausedHashes = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    /**
     * Torrents whose finished files were copied out for the user. The engine
     * reports "finished" again for every complete torrent at each start
     * (emulator, 2026-11-25): without this the copy ran at every launch — a
     * numbered copy of each file in Downloads (1.4.13), or a copy the user had
     * deleted put back. A run with a failure is not marked, so it is finished
     * at the next start. Saved with the entry.
     */
    private val copiedHashes = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()
    private val copyingHashes = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    /**
     * Torrents this process watched download — added here, or seen downloading.
     * Only those are announced when they finish ([onFinished]).
     */
    private val unfinishedSeen = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    /** A metadata keep in progress, per torrent — the alert and a retry can meet. */
    private val keeping = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    /** Keep attempts per torrent this process, and when the next may run (engine thread only). */
    private val keepTries = HashMap<String, Int>()
    private val keepNextAt = HashMap<String, Long>()
    private val keepTriesMax = 3
    /** How long before a failed keep is tried again; a test sets 0. */
    internal var keepRetryEveryMs = 60_000L

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
            _notices.tryEmit(Strings.t.noticeNoVideoYet)
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
            _notices.tryEmit(Strings.t.noticeCannotOpenItem)
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
     * The Wi-Fi / charging gate as last seen by the poller ([refreshNow];
     * true until the first look, which runs at startup). The download service
     * holds its wake lock only while this is open — with the gate closed the
     * engine is paused and no byte can move, yet the lock used to stay held.
     */
    private val _gateOpen = MutableStateFlow(true)
    val gateOpen: StateFlow<Boolean> = _gateOpen.asStateFlow()

    /**
     * Called (off the main thread, from the poller) once per poll WHILE the
     * Wi-Fi / charging gate is open. DeepFluxApp wires it to the HTTP/HLS
     * lane's `onGateReopened()` — without it, a download the gate paused
     * stayed Paused until the user resumed it by hand (fix 2026-11-23).
     * Every poll rather than only at the transition: the HTTP workers sample
     * the gate per chunk and can pause an item during a blip the poller
     * never sees; the callback is free when nothing waits on the gate.
     */
    var onGateOpened: (() -> Unit)? = null

    /** Main tab a notification asked to open (a `Screen` name), or null. Consumed by AppRoot. */
    private val _requestedTab = MutableStateFlow<String?>(null)
    val requestedTab: StateFlow<String?> = _requestedTab.asStateFlow()

    /** Ask the UI to switch to a tab — notification taps (they used to land on the Agent tab). */
    fun requestTab(tab: String) {
        _requestedTab.value = tab
    }

    fun consumeRequestedTab() {
        _requestedTab.value = null
    }

    /**
     * Play ANY already-downloaded file (plain-HTTP downloads land here;
     * torrents use playVideo which resolves their video file first) —
     * registers the loopback stream + launches the player. Returns true
     * when the player launched.
     */
    fun playFile(path: String, title: String, context: android.content.Context): Boolean {
        val file = java.io.File(path)
        if (!file.exists()) {
            _notices.tryEmit(Strings.t.noticeFileGoneFmt.format(path))
            return false
        }
        val url = streamServer.registerAndGetUrl(file)
            ?: run {
                _notices.tryEmit(Strings.t.noticeStreamServerFailed)
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

    /** The caller's thread waits this long for the saved list — it is a small file read that runs first thing at startup, so this is a no-op in practice. */
    private fun awaitLoaded() {
        loadedLatch.await(3, java.util.concurrent.TimeUnit.SECONDS)
    }

    // ------------------------------------------------------------------ lifecycle

    fun start() {
        engine.listener = alertListener
        scope.launch {
            // The saved list FIRST. It used to wait for the engine to start,
            // so a slow start held up every add, pause and resume (each waits
            // for the list) for as long as the start took.
            try {
                loadState()
                sweepStagedTorrents()
            } finally {
                // The saved list is in (or could not be read) — mutations may
                // persist from here on. This used to sit in a `finally` AFTER
                // the endless poller(), so it never ran and every add / pause /
                // resume / remove tap waited out the full 3 s timeout ON THE
                // MAIN THREAD (found 2026-11-23).
                loadedLatch.countDown()
            }
            if (startEngine()) {
                withContext(engineDispatcher) {
                    // What a keep writes and then moves into place: one left behind by a
                    // start that was cut short is no torrent. Only one that has been lying
                    // there for a minute — an import writes its own copy through such a
                    // file right now.
                    val aMinuteAgo = System.currentTimeMillis() - 60_000
                    runCatching {
                        File(context.filesDir, "torrents")
                            .listFiles { f -> f.name.endsWith(".tmp") && f.lastModified() < aMinuteAgo }
                            ?.forEach { it.delete() }
                    }
                    entries.forEach { e ->
                        // Sequential preference is (re)applied post-metadata by the poller.
                        // Prefer the saved .torrent (metadata intact) — private-tracker
                        // torrents cannot re-fetch metadata from DHT (disabled by design).
                        val saved = torrentFileOf(e.infoHash)
                        val kept = if (saved.isFile) {
                            runCatching { engine.addTorrentFile(saved.absolutePath, saveDir) }.getOrNull()
                        } else {
                            null
                        }
                        if (kept is TorrentFileAdd.Added) {
                            // It must be THIS torrent (the v2 prefix: a magnet may carry
                            // a torrent's v2 hash under `btih:`). A kept file that is some
                            // OTHER torrent would add what nobody asked for — invisible
                            // in the list, its files copied out when it completes.
                            if (kept.infoHash == e.infoHash || kept.v2Prefix == e.infoHash) {
                                // The torrent's own name beats a placeholder: adds from a
                                // .torrent file used to be stored under their info-hash.
                                renameEntry(e.infoHash, kept.name)
                            } else {
                                // (Not when another entry owns that torrent: its own turn adds it.)
                                if (entries.none { it.infoHash == kept.infoHash }) runCatching { engine.removeTorrent(kept.infoHash) }
                                runCatching { saved.delete() }
                                runCatching { engine.addMagnet(e.magnetUri, saveDir) }
                            }
                        } else {
                            // A kept file that is no torrent, or carries a symbolic link, is
                            // of no use to anyone — and in the way: the metadata is kept only
                            // while there is none. Dropped, it is kept afresh when the info
                            // arrives. But it may be all a private torrent will ever get, so
                            // "no torrent" is taken from the app's own reader too — a
                            // moment's read error is not a verdict — and one the engine merely
                            // could not take just now stays as it is.
                            val noTorrentByOurReader = runCatching { TorrentMeta.readTorrent(saved.readBytes()) == null }.getOrDefault(false)
                            if (kept === TorrentFileAdd.HasSymbolicLinks ||
                                (kept === TorrentFileAdd.NotATorrent && noTorrentByOurReader)
                            ) {
                                runCatching { saved.delete() }
                            }
                            runCatching { engine.addMagnet(e.magnetUri, saveDir) }
                        }
                    }
                    // A restart used to resume everything — including what the
                    // user had paused by hand, and what "Pause all" had left for
                    // "Resume all" to restore. The pause is saved with the entry.
                    entries.filter { pausedHashes.contains(it.infoHash) }
                        .forEach { e -> runCatching { engine.pauseTorrent(e.infoHash) } }
                }
            }
            refreshNow()
            poller()
        }
    }

    /**
     * The engine gets a few tries. One failed start used to be final: every
     * later engine call then silently did nothing, and nothing downloaded
     * until the next app start. Entries are only re-added once it runs —
     * added to a session that is not there, they would be rows that never
     * move.
     */
    private suspend fun startEngine(): Boolean {
        var attempt = 0
        while (true) {
            val failure = withContext(engineDispatcher) { runCatching { engine.start() }.exceptionOrNull() }
            if (failure == null) {
                engineReady.complete(true)
                return true
            }
            attempt++
            Log.e(TAG, "engine start failed (attempt $attempt): ${failure.message}")
            if (attempt >= ENGINE_START_ATTEMPTS) {
                engineReady.complete(false)
                _notices.tryEmit(Strings.t.noticeEngineStartFailed)
                return false
            }
            delay(engineStartRetryMs * attempt)
        }
    }

    /**
     * Answered once by [startEngine]: true when the engine runs. An add that
     * arrives while the engine is still starting — or retrying — waits for
     * it instead of failing with "could not add" (second review).
     */
    private val engineReady = kotlinx.coroutines.CompletableDeferred<Boolean>()

    private suspend fun awaitEngine(): Boolean = engineReady.await()

    /**
     * A .torrent staged for a confirmation that never came (the process died
     * with the dialog up, a page fired a volley) does not evaporate on its
     * own. Anything older than [STAGED_SWEEP_AGE_MS] goes at startup; a
     * younger one may belong to a request still being read.
     */
    private fun sweepStagedTorrents() {
        val dir = context.cacheDir ?: return
        val cutoff = clock() - STAGED_SWEEP_AGE_MS
        dir.listFiles { f -> f.isFile && f.name.endsWith(".torrent") && STAGED_PREFIXES.any { f.name.startsWith(it) } }
            ?.filter { it.lastModified() < cutoff }
            ?.forEach { it.delete() }
    }

    private suspend fun poller() {
        while (scope.isActive) {
            delay(POLL_MS)
            refreshNow()
        }
    }

    /** Stops the poller and pending work. Tests only — the app never shuts the repository down. */
    @androidx.annotation.VisibleForTesting
    internal fun shutdownForTests() {
        scope.cancel()
    }

    /** One poll, on demand — a test has no poller to wait for. */
    internal suspend fun refreshForTests() = refreshNow()

    /** True while a metadata keep for this torrent is running — a test waits on it. */
    internal fun isKeeping(infoHash: String): Boolean = infoHash in keeping

    // ------------------------------------------------------------------ public API

    fun addMagnet(text: String, sequentialOverride: Boolean? = null, onDone: (Boolean) -> Unit = {}) {
        awaitLoaded() // no mutation may persist before the saved list loads
        val magnet = MagnetParser.extractMagnet(text.trim()) ?: text.trim()
        val hash = MagnetParser.infoHashHex(magnet)
        if (hash == null) {
            _notices.tryEmit(Strings.t.noticeInvalidMagnet)
            return
        }
        if (entries.any { it.infoHash == hash }) {
            _notices.tryEmit(Strings.t.noticeAlreadyInList)
            return
        }
        val sequential = sequentialOverride ?: prefs.value.sequentialDefault
        scope.launch {
            // The engine may still be starting (or retrying): wait for it, do not fail.
            if (!awaitEngine()) {
                _notices.tryEmit(Strings.t.noticeCouldNotAddMagnet)
                onDone(false)
                return@launch
            }
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
                        entries.add(Entry(h, magnet, listedName(MagnetParser.displayName(magnet)), sequential, System.currentTimeMillis()))
                        unfinishedSeen.add(h) // its finish is news
                        h
                    } else {
                        null
                    }
                }
            }
            if (added == null) {
                // Inside the dispatcher the list is final: a hash that is there is there.
                if (entries.any { it.infoHash == hash }) _notices.tryEmit(Strings.t.noticeAlreadyInList)
                else _notices.tryEmit(Strings.t.noticeCouldNotAddMagnet)
                onDone(false)
            } else {
                persist()
                refreshNow()
                ensureService()
                _notices.tryEmit(Strings.t.noticeAddedFetching)
                onDone(true)
            }
        }
    }

    /**
     * A torrent the user has NOT ok'd yet — see [requestAddMagnet]. [file]
     * is a staged .torrent in cacheDir: consumed on confirm, deleted on cancel.
     */
    data class PendingAdd(
        val magnet: String,
        val name: String,
        val infoHash: String,
        val sequential: Boolean,
        val file: File?,
        /** Host the request came from ("" = another app, or unknown). */
        val sourceHost: String,
        /** A frame of another site on the page asked — not the page the user is looking at. */
        val fromSubframe: Boolean = false,
        /** The user's own tap or share. A page-fired request (a script, a redirect, a frame) is not. */
        val userInitiated: Boolean = false,
        /** Place in the arrival order — [offer]'s bookkeeping; not for display. */
        val seq: Long = 0,
    )

    private val _pendingAdd = MutableStateFlow<PendingAdd?>(null)

    /** Guards take-and-clear of [_pendingAdd] (confirm vs cancel vs a newer request) and everything below. */
    private val pendingAddLock = Any()

    /** Non-null while the torrent-add confirmation is waiting for the user (rendered by AppRoot). */
    val pendingAdd: StateFlow<PendingAdd?> = _pendingAdd.asStateFlow()

    /**
     * Arrival order of add requests. A .torrent is read off the main
     * thread, so an OLDER request can finish reading after a newer one — and
     * used to replace the newer dialog with its own (review finding
     * 2026-11-24).
     */
    private val addRequestCounter = AtomicLong()

    /** Sequence number of the newest request shown — or answered. Guarded by [pendingAddLock]. */
    private var newestShownSeq = 0L

    /**
     * The user's own requests that are not answered yet — shown, or still
     * being fetched/read. While one exists, nothing a page fires may appear
     * in its place (second review): a page could wait for the user to tap a
     * slow .torrent, fire a magnet of its own, and have ITS dialog shown;
     * the user's file, arriving later, was then dropped as "older".
     * Guarded by [pendingAddLock].
     */
    private val openUserRequests = mutableSetOf<Long>()

    /** Hashes the user declined lately, with when — page-fired requests only. */
    private val declinedAt = HashMap<String, Long>()

    /**
     * When page-fired requests were last shown. The dialog is modal over
     * the whole app: a page that keeps re-firing what the user just
     * dismissed — the same magnet, or a volley of fresh ones — must not
     * re-open it with every round. That is a way to lock the user out of
     * their own app (second review). Guarded by [pendingAddLock].
     */
    private val pageFiredShown = ArrayDeque<Long>()
    private var stormNoticeAt = 0L

    /**
     * The page is asking again for a torrent the user declined a moment
     * ago. Only THAT torrent, and only for a fixed [DECLINE_QUIET_MS] after
     * the Cancel: a user who changes their mind gets the dialog again by
     * asking once more after those seconds — the window is not restarted
     * by the requests it swallows, and nothing else the page asks for is
     * held against it (those count against [pageIsFlooding] instead).
     */
    private fun declinedJustNow(hash: String): Boolean = synchronized(pendingAddLock) {
        val at = declinedAt[hash] ?: return false
        if (clock() - at >= DECLINE_QUIET_MS) {
            declinedAt.remove(hash)
            return false
        }
        true
    }

    /** Page-fired requests came too fast of late. Says so once per window. */
    private fun pageIsFlooding(): Boolean = synchronized(pendingAddLock) {
        val now = clock()
        while (pageFiredShown.isNotEmpty() && now - pageFiredShown.first() >= PAGE_FURY_WINDOW_MS) pageFiredShown.removeFirst()
        if (pageFiredShown.size < PAGE_FURY_LIMIT) {
            pageFiredShown.addLast(now)
            return false
        }
        if (stormNoticeAt == 0L || now - stormNoticeAt >= PAGE_FURY_WINDOW_MS) {
            stormNoticeAt = now
            _notices.tryEmit(Strings.t.noticePageAddStorm)
        }
        true
    }

    private fun userRequestEnded(seq: Long) = synchronized(pendingAddLock) { openUserRequests.remove(seq) }

    /**
     * Adds that did NOT come from the user's own hand ask first (fix
     * 2026-11-23): a page navigation, a redirect or another app must not put
     * a torrent in the list unasked — the desktop confirms every time. One
     * request at a time; a newer one replaces the older, so a page firing a
     * volley of magnet redirects produces ONE dialog, not a flood.
     *
     * The agent's add_torrent tool keeps calling [addMagnet] directly
     * (always-YOLO, owner decision 2026-11-16), as does the URL bar — those
     * are the user's own hand. A [userInitiated] request (a tapped link, the
     * links sheet, a share from another app) always gets its dialog and is
     * never displaced by a page; one a page fired on its own is dropped
     * while the user has just declined one, or when the page fires too many.
     */
    fun requestAddMagnet(
        text: String,
        sourceHost: String = "",
        userInitiated: Boolean = false,
        fromSubframe: Boolean = false,
    ) {
        val magnet = MagnetParser.extractMagnet(text.trim()) ?: text.trim()
        val hash = MagnetParser.infoHashHex(magnet)
        if (hash == null) {
            _notices.tryEmit(Strings.t.noticeInvalidMagnet)
            return
        }
        if (entries.any { it.infoHash == hash }) {
            _notices.tryEmit(Strings.t.noticeAlreadyInList)
            return
        }
        val seq = addRequestCounter.incrementAndGet()
        if (!userInitiated && (declinedJustNow(hash) || pageIsFlooding())) return
        offer(
            seq,
            PendingAdd(
                magnet = magnet,
                name = MagnetParser.displayName(magnet),
                infoHash = hash,
                sequential = prefs.value.sequentialDefault,
                file = null,
                sourceHost = sourceHost,
                fromSubframe = fromSubframe,
                userInitiated = userInitiated,
                seq = seq,
            ),
        )
    }

    /** [requestAddMagnet] for a .torrent file already in cacheDir — read and validated off the main thread. */
    fun requestAddTorrentFile(file: File, sourceHost: String = "", userInitiated: Boolean = false) {
        // The place in the order is fixed when the request ARRIVES, not when its read ends.
        val seq = addRequestCounter.incrementAndGet()
        if (userInitiated) synchronized(pendingAddLock) { openUserRequests.add(seq) }
        scope.launch {
            fun done(): Nothing? {
                file.delete()
                userRequestEnded(seq)
                return null
            }
            val info = withContext(Dispatchers.IO) { runCatching { readStagedTorrent(file) }.getOrNull() }
            if (info == null) {
                done()
                _notices.tryEmit(Strings.t.noticeNotTorrent)
                return@launch
            }
            if (entries.any { it.infoHash == info.infoHash }) {
                done()
                _notices.tryEmit(Strings.t.noticeAlreadyInList)
                return@launch
            }
            if (!userInitiated && (declinedJustNow(info.infoHash) || pageIsFlooding())) {
                done()
                return@launch
            }
            offer(
                seq,
                PendingAdd(
                    magnet = "magnet:?xt=urn:btih:${info.infoHash}",
                    name = info.name,
                    infoHash = info.infoHash,
                    sequential = prefs.value.sequentialDefault,
                    file = file,
                    sourceHost = sourceHost,
                    userInitiated = userInitiated,
                    seq = seq,
                ),
            )
        }
    }

    /**
     * Shows [request] unless a NEWER one has been shown — or answered — in
     * the meantime, and unless the user's own request is showing or on its
     * way (a page does not push that aside). One step under the lock, so a
     * confirm or cancel racing it cannot act on half a state. A request
     * that is not shown takes its staged file with it.
     */
    private fun offer(seq: Long, request: PendingAdd) {
        synchronized(pendingAddLock) {
            if (seq < newestShownSeq || (!request.userInitiated && openUserRequests.isNotEmpty())) {
                request.file?.delete()
                openUserRequests.remove(seq)
                return
            }
            // The request being replaced is over, whatever it was.
            _pendingAdd.value?.let { openUserRequests.remove(it.seq) }
            _pendingAdd.value?.file?.delete()
            newestShownSeq = seq
            _pendingAdd.value = request
            if (request.userInitiated) openUserRequests.add(seq)
        }
    }

    /** The user tapped Add. */
    fun confirmPendingAdd() {
        // Taking the request under the lock: a cancel racing this call must
        // not delete a file that is being imported.
        val p = takePendingAdd() ?: return
        val file = p.file
        if (file != null) {
            // The staged file can be gone (a cache sweep, a restore from backup).
            if (file.isFile) addTorrentFile(file) else _notices.tryEmit(Strings.t.noticeCouldNotReadTorrent)
        } else {
            addMagnet(p.magnet, p.sequential)
        }
    }

    /** The user dismissed the confirmation. */
    fun cancelPendingAdd() {
        val p = takePendingAdd() ?: return
        p.file?.delete()
        if (!p.userInitiated) {
            // Declining a page's request starts the quiet time during which
            // the page's own re-asks for THIS torrent are dropped.
            synchronized(pendingAddLock) { declinedAt[p.infoHash] = clock() }
        }
    }

    private fun takePendingAdd(): PendingAdd? = synchronized(pendingAddLock) {
        val p = _pendingAdd.value
        _pendingAdd.value = null
        if (p != null) openUserRequests.remove(p.seq)
        p
    }

    /**
     * Copies a .torrent handed over by another app into cacheDir — at most
     * [MAX_PENDING_TORRENT_BYTES], it used to be unbounded — and asks the
     * user ([requestAddTorrentFile]). Nothing stays behind on failure.
     * One at a time: a foreground app could loop "open with" intents, and
     * each copy is up to the cap.
     */
    fun addTorrentFromUri(uri: Uri) {
        if (!importingShare.compareAndSet(false, true)) return
        scope.launch(Dispatchers.IO) {
            try {
                val tmp = File(context.cacheDir, "import-${UUID.randomUUID()}.torrent")
                val ok = runCatching {
                    context.contentResolver.openInputStream(uri)?.use { src -> copyCapped(src, tmp, MAX_PENDING_TORRENT_BYTES.toLong()) } ?: false
                }.getOrDefault(false)
                if (!ok) {
                    tmp.delete()
                    _notices.tryEmit(Strings.t.noticeCouldNotReadTorrent)
                    return@launch
                }
                // The user picked "open with DeepFlux" in that other app: their own hand.
                requestAddTorrentFile(tmp, userInitiated = true)
            } finally {
                importingShare.set(false)
            }
        }
    }

    private val importingShare = java.util.concurrent.atomic.AtomicBoolean(false)

    /** At most [cap] bytes; false when the source is empty, larger than that, or fails. */
    private fun copyCapped(src: InputStream, dest: File, cap: Long): Boolean {
        dest.outputStream().use { out ->
            val buf = ByteArray(64 * 1024)
            var total = 0L
            while (true) {
                val n = src.read(buf)
                if (n == -1) break
                total += n
                if (total > cap) return false
                out.write(buf, 0, n)
            }
            return total > 0
        }
    }

    /**
     * Add a .torrent already on disk (the in-app browser saves intercepted
     * torrent downloads into cacheDir and hands them here). The file is
     * consumed (deleted) either way. [onDone] says whether the torrent ended
     * up in the list — a caller that marks an item "done" needs to know
     * (RSS used to mark a .torrent seen on the first byte of the file alone).
     */
    fun addTorrentFile(file: File, onDone: (Boolean) -> Unit = {}) {
        awaitLoaded() // no mutation may persist before the saved list loads
        scope.launch {
            val added = importTorrentFile(file)
            onDone(added)
        }
    }

    private suspend fun importTorrentFile(tmp: File): Boolean {
        run {
            if (!awaitEngine()) {
                tmp.delete()
                _notices.tryEmit(Strings.t.noticeCouldNotAddMagnet)
                return false
            }
            val sequential = prefs.value.sequentialDefault
            val outcome = withContext(engineDispatcher) {
                runCatching { engine.addTorrentFile(tmp.absolutePath, saveDir) }.getOrElse { TorrentFileAdd.Failed(it.message) }
            }
            val hash = (outcome as? TorrentFileAdd.Added)?.infoHash
            if (hash != null) {
                // Restart-proof .torrent store (private-tracker fix, 2026-11-16):
                // keep the bytes under filesDir/torrents/<hash>.torrent so start()
                // can re-add WITH metadata — a bare magnet can't recover it when
                // the torrent is PRIVATE (DHT disabled by design; the restored
                // zombie stays "Fetching info" forever).
                // In one move: a copy cut short would be no torrent.
                val dir = File(context.filesDir, "torrents").apply { mkdirs() }
                val saved = File(dir, "$hash.torrent")
                runCatching {
                    val part = File(dir, "$hash.torrent.tmp")
                    tmp.copyTo(part, overwrite = true)
                    if (!part.renameTo(saved)) throw java.io.IOException("could not be moved into place")
                }.onFailure { Log.w(TAG, "could not keep the added .torrent: ${it.message}") }
                unfinishedSeen.add(hash) // its finish is news
            }
            tmp.delete()
            if (hash == null || entries.any { it.infoHash == hash }) {
                if (hash != null) _notices.tryEmit(Strings.t.noticeAlreadyInList)
                else _notices.tryEmit(outcome.refusalNotice())
                return false
            }
            // Persist a hash-magnet so restarts can re-add the torrent (an
            // empty magnetUri silently never came back — zombie rows stuck
            // at 0 peers); start() prefers the saved .torrent file above.
            entries.add(
                Entry(
                    hash,
                    "magnet:?xt=urn:btih:$hash",
                    listedName((outcome as? TorrentFileAdd.Added)?.name).ifBlank { hash },
                    sequential,
                    System.currentTimeMillis(),
                ),
            )
            persist()
            refreshNow()
            ensureService()
            _notices.tryEmit(Strings.t.noticeTorrentAdded)
            return true
        }
    }

    /**
     * Entry point for VIEW (magnet:/content) and SEND intents. Another app
     * handing us a torrent is not the user's own hand in THIS app: the add is
     * confirmed first ([requestAddMagnet] / [requestAddTorrentFile]).
     */
    fun addFromIntent(intent: Intent?) {
        if (intent == null) return
        when (intent.action) {
            Intent.ACTION_VIEW -> {
                val data = intent.data ?: return
                if (data.scheme.equals("magnet", ignoreCase = true)) {
                    // The user chose DeepFlux in that other app: their own hand —
                    // never dropped as a "re-fired" request.
                    requestAddMagnet(data.toString(), userInitiated = true)
                } else {
                    addTorrentFromUri(data)
                }
            }
            Intent.ACTION_SEND -> {
                val text = intent.getStringExtra(Intent.EXTRA_TEXT)
                if (!text.isNullOrBlank() && text.contains("magnet:", ignoreCase = true)) {
                    requestAddMagnet(text, userInitiated = true)
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
        torrentOp(infoHash, Strings.t.noticePaused, savePause = true) { engine.pauseTorrent(it) }
    }

    fun resume(infoHash: String) {
        awaitLoaded() // no mutation may persist before the saved list loads
        pausedHashes.remove(infoHash)
        pauseAllHashes.remove(infoHash) // resumed by hand: no longer "resume all"'s to restore
        publishPauseAll()
        torrentOp(infoHash, Strings.t.noticeResumed, savePause = true) { engine.resumeTorrent(it) }
    }

    /**
     * Hashes [pauseAll] paused — [resumeAll] restores exactly these, never a
     * torrent the user had paused by hand before pressing "Pause all".
     */
    private val pauseAllHashes = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    /** Pause every torrent that is not paused already (the notification's "Pause all"). */
    fun pauseAll() {
        awaitLoaded()
        // Recorded before any engine call, like resumeAll: the service's
        // onTimeout pauses and stops at once, and what it leaves behind must
        // already say "waiting for Resume all".
        val targets = entries.map { it.infoHash }.filter { !pausedHashes.contains(it) }
        for (h in targets) {
            pauseAllHashes.add(h)
            pausedHashes.add(h)
        }
        publishPauseAll()
        scope.launch {
            persist()
            withContext(engineDispatcher) { targets.forEach { runCatching { engine.pauseTorrent(it) } } }
            refreshNow()
        }
    }

    /** True while something is paused by [pauseAll] (drives "Resume all"). */
    fun anyPausedByPauseAll(): Boolean = pauseAllHashes.isNotEmpty()

    private val _pausedByPauseAll = MutableStateFlow(false)

    /** [anyPausedByPauseAll] as a flow, for screens that show "Resume all". */
    val pausedByPauseAll: StateFlow<Boolean> = _pausedByPauseAll.asStateFlow()

    /**
     * "Something is waiting for Resume all" changed. DeepFluxApp wires it to
     * the standing "paused" notice, so the notice also leaves when the last
     * waiting torrent is removed — by the agent, where no screen re-syncs it.
     */
    var onPauseAllChanged: (() -> Unit)? = null

    private fun publishPauseAll() {
        _pausedByPauseAll.value = pauseAllHashes.isNotEmpty()
        onPauseAllChanged?.let { cb -> runCatching { cb() } }
    }

    /** Resume exactly what [pauseAll] paused. */
    fun resumeAll() {
        awaitLoaded()
        // Cleared before any engine call: "is something waiting for Resume
        // all?" is answered from these sets, and a caller that resumes and
        // then asks (the paused notice) must not see the state it just ended.
        val mine = pauseAllHashes.toList()
        for (h in mine) {
            pauseAllHashes.remove(h)
            pausedHashes.remove(h)
        }
        publishPauseAll()
        scope.launch {
            persist()
            withContext(engineDispatcher) { mine.forEach { runCatching { engine.resumeTorrent(it) } } }
            refreshNow()
            ensureService()
        }
    }

    fun remove(infoHash: String) {
        awaitLoaded() // no mutation may persist before the saved list loads
        val entry = entries.firstOrNull { it.infoHash == infoHash } ?: return
        scope.launch {
            withContext(engineDispatcher) {
                runCatching { engine.removeTorrent(infoHash) }
            }
            // By what identifies it, never by the copy taken above: that copy's
            // name may have been replaced meanwhile (a magnet's real name
            // arrived), and a copy that no longer equals its entry used to stay
            // in — and come back at the next start (review, 2026-11-25).
            entries.removeIf { it.infoHash == infoHash && it.addedAt == entry.addedAt }
            pausedHashes.remove(infoHash)
            pauseAllHashes.remove(infoHash)
            copiedHashes.remove(infoHash)
            unfinishedSeen.remove(infoHash)
            publishPauseAll()
            appliedSequential.remove(infoHash)
            runCatching { File(File(context.filesDir, "torrents"), "$infoHash.torrent").delete() }
            persist()
            _torrents.value = _torrents.value.filterNot { it.infoHash == infoHash }
            _notices.tryEmit(Strings.t.noticeRemovedFmt.format(entry.name.ifBlank { entry.infoHash.take(12) }))
        }
    }

    private fun torrentOp(infoHash: String, notice: String, savePause: Boolean = false, op: (String) -> Unit) {
        scope.launch {
            if (savePause) persist()
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

    /**
     * Saves one setting off the UI thread. A save that fails is TOLD,
     * not fatal: the error used to escape the coroutine — on a phone that
     * ends the process (a full disk at the moment of a toggle, or of the
     * chat-history save after every reply). The store keeps handing out
     * the new value until a later save writes everything, so the change
     * is lost if the app goes first — which is why the user hears of it
     * now (measured on this library 2026-11-25).
     */
    internal fun saveSetting(block: suspend () -> Unit) = scope.launch {
        try {
            block()
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.w(TAG, "could not save a setting: ${e.message}")
            _notices.tryEmit(Strings.t.noticeSettingNotSaved)
        }
    }

    fun setWifiOnly(value: Boolean) = saveSetting { settings.setWifiOnly(value) }

    fun setChargingOnly(value: Boolean) = saveSetting { settings.setChargingOnly(value) }

    fun setSequentialDefault(value: Boolean) = saveSetting { settings.setSequentialDefault(value) }

    fun setCustomApiKey(value: String) = saveSetting { settings.setCustomApiKey(value) }

    fun setOpensubtitlesApiKey(value: String) = saveSetting { settings.setOpensubtitlesApiKey(value) }

    fun setHomepage(value: String) = saveSetting { settings.setHomepage(value) }

    fun setUiMode(value: String) = saveSetting { settings.setUiMode(value) }

    fun setUpdatesAutoCheck(value: Boolean) = saveSetting { settings.setUpdatesAutoCheck(value) }

    fun setUpdateSkipVersion(value: String) = saveSetting { settings.setUpdateSkipVersion(value) }

    /** Direct DataStore access for the backup card (plan §13 #6). */
    val appSettings: AppSettings get() = settings

    fun engineVersion(): String = engine.libtorrentVersion()

    /** Tracker announce URLs (agent diagnostics, §13 #10) — engine thread. */
    fun torrentTrackers(infoHash: String): List<String> =
        withContextBlocking { engine.trackerUrls(infoHash) }.orEmpty()

    fun loadChatHistory(): String = settings.loadChatHistory()

    fun setLanguage(chinese: Boolean) =
        saveSetting { settings.setLanguage(if (chinese) "zh" else "en") }

    fun loadLanguageIsChinese(): Boolean = settings.loadLanguage() == "zh"

    /**
     * Settings → "Save location": the user picked the folder finished
     * downloads are copied to. [kept] says whether the system keeps the app's
     * access to it across launches — when it does not, the folder is no use
     * as a destination: the earlier choice stands and the user is told.
     */
    fun pickedFinishedDownloadsFolder(uri: Uri, kept: Boolean) = saveSetting {
        if (kept) settings.setCustomSaveDir(uri.toString()) else _notices.tryEmit(Strings.t.noticeFolderNotKept)
    }

    /** Back to the public Downloads folder for finished downloads. */
    fun clearCustomSaveDir() = saveSetting { settings.clearCustomSaveDir() }

    /**
     * The folder finished downloads are copied to ("" = the public
     * Downloads folder), read where no coroutine is running — the HTTP
     * lane's export thread. It waits for the stored settings, so a
     * download finishing in the first moment after launch is not sent to
     * the default by a setting that has not loaded yet.
     */
    fun finishedDownloadFolder(): String = runCatching {
        kotlinx.coroutines.runBlocking { settings.prefsSnapshot().customSaveDirUri }
    }.onFailure { Log.w(TAG, "could not read the save location: ${it.message}") }.getOrDefault("")

    /** Callback the activity sets to open the SAF folder picker. */
    @Volatile
    var pickFolder: (() -> Unit)? = null

    fun saveChatHistory(json: String) = saveSetting { settings.saveChatHistory(json) }

    // ------------------------------------------------------------------ refresh + gates

    @Volatile
    private var hadActiveTorrents = false

    private suspend fun refreshNow() {
        val snapshot = withContext(engineDispatcher) {
            val gateOk = gatesOk()
            if (lastGateOk == null) {
                // The first look (startup) applies the gate but is NOT a
                // transition. It used to run the "gate opened" path, which
                // starts the foreground service: on every launch, with no
                // transfer and no user gesture — an idle service on every
                // start, burning the background time budget (Android 12+
                // also refuses such a start from the background).
                lastGateOk = gateOk
                if (gateOk) engine.resumeSession() else engine.pauseSession()
            } else if (gateOk != lastGateOk) {
                lastGateOk = gateOk
                if (gateOk) {
                    engine.resumeSession()
                    // Transfers are live again — re-assert the FGS (best effort:
                    // Android 12+ refuses a background start, which is fine while
                    // the app is closed; MainActivity.onStart retries when shown).
                    ensureService()
                } else {
                    engine.pauseSession()
                }
                Log.i(TAG, "network gate -> ${if (gateOk) "open" else "paused (wifi/charging policy)"}")
            }
            if (gateOk) {
                // The HTTP/HLS lane has no watcher of its own: it is told on
                // EVERY poll while the gate is open — not only at a
                // transition. Its worker samples the gate per chunk, so a
                // blip shorter than the poll can pause an item without the
                // poller ever seeing the gate closed; transition-only
                // nudges left such downloads Paused for good. The call is
                // free when nothing waits on the gate.
                onGateOpened?.let { cb -> runCatching { cb() } }
            }
            _gateOpen.value = gateOk
            // Retry sequential application until metadata exists (once per torrent).
            entries.filter { it.sequential && infoHashPending(it.infoHash) }
                .forEach { e -> if (engine.applySequential(e.infoHash)) appliedSequential.add(e.infoHash) }
            retryKeeps(System.currentTimeMillis())
            engine.snapshot(entries.map { it.infoHash })
        }

        val items = snapshot.mapNotNull { (hex, st) ->
            val entry = entries.firstOrNull { it.infoHash == hex } ?: return@mapNotNull null
            st ?: return@mapNotNull null
            entry.toItem(st)
        }
        _torrents.value = items
        watchDownloading(items)

        // The foreground service follows ACTIVITY — never merely the app
        // being launched. It starts when torrents become active, which
        // includes the ones a restart resumes: MainActivity.onStart used to
        // be their only chance and it reads this list before the first
        // snapshot exists, so a cold start with unfinished torrents ran on
        // with no service (second review, 2026-11-24). Edge-triggered: an
        // app with nothing to transfer still never starts one.
        val anyActive = activeOverride?.invoke() ?: items.any { it.isActive }
        if (anyActive != hadActiveTorrents) {
            hadActiveTorrents = anyActive
            if (anyActive) ensureService()
        }
    }

    private fun gatesOk(): Boolean {
        // Public via networkGateOpen() below — shared with the HTTP download
        // engine so both lanes obey the SAME wifi/charging policy.
        gateOverride?.invoke()?.let { return it }
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
        val dnName = magnetUri.substringAfter("dn=", "").substringBefore('&').takeIf { it.isNotBlank() }?.let { listedName(it) }
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
                    val hex = runCatching { alert.handle()?.infoHash()?.toHex()?.lowercase() }.getOrNull()
                    onFinished(hex)
                    scope.launch { refreshNow() }
                    // v0.9: export completed video files to public Downloads
                    exportFinishedFiles(alert, hex)
                }
                is TorrentErrorAlert -> {
                    val name = alertTorrentName(alert)
                    val msg = runCatching { alert.error()?.message }.getOrNull().orEmpty()
                    postEventNotification(
                        EventKind.FAILED,
                        name.ifBlank { context.getString(R.string.notif_generic_torrent) },
                        msg,
                    )
                    scope.launch { refreshNow() }
                }
                is MetadataReceivedAlert -> {
                    val hex = runCatching { alert.handle()?.infoHash()?.toHex()?.lowercase() }.getOrNull()
                    if (hex != null) scope.launch { metadataArrived(hex) } else scope.launch { refreshNow() }
                }
                else -> Unit
            }
        }
    }

    @androidx.annotation.VisibleForTesting
    /** What to tell the user when the engine did not take a .torrent file. */
    private fun TorrentFileAdd.refusalNotice(): String =
        if (this === TorrentFileAdd.HasSymbolicLinks) Strings.t.noticeTorrentHasSymlinksFile
        else if (this is TorrentFileAdd.Failed) Strings.t.noticeCouldNotAddTorrent
        else Strings.t.noticeNotTorrent

    /** Where a torrent's metadata is kept once the engine knows it. */
    private fun torrentFileOf(infoHash: String) = File(File(context.filesDir, "torrents"), "$infoHash.torrent")

    /**
     * A name as it is listed and saved — cleaned exactly as the confirmation
     * shows it ([confirmationName]: it comes from the torrent, i.e. from
     * strangers), so what was agreed to is what the list shows.
     */
    private fun listedName(raw: String?): String = confirmationName(raw.orEmpty())

    /**
     * Gives a torrent its real name — once the engine knows it. Blank
     * names and no-ops are ignored; the list is saved when something changed.
     * An entry is replaced in one step (never an index found earlier and used
     * later), so a Remove running alongside still finds its entry — or the
     * entry is gone, as a whole.
     */
    private fun renameEntry(infoHash: String, name: String?) {
        val clean = listedName(name)
        if (clean.isEmpty()) return
        var changed = false
        entries.replaceAll { if (it.infoHash == infoHash && it.name != clean) { changed = true; it.copy(name = clean) } else it }
        if (changed) persist()
    }

    /**
     * The engine now knows a magnet's files. One that carries a symbolic
     * link goes again, at once (it can point at any file of the app's
     * private storage). Otherwise two things are kept that used to be lost:
     * the metadata itself — written as a .torrent, so the next start needs
     * no peer to hand it back (a finished torrent came back as "Fetching
     * info", with nothing to play, whenever nobody was there to serve it) —
     * and the torrent's own name. Internal for the tests: the alert itself
     * cannot be built on the JVM, this is what it comes down to.
     */
    internal suspend fun metadataArrived(infoHash: String) {
        // The alert and a retry can meet here; one of the two is enough.
        if (!keeping.add(infoHash)) return
        keepMetadata(infoHash)
    }

    /** The keep itself. Callers hold the torrent in [keeping] for its duration and release it. */
    private suspend fun keepMetadata(infoHash: String) {
        try {
            val linked = withContext(engineDispatcher) {
                runCatching { engine.hasSymlinkEntries(infoHash) }.getOrDefault(false)
            }
            if (linked) {
                removeSymlinkTorrent(infoHash)
                return
            }
            val saved = torrentFileOf(infoHash)
            val name = withContext(engineDispatcher) {
                if (!saved.isFile) {
                    val keptNow = runCatching { engine.saveTorrentFile(infoHash, saved) }.getOrDefault(false)
                    if (!keptNow) {
                        // (The engine says why.) Not tried again at the very next poll: the
                        // disk rarely has room again a second later.
                        keepNextAt[infoHash] = System.currentTimeMillis() + keepRetryEveryMs
                    }
                }
                runCatching { engine.torrentName(infoHash) }.getOrNull()
            }
            renameEntry(infoHash, name)
            refreshNow()
        } finally {
            keeping.remove(infoHash)
        }
    }

    /**
     * Keeps, again, the metadata of torrents that have it and no kept file —
     * a keep fails when the disk is full at that moment. At most
     * [keepTriesMax] times per torrent and process, [keepRetryEveryMs] apart,
     * each on a refresh. Runs on the engine thread.
     */
    private fun retryKeeps(now: Long) {
        entries.filter { e -> !torrentFileOf(e.infoHash).isFile }.forEach { e ->
            val tries = keepTries[e.infoHash] ?: 0
            if (tries >= keepTriesMax || now < (keepNextAt[e.infoHash] ?: 0L)) return@forEach
            // Reserved here, where it is launched — not when the coroutine gets going:
            // two refreshes a moment apart otherwise launched two, and one of the
            // tries was spent on nothing.
            if (!keeping.add(e.infoHash)) return@forEach
            val hasMetadata = runCatching { engine.torrentName(e.infoHash) != null }.getOrDefault(false)
            if (!hasMetadata) {
                keeping.remove(e.infoHash)
                return@forEach // the metadata is not here yet
            }
            keepTries[e.infoHash] = tries + 1
            keepNextAt[e.infoHash] = now + keepRetryEveryMs
            scope.launch { keepMetadata(e.infoHash) }
        }
    }

    /**
     * What a poll found DOWNLOADING is watched: its finish is news — and its
     * files are no longer "copied out", they are being written anew. Only that
     * state counts as watched, not "bytes still to get": a complete torrent
     * has its files re-checked at each start, reads that way until the check
     * is done, and was announced on every launch (emulator, 2026-11-25).
     * Internal for the tests: the engine's status cannot be built on the JVM.
     */
    internal fun watchDownloading(items: List<TorrentItem>) {
        items.forEach {
            if (it.state == TorrentState.DOWNLOADING) {
                unfinishedSeen.add(it.infoHash)
                copiedHashes.remove(it.infoHash)
            }
        }
    }

    /**
     * The engine says a torrent finished. News only when this process watched
     * it download — added here, or seen downloading. The engine reports
     * "finished" again for every complete torrent at each start (emulator,
     * 2026-11-25); that used to be a "Download finished" on every launch.
     * Internal for the tests: the alert itself cannot be built on the JVM.
     */
    internal fun onFinished(infoHash: String?) {
        if (infoHash == null || !unfinishedSeen.remove(infoHash)) return
        val name = entries.firstOrNull { it.infoHash == infoHash }?.name.orEmpty()
        postEventNotification(EventKind.FINISHED, name.ifBlank { context.getString(R.string.notif_generic_torrent) })
    }

    internal suspend fun removeSymlinkTorrent(infoHash: String) {
        withContext(engineDispatcher) { runCatching { engine.removeTorrent(infoHash) } }
        // By what identifies it, never by a copy taken earlier — see remove().
        if (entries.none { it.infoHash == infoHash }) return
        entries.removeIf { it.infoHash == infoHash }
        pausedHashes.remove(infoHash)
        pauseAllHashes.remove(infoHash)
        copiedHashes.remove(infoHash)
        unfinishedSeen.remove(infoHash)
        publishPauseAll()
        persist()
        _torrents.value = _torrents.value.filterNot { it.infoHash == infoHash }
        Log.w(TAG, "torrent removed: it contains symbolic links")
        _notices.tryEmit(Strings.t.noticeTorrentHasSymlinks)
    }

    /**
     * Copies a finished torrent's files out for the user — once: every file
     * into the folder chosen in Settings, or — with none chosen (or one the
     * app can no longer open) — the video files into the public Downloads
     * collection ([space.deepflux.android.dlmgr.FinishedExport.isCopied]).
     *
     * The engine reports "finished" again for every complete torrent at each
     * start, so this is asked then too: [copiedHashes] is what tells the first
     * time from a re-run (which added a numbered copy in Downloads at every
     * launch). A run with any failure is not marked, and so is finished at
     * the next start.
     */
    private fun exportFinishedFiles(alert: org.libtorrent4j.alerts.TorrentFinishedAlert, infoHash: String?) {
        if (infoHash == null || !needsCopy(infoHash) || !copyingHashes.add(infoHash)) return
        scope.launch(Dispatchers.IO) {
            var queued = false
            try {
                runCatching {
                    // libtorrent is touched on its own dispatcher only. The handle is read
                    // after the alert's callback returned (as it always was): null once it
                    // is gone — the torrent was removed meanwhile, as a rule.
                    val paths = kotlinx.coroutines.withContext(engineDispatcher) {
                        runCatching {
                            val info = alert.handle().torrentFile() ?: return@runCatching null
                            val files = info.files()
                            (0 until files.numFiles()).mapNotNull { files.filePath(it) }
                        }.getOrNull()
                    }
                    // The pool runs the copy in its own good time: the torrent stays marked
                    // "being copied" until the copy itself is over — not until it is queued.
                    // (Released at the queueing, a second "finished" a moment later started
                    // a second run before the first could say it was done.)
                    queued = true
                    space.deepflux.android.dlmgr.FinishedExport.submit {
                        try {
                            copyFinishedOnce(infoHash, paths)
                        } finally {
                            copyingHashes.remove(infoHash)
                        }
                    }
                }.onFailure { Log.w(TAG, "export failed: " + it.message) }
            } finally {
                if (!queued) copyingHashes.remove(infoHash)
            }
        }
    }

    /**
     * One copy run for a finished torrent, on the export pool. [paths] as the
     * torrent names its files; null when they could not be listed — nothing is
     * marked then, and the next start asks again. True once the torrent is done
     * being copied (marked so that it is never asked again).
     */
    internal fun copyFinishedOnce(
        infoHash: String,
        paths: List<String>?,
        copyOne: (String, String, space.deepflux.android.dlmgr.FinishedExport.Run) -> space.deepflux.android.dlmgr.FinishedExport.Result? =
            { path, folder, run -> exportTorrentFile(saveDir, path, folder, run) },
    ): Boolean {
        if (paths == null) {
            Log.w(TAG, "the files of a finished torrent could not be listed; the copy is tried again at the next start")
            return false
        }
        if (exportFinishedFiles(saveDir, paths, copyOne) != 0) return false
        markCopied(infoHash)
        return true
    }

    /** True while a finished torrent's files still have to be copied out for the user. */
    internal fun needsCopy(infoHash: String): Boolean = infoHash !in copiedHashes

    /** The files were copied out (or there was nothing to copy): never again for this torrent. */
    internal fun markCopied(infoHash: String) {
        if (copiedHashes.add(infoHash)) persist()
    }

    /**
     * Copies the files a finished torrent lists ([paths], as the torrent
     * names them, inside [downloadFolder]) out for the user, and returns how
     * many copies failed — the user is told about those, once. One file's
     * trouble never stops the others. Synchronous: the app runs it on the
     * export pool, a test on its own thread. [copyOne] is the per-file copy
     * ([exportTorrentFile]); a test can stand in for it.
     */
    internal fun exportFinishedFiles(
        downloadFolder: File,
        paths: List<String>,
        copyOne: (String, String, space.deepflux.android.dlmgr.FinishedExport.Run) -> space.deepflux.android.dlmgr.FinishedExport.Result? =
            { path, folder, run -> exportTorrentFile(downloadFolder, path, folder, run) },
    ): Int {
        val folder = space.deepflux.android.dlmgr.FinishedExport.effectiveFolder(context, finishedDownloadFolder())
        val run = space.deepflux.android.dlmgr.FinishedExport.Run()
        var failed = 0
        for (path in paths) {
            // A copy that blew up is a file not copied, like a reported failure.
            val outcome = try {
                copyOne(path, folder, run)
            } catch (e: Exception) {
                Log.w(TAG, "export failed for a file of a torrent: ${e.message}")
                space.deepflux.android.dlmgr.FinishedExport.Result.Failed("the copy did not run")
            }
            if (outcome is space.deepflux.android.dlmgr.FinishedExport.Result.Failed) failed++
        }
        if (failed > 0) {
            _notices.tryEmit(Strings.t.noticeExportFailedCountFmt.format(failed))
            // Told outside the app too: a snackbar is seen only while it is open.
            postEventNotification(EventKind.COPY_FAILED, context.resources.getQuantityString(R.plurals.notif_files_count, failed, failed))
        }
        return failed
    }

    /**
     * One file of a finished torrent → a copy for the user, placed where
     * [relativePath] puts it inside the torrent ("Show/Season 1/ep1.mkv").
     * Returns null when there is nothing to copy; else what the copy came to.
     *
     * Only a real file of the download folder is copied out. A symbolic link
     * inside the folder — a hostile torrent can plant one — would hand
     * whatever it points at (any file of this app's private storage) to the
     * user's folder. Such torrents are refused on add; this is the second
     * lock on the same door.
     */
    internal fun exportTorrentFile(
        downloadFolder: File,
        relativePath: String,
        folderUri: String,
        run: space.deepflux.android.dlmgr.FinishedExport.Run? = null,
    ): space.deepflux.android.dlmgr.FinishedExport.Result? {
        if (!space.deepflux.android.dlmgr.FinishedExport.isCopied(relativePath, folderUri)) return null
        val file = File(downloadFolder, relativePath)
        if (java.nio.file.Files.isSymbolicLink(file.toPath())) {
            Log.w(TAG, "export skipped a symbolic link: $relativePath")
            return null
        }
        val root = downloadFolder.canonicalFile
        val canonical = runCatching { file.canonicalFile }.getOrNull() ?: return null
        if (canonical.parentFile != root && !canonical.path.startsWith(root.path + File.separator)) return null
        if (!file.exists()) return null
        // Into the folder chosen in Settings, or the public Downloads
        // folder when none is; a torrent's own sub-folders are kept.
        // The folder was decided for the whole torrent (effectiveFolder): the
        // copy is not re-routed behind its back, and the system is not asked
        // about the permission again for every file — a folder that cannot be
        // written simply fails, which is counted and told.
        return space.deepflux.android.dlmgr.FinishedExport.export(
            context, file, folderUri, relativePath = relativePath, access = { true }, run = run, fallbackToDownloads = false,
        )
    }

    /** libtorrent4j alerts carry no torrentName() — read it from the handle's status, guarded. */
    private fun alertTorrentName(alert: Alert<*>): String =
        runCatching {
            (alert as? org.libtorrent4j.alerts.TorrentAlert<*>)?.handle()?.status()?.name()
        }.getOrNull().orEmpty()

    /** What a download-event notification is about. */
    enum class EventKind { FINISHED, FAILED, COPY_FAILED }

    /**
     * "Download finished / error / not copied to your folder" notification
     * for either lane. Worded in the SYSTEM locale (it is read outside the
     * app) and tapping it opens the Downloads tab — it used to land on the
     * Agent tab.
     */
    fun postEventNotification(kind: EventKind, name: String, detail: String = "") {
        // Plain conditionals, not `when (kind)`: a `when` over an enum makes
        // Kotlin build ONE lookup table for every enum this class switches
        // on — and this class switches on a libtorrent4j enum, whose
        // initialization loads the native library. The JVM tests (no native
        // library) could then never reach this function.
        val titleRes = if (kind == EventKind.FINISHED) {
            R.string.notif_finished_title
        } else if (kind == EventKind.COPY_FAILED) {
            // A download finished, but its copy for the user did not.
            R.string.notif_copy_failed_title
        } else {
            R.string.notif_failed_title
        }
        val title = context.getString(titleRes)
        val text = if (detail.isBlank()) name else context.getString(R.string.notif_error_fmt, name, detail)
        notifyEvent(title, text)
    }

    // Android's background time limit is told outside the app by the standing
    // "paused" notice (TorrentService.syncPausedNotice, wording bySystemLimit).

    private fun notifyEvent(title: String, text: String) {
        val intent = PendingIntent.getActivity(
            context,
            0,
            Intent(context, MainActivity::class.java)
                .putExtra(EXTRA_OPEN_TAB, TAB_DOWNLOADS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
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
                val hash = o.getString("h")
                // The pause is the user's: a hand pause and a "Pause all" pause both survive a restart.
                if (o.optBoolean("p", false)) pausedHashes.add(hash)
                if (o.optBoolean("pa", false)) pauseAllHashes.add(hash)
                // Its files were copied out for the user; "finished" is reported again at each
                // start. A list saved before the copy was remembered (1.4.13 and earlier) says
                // nothing about it: what is in the user's folder is left as it is — copied
                // again at the first start, a deleted copy came back. A torrent that is still
                // downloading is taken off this the moment a poll sees it so.
                if (!o.has("c") || o.optBoolean("c", false)) copiedHashes.add(hash)
                entries.add(
                    Entry(
                        infoHash = hash,
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
        publishPauseAll()
    }

    /**
     * Serialises [persist]. Two coroutines saving at once — two adds in a
     * row, an add and a pause — used to share the one tmp file, and
     * whichever finished last could put its OLDER, shorter list on disk: a
     * torrent added a moment earlier was gone after a restart (found
     * 2026-11-24 through a flaky test). Each writer now saves the list as it
     * is when ITS turn comes.
     */
    private val persistLock = Any()

    /** Atomic (temp + move), mirroring the desktop config.to_file lesson. */
    private fun persist() = synchronized(persistLock) {
        // Never save before the saved list is in: entries not read yet would
        // be written out of existence. The wait is bounded, and a save that
        // could not wait is skipped — a stale file beats an empty one.
        if (!loadedLatch.await(LOAD_WAIT_MS, TimeUnit.MILLISECONDS)) {
            Log.w(TAG, "state persist skipped: the saved list is still being read")
            return@synchronized
        }
        runCatching {
            val arr = JSONArray()
            entries.forEach { e ->
                arr.put(
                    JSONObject()
                        .put("h", e.infoHash)
                        .put("m", e.magnetUri)
                        .put("n", e.name)
                        .put("s", e.sequential)
                        .put("a", e.addedAt)
                        .put("p", pausedHashes.contains(e.infoHash))
                        .put("pa", pauseAllHashes.contains(e.infoHash))
                        .put("c", copiedHashes.contains(e.infoHash)),
                )
            }
            val tmp = File(context.filesDir, "torrents_state.json.tmp")
            tmp.writeText(arr.toString())
            moveIntoPlace(tmp, stateFile)
        }.onFailure { Log.w(TAG, "state persist failed: ${it.message}") }
    }

    private val moveAttempts = 8
    private val moveRetryMs = 15L

    /**
     * The move that makes a save atomic — tried a few times. On some file systems
     * (Windows) it is refused while anything else has the target open, and a save
     * dropped for that moment is a change lost: a torrent's real name was seen to
     * stay unsaved in a test that read this file in a tight loop (2026-11-25). A
     * refusal that stays is still an error, after about a tenth of a second.
     */
    private fun moveIntoPlace(from: File, to: File) {
        var refusal: java.io.IOException? = null
        repeat(moveAttempts) { attempt ->
            try {
                Files.move(from.toPath(), to.toPath(), StandardCopyOption.REPLACE_EXISTING)
                return
            } catch (e: java.io.IOException) {
                refusal = e
                if (attempt < moveAttempts - 1) Thread.sleep(moveRetryMs)
            }
        }
        throw refusal ?: java.io.IOException("$from could not be moved into place")
    }

    companion object {
        private const val TAG = "TorrentRepository"
        const val POLL_MS = 1_000L

        /** A .torrent above this size is not read for the confirmation (the engine's own cap). */
        const val MAX_PENDING_TORRENT_BYTES = 32 * 1024 * 1024

        /** Intent extra (on MainActivity) naming the main tab to open — `Screen.name`. */
        const val EXTRA_OPEN_TAB = "open_tab"

        /** Intent extra (on MainActivity) naming a one-shot action — [ACTION_RESUME_ALL]. */
        const val EXTRA_ACTION = "action"

        /** Value of [EXTRA_ACTION]: the notification's "Resume all" button. */
        const val ACTION_RESUME_ALL = "resume_all"

        /**
         * Intent extra (on MainActivity) proving an action came from OUR
         * notification: the per-process secret of TorrentService.notificationToken.
         */
        const val EXTRA_NOTIFICATION_TOKEN = "notification_token"

        /** [Screen.DOWNLOADS]'s name — what a download notification opens. */
        const val TAB_DOWNLOADS = "DOWNLOADS"

        /** Engine start: this many tries, spaced [ENGINE_START_RETRY_MS] x attempt. */
        private const val ENGINE_START_ATTEMPTS = 4
        private const val ENGINE_START_RETRY_MS = 1_500L

        /** How long a save may wait for the saved list to be read. */
        private const val LOAD_WAIT_MS = 30_000L

        /** The .torrent staged behind a confirmation: present, within the size cap, and readable as bencode. */
        private fun readStagedTorrentCapped(file: File): TorrentMeta.TorrentInfo? {
            if (!file.isFile || file.length() > MAX_PENDING_TORRENT_BYTES) return null
            return runCatching { TorrentMeta.readTorrent(file.readBytes()) }.getOrNull()
        }

        /** Zero-width marks and bidi overrides: invisible, so they are removed outright — a word stays one word. */
        private val INVISIBLE_MARKS = Regex("[\\u200B-\\u200F\\u202A-\\u202E\\u2066-\\u2069\\uFEFF]")

        /** Control characters: line breaks and tabs become a space. */
        private val CONTROL_CHARS = Regex("\\p{Cntrl}")

        private val ANY_SPACE = Regex("\\s+")

        /**
         * A torrent's name as the confirmation shows it. Names come from the
         * web: line breaks would push the buttons out of the dialog, and a
         * bidi override can make a name read as something else
         * ("report\u202Eexe.txt" displays as if it started with "exe").
         */
        fun confirmationName(raw: String): String =
            raw.replace(INVISIBLE_MARKS, "").replace(CONTROL_CHARS, " ").replace(ANY_SPACE, " ").trim().take(200)

        /** A staged .torrent this old at startup belongs to a confirmation that never came. */
        private const val STAGED_SWEEP_AGE_MS = 10 * 60 * 1000L

        /** File-name prefixes of the .torrent files staged for a confirmation (browser, another app, RSS). */
        private val STAGED_PREFIXES = listOf("import-", "web-", "rss-")

        /** After "Cancel", a page's own re-requests are dropped until this much time passes without one. */
        private const val DECLINE_QUIET_MS = 5_000L

        /**
         * At most this many page-fired add requests per window are shown at
         * all — a page cycling through fresh magnets must not keep the
         * modal dialog up, whatever the user answers.
         */
        private const val PAGE_FURY_LIMIT = 3
        private const val PAGE_FURY_WINDOW_MS = 60_000L
    }
}
