package space.deepflux.android.dlmgr

import android.content.Context
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import space.deepflux.android.agent.PublicUrls
import space.deepflux.android.ui.Strings
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.RandomAccessFile
import java.net.URL
import java.net.URLDecoder
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

enum class HttpState { QUEUED, RUNNING, PAUSED, DONE, ERROR }

/**
 * One HTTP download. `totalBytes < 0` = unknown (server sent no length);
 * progress is then indeterminate — except for HLS streams, whose progress
 * is counted in segments.
 */
data class HttpDownload(
    val id: String,
    val url: String,
    val fileName: String,
    val headers: Map<String, String> = emptyMap(),
    val totalBytes: Long = -1,
    val doneBytes: Long = 0,
    val state: HttpState = HttpState.QUEUED,
    val speed: Long = 0,
    val error: String = "",
    /** An HLS (.m3u8) stream: fetched segment by segment into one file. */
    val hls: Boolean = false,
    val segmentsDone: Int = 0,
    val segmentsTotal: Int = 0,
    /**
     * Added through the agent (`download_url`): EVERY address this download
     * touches — each redirect hop, every HLS playlist / key / segment URL —
     * must be a public internet address (PublicUrls). A public server can
     * otherwise 302 the phone into its own network. User- and browser-added
     * downloads are the user's own choice and are not restricted.
     */
    val publicOnly: Boolean = false,
    /**
     * Paused by the NETWORK GATE, not by the user: goes back to the queue
     * when the gate reopens. Persisted, so it survives the process dying
     * (routine on Android) — it used to be a memory-only id set.
     */
    val gatePaused: Boolean = false,
    /** Paused by "Pause all": "Resume all" restores exactly these. Persisted. */
    val pauseAll: Boolean = false,
) {
    val progress: Float
        get() = when {
            segmentsTotal > 0 -> (segmentsDone.toFloat() / segmentsTotal).coerceIn(0f, 1f)
            totalBytes > 0 -> (doneBytes.toFloat() / totalBytes).coerceIn(0f, 1f)
            else -> 0f
        }

    /** True when [progress] is meaningful (drives determinate vs. busy bars). */
    val hasProgress: Boolean get() = segmentsTotal > 0 || totalBytes > 0
}

/**
 * Plain-HTTP download engine (ANDROID_STUDY.md §12 Phase A1) — the Android
 * twin of the desktop dlmgr: queued workers, pause/resume/retry, byte-Range
 * resume through a `.part` sidecar, and completion export to the public
 * Downloads collection. The FOUNDATION the browser's file downloads and the
 * video grabber (A2/A3) land on.
 *
 * Robustness lessons baked in (2026-11-14): state is persisted on every
 * lifecycle transition AND periodically mid-transfer — NOT only at shutdown
 * (the torrent engine lost its list to an empty state file after a hard
 * kill the same day); resume truth is the `.part` length on disk, not the
 * persisted byte count.
 */
class HttpDownloadRepository(
    private val context: Context,
    private val saveDir: File,
    /** Shared network gate (wifi/charging policy) — same policy as torrents. */
    private val gateOk: () -> Boolean,
    /** Snackbar/notice channel — same sink the torrent repository uses. */
    private val notify: (String) -> Unit = {},
    /**
     * "Not a public internet address" check for agent-added downloads
     * ([HttpDownload.publicOnly]) — null = allowed. Injectable so a test
     * can play "public start URL, LAN redirect" against a loopback server.
     */
    private val urlPolicy: (String) -> String? = { PublicUrls.rejection(it) },
    /**
     * The folder finished files are copied to ("" = the public Downloads
     * folder) — the user's choice in Settings → "Save location".
     */
    private val exportFolder: () -> String = { "" },
) {

    private val lock = Any()

    /** A download finished (file promoted + exported). Wired to a notification by DeepFluxApp. */
    var onFinished: ((String) -> Unit)? = null

    /** A finished download could not be copied out for the user. Wired to a notification by DeepFluxApp. */
    var onCopyFailed: ((String) -> Unit)? = null

    /** A download gave up (retries exhausted / permanent failure). Wired to a notification by DeepFluxApp. */
    var onFailed: ((fileName: String, reason: String) -> Unit)? = null

    private val _items = MutableStateFlow<List<HttpDownload>>(emptyList())
    val items: StateFlow<List<HttpDownload>> = _items.asStateFlow()

    private class Control {
        @Volatile var pauseRequested = false
        @Volatile var cancelRequested = false
    }

    /** A failure retrying cannot fix (HTTP 4xx other than 408/429). */
    private class PermanentFailure(message: String) : IOException(message)

    private val controls = ConcurrentHashMap<String, Control>()

    /**
     * Ids with a LIVE worker thread (guarded by [lock]). A download is never
     * given a second worker while the first is still unwinding: pause then
     * resume on a slow link used to start worker #2 while #1 was blocked in
     * a socket read, and two writers on one `.part` ended in "could not move
     * .part into place" (the 1 GB failure of 2026-11-14).
     */
    private val activeWorkers = HashSet<String>()

    private val pool = Executors.newFixedThreadPool(MAX_ACTIVE) { r ->
        Thread(r, "http-dl").apply { isDaemon = true }
    }

    private val stateFile: File get() = File(context.filesDir, "http_downloads.json")

    /**
     * Opens once the saved list has been read. No mutation may run before
     * that: an add() that persisted ahead of the load would overwrite — and
     * so erase — every saved entry.
     */
    private val loadedLatch = java.util.concurrent.CountDownLatch(1)

    private fun awaitLoaded() {
        loadedLatch.await(3, java.util.concurrent.TimeUnit.SECONDS)
    }

    init {
        saveDir.mkdirs()
        Thread({
            try {
                load()
            } finally {
                loadedLatch.countDown()
            }
        }, "http-dl-load").apply { isDaemon = true }.start()
    }

    // ------------------------------------------------------------------ public API

    /**
     * Enqueue a download. Returns the item, or null for a non-http(s) URL.
     * `headers` ride every request incl. resumed ones (the grabber passes
     * Referer/Cookie/UA; they persist across restarts). `hls` marks an HLS
     * stream; a URL ending in .m3u8 is recognised without it.
     *
     * [fileName] is whatever the caller was told the file is called — an
     * agent tool argument, a Content-Disposition header, a page title — so it
     * is untrusted: it is reduced to a plain file name here, once, for every
     * caller (see [safeFileName]).
     */
    fun add(
        url: String,
        headers: Map<String, String> = emptyMap(),
        fileName: String? = null,
        hls: Boolean = false,
        publicOnly: Boolean = false,
    ): HttpDownload? {
        val trimmed = url.trim()
        if (!trimmed.startsWith("http://", true) && !trimmed.startsWith("https://", true)) return null
        awaitLoaded()
        synchronized(lock) {
            val existing = _items.value.find { it.url == trimmed && it.state != HttpState.ERROR }
            if (existing != null) {
                notify(Strings.t.noticeAlreadyInList)
                return existing
            }
            val item = HttpDownload(
                id = newId(),
                url = trimmed,
                fileName = dedupe(safeFileName(fileName ?: "").ifBlank { nameFromUrl(trimmed) }),
                headers = headers,
                hls = hls || trimmed.substringBefore('?').endsWith(".m3u8", ignoreCase = true),
                publicOnly = publicOnly,
            )
            _items.value = _items.value + item
            persistLocked()
            scheduleLocked()
            return item
        }
    }

    fun pause(id: String) {
        awaitLoaded()
        synchronized(lock) {
            updateLocked(id) {
                when {
                    it.state == HttpState.RUNNING || it.state == HttpState.QUEUED ->
                        it.copy(state = HttpState.PAUSED, speed = 0, gatePaused = false, pauseAll = false)
                    // A pause is the user's from now on: the gate reopening
                    // must not undo it and "Resume all" no longer owns it.
                    it.gatePaused || it.pauseAll -> it.copy(gatePaused = false, pauseAll = false)
                    else -> it
                }
            }
            controls[id]?.pauseRequested = true
            persistLocked()
        }
    }

    fun resume(id: String) {
        awaitLoaded()
        synchronized(lock) {
            updateLocked(id) {
                if (it.state == HttpState.PAUSED || it.state == HttpState.ERROR)
                    it.copy(state = HttpState.QUEUED, error = "", speed = 0, gatePaused = false, pauseAll = false) else it
            }
            // Deliberately NOT clearing the old worker's pause flag: it must
            // still unwind. The re-queued item gets a fresh worker (and a
            // fresh Control) from scheduleLocked once the old one is gone.
            persistLocked()
            scheduleLocked()
        }
    }

    /** Pause every running/queued download (the notification's "Pause all"). */
    fun pauseAll() {
        awaitLoaded()
        synchronized(lock) {
            for (item in _items.value) {
                val live = item.state == HttpState.RUNNING || item.state == HttpState.QUEUED
                // A download the GATE paused is waiting too: left alone, it
                // came back with the next Wi-Fi reconnect despite "Pause all".
                if (!live && !item.gatePaused) continue
                updateLocked(item.id) {
                    it.copy(state = HttpState.PAUSED, speed = 0, gatePaused = false, pauseAll = true)
                }
                controls[item.id]?.pauseRequested = true
            }
            persistLocked()
        }
    }

    /** True while something is paused by [pauseAll] (drives "Resume all"). Lock-free: read from any thread. */
    fun anyPausedByPauseAll(): Boolean = _items.value.any { it.pauseAll }

    /** Resume exactly what [pauseAll] paused. */
    fun resumeAll() {
        awaitLoaded()
        synchronized(lock) {
            for (item in _items.value.filter { it.pauseAll }) {
                updateLocked(item.id) {
                    it.copy(
                        pauseAll = false,
                        // Resumed by hand meanwhile: it already moved on.
                        state = if (it.state == HttpState.PAUSED) HttpState.QUEUED else it.state,
                        speed = 0,
                    )
                }
            }
            persistLocked()
            scheduleLocked()
        }
    }

    /** True while something waits for the network gate. Lock-free and cheap: the torrent lane's poll asks every second. */
    fun hasGatePaused(): Boolean = _items.value.any { it.gatePaused }

    /**
     * The network gate opened again (Wi-Fi back, charger attached — the
     * torrent lane's gate watcher calls this). Everything the GATE paused
     * returns to the queue, and downloads added while the gate was closed
     * (they sat Queued because nothing may start behind a closed gate)
     * finally start.
     *
     * Called on every poll while the gate is open, not only on a transition:
     * a gate blip shorter than the poll interval used to leave downloads
     * paused with nothing left to notice the change.
     */
    fun onGateReopened() {
        if (!hasGatePaused()) return
        awaitLoaded()
        if (!gateOk()) return
        synchronized(lock) {
            for (item in _items.value.filter { it.gatePaused }) {
                updateLocked(item.id) {
                    it.copy(
                        gatePaused = false,
                        // Queued behind the closed gate, or paused by it
                        // mid-transfer. Resumed by hand meanwhile: it already moved on.
                        state = if (it.state == HttpState.PAUSED || it.state == HttpState.QUEUED) HttpState.QUEUED else it.state,
                        speed = 0,
                    )
                }
            }
            persistLocked()
            scheduleLocked()
        }
    }

    /** Remove from the list. A finished file is KEPT (torrent semantics); the `.part` temp goes. */
    fun remove(id: String) {
        awaitLoaded()
        val item = synchronized(lock) {
            val it0 = _items.value.find { it.id == id } ?: return
            controls[id]?.cancelRequested = true
            _items.value = _items.value.filterNot { it.id == id }
            persistLocked()
            it0
        }
        if (item.state != HttpState.DONE) {
            File(saveDir, item.fileName + PART_SUFFIX).delete()
            File(saveDir, item.fileName + PART_SUFFIX + HLS_SIDECAR).delete()
        }
    }

    fun shutdown() {
        synchronized(lock) {
            _items.value = _items.value.map {
                if (it.state == HttpState.RUNNING) it.copy(state = HttpState.PAUSED, speed = 0) else it
            }
            controls.values.forEach { it.pauseRequested = true }
            persistLocked()
        }
    }

    // ------------------------------------------------------------------ scheduling

    private fun scheduleLocked() {
        var slots = MAX_ACTIVE - activeWorkers.size
        if (slots <= 0) return
        if (!gateOk()) {
            // Nothing starts behind a closed gate. Remember that these wait on
            // the GATE — not on a free worker slot — so the gate watcher's
            // nudge (hasGatePaused) knows to look here when it reopens.
            for (item in _items.value) {
                if (item.state == HttpState.QUEUED && !item.gatePaused) {
                    updateLocked(item.id) { it.copy(gatePaused = true) }
                }
            }
            return
        }
        var started = 0
        for (item in _items.value) {
            if (slots <= 0) break
            if (item.state != HttpState.QUEUED) continue
            // Its previous worker is still unwinding (pause → quick resume):
            // that worker's exit re-runs this scheduler and picks it up then.
            if (item.id in activeWorkers) continue
            updateLocked(item.id) { it.copy(state = HttpState.RUNNING, speed = 0, gatePaused = false) }
            val ctl = Control()
            controls[item.id] = ctl
            activeWorkers.add(item.id)
            pool.execute { runWorker(item.id, ctl) }
            slots--
            started++
        }
        // The first RUNNING item is what makes this lane "active", and the
        // foreground service starts on that edge. Every caller persists
        // BEFORE scheduling, so the next look used to be a worker's 5 s
        // tick — too late for a user who leaves the app within seconds of
        // starting a download (Android 12+ refuses the start from the
        // background; second review, 2026-11-24).
        if (started > 0) checkActive()
    }

    // ------------------------------------------------------------------ worker

    private fun runWorker(id: String, ctl: Control) {
        Log.i(TAG, "worker start $id")
        try {
            var attempt = 0
            while (attempt < MAX_ATTEMPTS) {
                val snapshot = _items.value.find { it.id == id } ?: return // removed
                if (ctl.cancelRequested) return
                if (snapshot.state != HttpState.RUNNING) return // paused via UI
                try {
                    transfer(id, ctl)
                    return // finished (DONE, paused, or error already recorded)
                } catch (e: Exception) {
                    if (ctl.cancelRequested) return
                    if (ctl.pauseRequested) return
                    attempt++
                    if (e is PermanentFailure || attempt >= MAX_ATTEMPTS) {
                        val reason = (e.message ?: "network error").take(160)
                        update(id) {
                            it.copy(state = HttpState.ERROR, error = reason, speed = 0)
                        }
                        persist()
                        notify(Strings.t.noticeDownloadFailedFmt.format(snapshot.fileName))
                        // A notification too: the snackbar is only seen while
                        // the app is open, and downloads usually run while it is not.
                        onFailed?.let { cb -> runCatching { cb(snapshot.fileName, reason) } }
                        Log.w(
                            TAG,
                            "download failed (${if (e is PermanentFailure) "not retryable" else "after $attempt attempts"}): ${e.message}"
                        )
                        return
                    }
                    Log.i(TAG, "attempt $attempt failed (${e.message}), retrying with resume in ${attempt}s")
                    // Cooperative backoff; pause/cancel can still win.
                    val until = System.currentTimeMillis() + attempt * 1000L
                    while (System.currentTimeMillis() < until) {
                        if (ctl.cancelRequested || ctl.pauseRequested) return
                        Thread.sleep(200)
                    }
                }
            }
        } finally {
            Log.i(TAG, "worker exit $id")
            synchronized(lock) {
                controls.remove(id, ctl) // only if it is still OUR control
                activeWorkers.remove(id)
                scheduleLocked()
            }
        }
    }

    /**
     * Shared stop check for both transfer loops. Returns true when the
     * worker must unwind (cancel, user pause, or the network gate closed).
     */
    private fun shouldStop(id: String, ctl: Control, part: File): Boolean {
        if (ctl.cancelRequested) return true
        if (ctl.pauseRequested) {
            // pause()/shutdown() already set the state — and a quick resume
            // may have re-queued the item since, so the state is NOT touched.
            update(id) { it.copy(speed = 0, doneBytes = part.length()) }
            persist()
            return true
        }
        if (!gateOk()) {
            // wifi/charging policy closed mid-transfer — same PAUSED
            // semantics as the torrent engine's session pause. Remembered as
            // gate-paused so onGateReopened() can put it back.
            ctl.pauseRequested = true
            update(id) {
                if (it.state == HttpState.RUNNING)
                    it.copy(state = HttpState.PAUSED, speed = 0, doneBytes = part.length(), gatePaused = true) else it
            }
            persist()
            return true
        }
        return false
    }

    /**
     * One full transfer pass (resumes from the `.part`). Returns normally on
     * DONE / paused / gate-closed; throws to trigger the retry loop.
     */
    private fun transfer(id: String, ctl: Control) {
        val item = _items.value.find { it.id == id } ?: return
        if (item.hls) return transferHls(id, ctl)
        var fileName = item.fileName
        var part = File(saveDir, fileName + PART_SUFFIX)
        dropIfLink(part)
        var have = part.length()
        val conn = openFollowing(item.url, item.headers, have, item.publicOnly)
        try {
            var code = conn.code
            if (code == 416) {
                conn.close()
                // Range not satisfiable. If the .part already holds the WHOLE
                // file (app died between the last byte and the rename — seen
                // 2026-11-14 on a 1 GB transfer), promote it instead of
                // throwing the bytes away and redownloading.
                val known = item.totalBytes
                if (have > 0 && known > 0 && have >= known) {
                    finish(id, part, File(saveDir, fileName), have, known)
                    return
                }
                // Otherwise the .part is stale/oversized: drop it once and
                // restart the pass clean (offset 0 can't 416 again).
                part.delete()
                if (have == 0L) throw IOException("HTTP 416 with empty .part")
                have = 0
                return transferFresh(id, ctl)
            }
            if (code != 200 && code != 206) {
                // 4xx is the server's final word (404/403/410…) — retrying
                // just burns backoff time. 408/429 are the transient ones.
                if (code in 400..499 && code != 408 && code != 429) throw PermanentFailure("HTTP $code")
                throw IOException("HTTP $code")
            }
            if (code == 200 && have > 0) {
                // Server ignored the Range: our tail is worthless, truncate.
                part.delete()
                have = 0
            }
            if (have == 0L && conn.header("Content-Type")?.contains("mpegurl", ignoreCase = true) == true) {
                // An extension-less URL that turned out to be an HLS playlist:
                // saving the playlist text as "the video" would be useless.
                conn.close()
                part.delete()
                update(id) { it.copy(hls = true) }
                persist()
                return transferHls(id, ctl)
            }
            val total = when {
                code == 206 -> parseContentRangeTotal(conn.header("Content-Range"))
                    ?: ((conn.body?.contentLength() ?: -1).takeIf { it > 0 }?.plus(have) ?: -1)
                else -> (conn.body?.contentLength() ?: -1).takeIf { it > 0 } ?: -1
            }
            // Content-Disposition wins only when the URL gave no usable extension.
            if (!fileName.contains('.')) {
                dispositionName(conn.header("Content-Disposition"))?.let { dn ->
                    // A server-chosen name is untrusted too: it must not carry a path.
                    val deduped = dedupe(safeFileName(dn))
                    val newPart = File(saveDir, deduped + PART_SUFFIX)
                    dropIfLink(newPart)
                    if (have == 0L || part.renameTo(newPart)) {
                        fileName = deduped
                        part = newPart
                        update(id) { it.copy(fileName = deduped) }
                        persist() // keep state file in sync with the renamed .part
                    }
                }
            }
            update(id) { it.copy(totalBytes = total, doneBytes = have) }

            val input: InputStream = conn.body?.byteStream() ?: throw IOException("empty response body")
            val startedAt = System.currentTimeMillis()
            var speedWindowBytes = 0L
            var speedWindowStart = startedAt
            var lastPersist = startedAt
            RandomAccessFile(part, "rw").use { raf ->
                raf.seek(have)
                val buf = ByteArray(64 * 1024)
                while (true) {
                    if (shouldStop(id, ctl, part)) return
                    val n = input.read(buf)
                    if (n == -1) break
                    raf.write(buf, 0, n)
                    have += n
                    speedWindowBytes += n
                    val now = System.currentTimeMillis()
                    if (now - speedWindowStart >= 500) {
                        val dt = (now - speedWindowStart).coerceAtLeast(1)
                        val speed = speedWindowBytes * 1000 / dt
                        speedWindowBytes = 0
                        speedWindowStart = now
                        update(id) { it.copy(doneBytes = have, speed = speed) }
                    }
                    if (now - lastPersist >= 5000) {
                        persist()
                        lastPersist = now
                    }
                }
            }
            if (total > 0 && have != total) throw IOException("connection ended early: $have/$total bytes")
            finish(id, part, File(saveDir, fileName), have, total)
        } finally {
            conn.close()
        }
    }

    // ------------------------------------------------------------------ HLS

    /**
     * Download an HLS VOD stream into ONE file (plan §12 A3, HLS-by-segments):
     * resolve the best variant, then fetch every segment in order —
     * decrypting AES-128 ones — and append it. MPEG-TS segments concatenate
     * into a playable `.ts`; fMP4 segments (after their init section) into a
     * fragmented `.mp4`. No remux step exists on Android (no ffmpeg), so
     * anything that would need one is refused up front by [HlsPlaylist].
     *
     * Resume truth is a tiny sidecar written atomically after EVERY segment
     * (segments done, bytes on disk, segment count): on restart the `.part`
     * is truncated to that byte count, dropping any half-written segment.
     */
    private fun transferHls(id: String, ctl: Control) {
        val item = _items.value.find { it.id == id } ?: return
        val media = try {
            var text = fetchText(item.url, item.headers, item.publicOnly)
            if (!HlsPlaylist.isPlaylist(text)) throw PermanentFailure("not an HLS playlist")
            var mediaUrl = item.url
            if (HlsPlaylist.isMaster(text)) {
                mediaUrl = HlsPlaylist.pickVariant(text, item.url)
                text = fetchText(mediaUrl, item.headers, item.publicOnly)
            }
            HlsPlaylist.parseMedia(text, mediaUrl)
        } catch (e: HlsPlaylist.Unsupported) {
            throw PermanentFailure(e.message ?: "unsupported stream")
        }
        if (!media.ended) throw PermanentFailure("live stream — cannot be downloaded")
        val total = media.segments.size
        if (total == 0) throw PermanentFailure("stream has no segments")
        // The variant was picked from the (untrusted) playlist text: check it
        // and every URL derived from it before the first byte is fetched.
        if (item.publicOnly) {
            for (u in listOfNotNull(media.initUrl, media.segments.firstOrNull()?.url) + media.segments.mapNotNull { it.key?.uri }) {
                urlPolicy(u)?.let { throw PermanentFailure(it) }
            }
        }

        val ext = if (media.initUrl != null) ".mp4" else ".ts"
        var fileName = item.fileName
        if (!fileName.endsWith(ext, ignoreCase = true)) {
            // First pass: still named after the playlist ("master.m3u8") or a
            // page title — name it for the container it will actually be.
            File(saveDir, fileName + PART_SUFFIX).delete()
            File(saveDir, fileName + PART_SUFFIX + HLS_SIDECAR).delete()
            val stem = if (fileName.contains('.')) fileName.substringBeforeLast('.') else fileName
            val renamed = dedupe(stem + ext)
            fileName = renamed
            update(id) { it.copy(fileName = renamed) }
            persist()
        }
        val part = File(saveDir, fileName + PART_SUFFIX)
        dropIfLink(part)
        val sidecar = File(saveDir, fileName + PART_SUFFIX + HLS_SIDECAR)

        var index = 0
        var have = 0L
        readHlsSidecar(sidecar)?.let { (doneIndex, bytes, count) ->
            if (count == total && doneIndex in 1..total && part.length() >= bytes) {
                index = doneIndex
                have = bytes
            }
        }
        update(id) { it.copy(segmentsTotal = total, segmentsDone = index, doneBytes = have, totalBytes = -1) }

        val keys = HashMap<String, ByteArray>()
        var windowBytes = 0L
        var windowStart = System.currentTimeMillis()
        var lastPersist = windowStart
        RandomAccessFile(part, "rw").use { raf ->
            raf.setLength(have) // drop a half-written segment (or everything, on a fresh start)
            raf.seek(have)
            val initUrl = media.initUrl
            if (index == 0 && initUrl != null) {
                val init = fetchBytes(initUrl, item.headers, MAX_SEGMENT_BYTES, item.publicOnly)
                raf.write(init)
                have += init.size
            }
            while (index < total) {
                if (shouldStop(id, ctl, part)) return
                val seg = media.segments[index]
                var bytes = fetchSegment(seg.url, item.headers, ctl, item.publicOnly) ?: return // stop asked mid-retry
                val k = seg.key
                if (k != null) {
                    val key = keys.getOrPut(k.uri) { fetchBytes(k.uri, item.headers, 1024, item.publicOnly) }
                    if (key.size != 16) throw PermanentFailure("unexpected AES key length (${key.size})")
                    bytes = decryptAes128(bytes, key, k.iv ?: HlsPlaylist.sequenceIv(seg.sequence))
                }
                raf.write(bytes)
                have += bytes.size
                index++
                writeHlsSidecar(sidecar, index, have, total)

                windowBytes += bytes.size
                val now = System.currentTimeMillis()
                val elapsed = now - windowStart
                val doneNow = index
                val haveNow = have
                if (elapsed >= 500) {
                    val speed = windowBytes * 1000 / elapsed
                    windowBytes = 0
                    windowStart = now
                    update(id) { it.copy(segmentsDone = doneNow, doneBytes = haveNow, speed = speed) }
                } else {
                    update(id) { it.copy(segmentsDone = doneNow, doneBytes = haveNow) }
                }
                if (now - lastPersist >= 5000) {
                    persist()
                    lastPersist = now
                }
            }
        }
        sidecar.delete()
        finish(id, part, File(saveDir, fileName), have, -1)
    }

    /**
     * One segment with its OWN retry budget — a 900-segment stream must not
     * die on its fourth network blip (the per-download budget is 4 passes).
     * Returns null when a pause/cancel arrived while waiting to retry.
     */
    private fun fetchSegment(url: String, headers: Map<String, String>, ctl: Control, publicOnly: Boolean): ByteArray? {
        var attempt = 0
        while (true) {
            try {
                return fetchBytes(url, headers, MAX_SEGMENT_BYTES, publicOnly)
            } catch (e: PermanentFailure) {
                throw e
            } catch (e: IOException) {
                attempt++
                if (attempt >= SEGMENT_ATTEMPTS) throw e
                val until = System.currentTimeMillis() + attempt * 1000L
                while (System.currentTimeMillis() < until) {
                    if (ctl.cancelRequested || ctl.pauseRequested) return null
                    Thread.sleep(200)
                }
            }
        }
    }

    /** Whole small body (playlist, key, init section, segment) with a hard size cap. */
    private fun fetchBytes(url: String, headers: Map<String, String>, cap: Int, publicOnly: Boolean = false): ByteArray {
        val conn = openFollowing(url, headers, 0, publicOnly)
        try {
            val code = conn.code
            if (code != 200) {
                if (code in 400..499 && code != 408 && code != 429) throw PermanentFailure("HTTP $code")
                throw IOException("HTTP $code")
            }
            val out = java.io.ByteArrayOutputStream()
            val buf = ByteArray(64 * 1024)
            conn.body?.byteStream()?.use { input ->
                while (true) {
                    val n = input.read(buf)
                    if (n == -1) break
                    if (out.size() + n > cap) throw PermanentFailure("stream part larger than ${cap / 1024} KB")
                    out.write(buf, 0, n)
                }
            } ?: throw IOException("empty response body")
            return out.toByteArray()
        } finally {
            conn.close()
        }
    }

    private fun fetchText(url: String, headers: Map<String, String>, publicOnly: Boolean): String =
        String(fetchBytes(url, headers, MAX_PLAYLIST_BYTES, publicOnly), Charsets.UTF_8)

    /**
     * A one-off fetch for callers that only need the bytes (an RSS item's
     * .torrent link): every redirect hop must be a public address — exactly
     * like a download the agent added — and the body is capped at [cap]
     * bytes. Blocking: call off the main thread. Null on any failure.
     */
    fun fetchPublic(url: String, cap: Int, headers: Map<String, String> = emptyMap()): ByteArray? =
        runCatching { fetchBytes(url, headers, cap, publicOnly = true) }.getOrNull()

    private fun decryptAes128(data: ByteArray, key: ByteArray, iv: ByteArray): ByteArray {
        return try {
            val cipher = javax.crypto.Cipher.getInstance("AES/CBC/PKCS5Padding")
            cipher.init(
                javax.crypto.Cipher.DECRYPT_MODE,
                javax.crypto.spec.SecretKeySpec(key, "AES"),
                javax.crypto.spec.IvParameterSpec(iv),
            )
            cipher.doFinal(data)
        } catch (e: java.security.GeneralSecurityException) {
            throw IOException("segment decryption failed: ${e.message}")
        }
    }

    /** Atomic (tmp + rename): a torn sidecar would resume at the wrong byte and corrupt the file. */
    private fun writeHlsSidecar(file: File, index: Int, bytes: Long, total: Int) {
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText("$index\n$bytes\n$total")
        if (!tmp.renameTo(file)) {
            file.delete()
            tmp.renameTo(file)
        }
    }

    private fun readHlsSidecar(file: File): Triple<Int, Long, Int>? = runCatching {
        val lines = file.readLines()
        Triple(lines[0].trim().toInt(), lines[1].trim().toLong(), lines[2].trim().toInt())
    }.getOrNull()

    /**
     * Completion: promote the `.part` to its final name, mark DONE, export.
     * Hardened after a 1 GB transfer died at this exact step
     * ("could not move .part into place", 2026-11-14): a missing .part with
     * a correct final file means a sibling pass already promoted it; a
     * failed rename falls back to copy+delete before giving up.
     */
    private fun finish(id: String, part: File, finalFile: File, have: Long, total: Long) {
        // Belt and braces behind safeFileName(): a file is only ever written
        // directly inside the save dir — whatever name a server, a page or
        // the agent came up with.
        if (finalFile.canonicalFile.parentFile != saveDir.canonicalFile) {
            part.delete()
            throw PermanentFailure("refused a file name outside the download folder")
        }
        if (!part.exists()) {
            if (finalFile.exists() && (total <= 0 || finalFile.length() == total)) {
                Log.i(TAG, ".part already promoted — marking done: ${finalFile.name}")
            } else {
                throw IOException(".part vanished before completion")
            }
        } else {
            if (finalFile.exists()) finalFile.delete()
            if (!part.renameTo(finalFile)) {
                Log.w(TAG, "rename failed for ${part.name} — falling back to copy")
                part.copyTo(finalFile, overwrite = true)
                part.delete()
            }
        }
        update(id) { it.copy(state = HttpState.DONE, doneBytes = have, totalBytes = if (total > 0) total else have, speed = 0) }
        persist()
        notify(Strings.t.noticeDownloadFinishedFmt.format(finalFile.name))
        // A notification too — the snackbar is only seen while the app is open.
        onFinished?.let { cb -> runCatching { cb(finalFile.name) } }
        // The copy for the user: into the folder chosen in Settings, or the
        // public Downloads folder when none is. On the shared export pool,
        // folder lookup included: a big file, or a slow settings read, must
        // not hold up the queue — and nothing in there can take the app down.
        FinishedExport.submit {
            val result = FinishedExport.export(context, finalFile, exportFolder())
            if (result is FinishedExport.Result.Failed) {
                Log.w(TAG, "copy for the user failed for ${finalFile.name}: ${result.reason}")
                runCatching { notify(Strings.t.noticeExportFailedFmt.format(finalFile.name)) }
                // Told outside the app too: a snackbar is seen only while it is open.
                onCopyFailed?.let { cb -> runCatching { cb(finalFile.name) } }
            }
        }
    }

    /** The 416-recovery pass: identical to [transfer] but guaranteed offset 0. */
    private fun transferFresh(id: String, ctl: Control) {
        val item = _items.value.find { it.id == id } ?: return
        // Temporarily neutralize the .part by resetting it under a fresh pass —
        // simplest correct route: delegate with a zero-length marker removed.
        val part = File(saveDir, item.fileName + PART_SUFFIX)
        if (part.exists()) part.delete()
        transfer(id, ctl)
    }

    /**
     * Opens the connection following redirects MANUALLY (cross-protocol
     * https→http chains are common on video CDNs — verified with the tv123
     * hosts, 2026-11-13).
     *
     * With [publicOnly] (agent-added downloads) EVERY address is checked
     * before it is requested — the first one again and each redirect target:
     * a public server can otherwise 302 the phone into its own network, and
     * the check the agent did on the original URL would never see it.
     * Blocking (DNS) — worker threads only.
     *
     * Transport is OkHttp (1.3.4): it negotiates HTTP/2, which a Chrome
     * User-Agent is expected to speak. HttpURLConnection's HTTP/1.1 +
     * Conscrypt shape gets Cloudflare-403'd by media CDNs (surrit.com)
     * even with a perfect browser header set — measured live 2026-11-15.
     */
    private val http: okhttp3.OkHttpClient = okhttp3.OkHttpClient.Builder()
        .connectTimeout(15, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .followRedirects(false)
        .build()

    private fun openFollowing(
        url: String,
        headers: Map<String, String>,
        offset: Long,
        publicOnly: Boolean = false,
    ): okhttp3.Response {
        var current = url
        var hops = 0
        while (true) {
            if (publicOnly) {
                urlPolicy(current)?.let { throw PermanentFailure(it) }
            }
            val rb = okhttp3.Request.Builder().url(current)
            if (offset > 0) rb.header("Range", "bytes=$offset-")
            for ((k, v) in headers) rb.header(k, v)
            if (!headers.containsKey("User-Agent")) rb.header("User-Agent", USER_AGENT)
            val resp = http.newCall(rb.build()).execute()
            if (resp.code in 300..399) {
                val loc = resp.header("Location")
                resp.close()
                if (loc.isNullOrBlank()) throw IOException("redirect without Location")
                current = URL(URL(current), loc).toString() // resolves relative Locations too
                if (++hops > MAX_REDIRECTS) throw IOException("too many redirects")
                continue
            }
            return resp
        }
    }

    // ------------------------------------------------------------------ state helpers

    private fun update(id: String, mutate: (HttpDownload) -> HttpDownload) =
        synchronized(lock) { updateLocked(id, mutate) }

    private fun updateLocked(id: String, mutate: (HttpDownload) -> HttpDownload) {
        val list = _items.value.toMutableList()
        val idx = list.indexOfFirst { it.id == id }
        if (idx >= 0) {
            list[idx] = mutate(list[idx])
            _items.value = list
        }
    }

    // ------------------------------------------------------------------ persistence

    private fun persist() = synchronized(lock) { persistLocked() }

    /**
     * Fired (outside the lock) when the lane transitions between "nothing
     * running" and "something running" — DeepFluxApp starts the foreground
     * service so the OS does not kill the process mid-download.
     */
    var onActiveChanged: ((Boolean) -> Unit)? = null
    private var wasActive = false

    private fun checkActive() {
        val active = _items.value.any { it.state == HttpState.RUNNING }
        if (active != wasActive) {
            wasActive = active
            onActiveChanged?.let { cb ->
                runCatching { cb(active) } // service start; never let it break a worker
            }
        }
    }

    private fun persistLocked() {
        checkActive()
        // Atomic tmp+rename (config.to_file ritual): a torn file after a hard
        // kill would silently wipe the list on next load.
        runCatching {
            val arr = JSONArray()
            for (it in _items.value) {
                arr.put(
                    JSONObject()
                        .put("id", it.id)
                        .put("url", it.url)
                        .put("fileName", it.fileName)
                        .put("headers", JSONObject(it.headers))
                        .put("totalBytes", it.totalBytes)
                        .put("doneBytes", it.doneBytes)
                        .put("state", it.state.name)
                        .put("error", it.error)
                        .put("hls", it.hls)
                        .put("segmentsDone", it.segmentsDone)
                        .put("segmentsTotal", it.segmentsTotal)
                        .put("po", it.publicOnly)
                        .put("gp", it.gatePaused)
                        .put("pa", it.pauseAll)
                )
            }
            val tmp = File(stateFile.parentFile, stateFile.name + ".tmp")
            tmp.writeText(arr.toString())
            if (!tmp.renameTo(stateFile)) {
                stateFile.delete()
                tmp.renameTo(stateFile)
            }
        }.onFailure { Log.w(TAG, "state save failed: ${it.message}") }
    }

    private fun load() {
        val txt = runCatching { stateFile.takeIf { it.exists() }?.readText() }.getOrNull() ?: return
        val loaded = runCatching {
            val arr = JSONArray(txt)
            (0 until arr.length()).mapNotNull { i ->
                val o = arr.getJSONObject(i)
                val url = o.optString("url")
                if (url.isBlank()) return@mapNotNull null
                HttpDownload(
                    id = o.optString("id").ifBlank { newId() },
                    url = url,
                    fileName = o.optString("fileName").ifBlank { nameFromUrl(url) },
                    headers = runCatching {
                        val h = o.optJSONObject("headers") ?: return@runCatching emptyMap<String, String>()
                        h.keys().asSequence().associateWith { h.optString(it) }
                    }.getOrDefault(emptyMap()),
                    totalBytes = o.optLong("totalBytes", -1),
                    doneBytes = o.optLong("doneBytes", 0),
                    // Anything mid-flight at kill time restarts QUEUED; the
                    // .part length on disk is the resume truth.
                    state = when (o.optString("state")) {
                        HttpState.DONE.name -> HttpState.DONE
                        HttpState.PAUSED.name -> HttpState.PAUSED
                        HttpState.ERROR.name -> HttpState.ERROR
                        else -> HttpState.QUEUED
                    },
                    error = o.optString("error", ""),
                    hls = o.optBoolean("hls", false),
                    segmentsDone = o.optInt("segmentsDone", 0),
                    segmentsTotal = o.optInt("segmentsTotal", 0),
                    publicOnly = o.optBoolean("po", false),
                    gatePaused = o.optBoolean("gp", false),
                    pauseAll = o.optBoolean("pa", false),
                )
            }
        }.getOrDefault(emptyList())
        if (loaded.isEmpty()) return
        synchronized(lock) {
            _items.value = loaded.map {
                // Names saved before safeFileName() existed are repaired for
                // EVERY item. This used to be the third branch of the `when`,
                // so a download that was running when the process died (it
                // comes back QUEUED, or gate-paused) kept a traversal name —
                // and wrote its `.part` outside the folder for a whole run.
                val safe = if (it.fileName != safeFileName(it.fileName)) dedupe(safeFileName(it.fileName)) else it.fileName
                val partLen = File(saveDir, safe + PART_SUFFIX).length()
                val renamed = if (safe != it.fileName) it.copy(fileName = safe) else it
                when {
                    // Paused by the gate when the process died: back into the
                    // queue; the scheduler re-evaluates the gate and starts it
                    // as soon as it permits — no hand resume needed.
                    renamed.gatePaused -> renamed.copy(state = HttpState.QUEUED, gatePaused = false, speed = 0, doneBytes = partLen)
                    renamed.state == HttpState.QUEUED -> renamed.copy(doneBytes = partLen)
                    else -> renamed
                }
            }
            persistLocked()
            scheduleLocked()
        }
        Log.i(TAG, "restored ${loaded.size} http downloads")
    }

    // ------------------------------------------------------------------ naming

    /**
     * Reduces any proposed file name — an agent tool argument, a
     * Content-Disposition header, a page title, a URL segment — to a plain
     * name that cannot escape the save dir (review finding 2026-11-24, HIGH):
     * only the part after the last path separator survives, control and
     * bidi-override characters go, and so do leading / trailing dots (which
     * also turns "." and ".." into nothing). The caller falls back to a name
     * from the URL when the result is empty.
     */
    /**
     * A symbolic link where this engine is about to write goes first — the
     * link itself, never what it points at. A hostile torrent can plant
     * "<name>.part" inside the download folder; opened THROUGH the link, a
     * download's bytes — and the HLS path's `setLength` — would land on any
     * file of the app's private storage the link names (second security
     * review, 2026-11-24). Such torrents are refused on add; this is the
     * second lock on the same door.
     */
    private fun dropIfLink(file: File) {
        if (!java.nio.file.Files.isSymbolicLink(file.toPath())) return
        Log.w(TAG, "removed a symbolic link where a download writes: ${file.name}")
        java.nio.file.Files.deleteIfExists(file.toPath())
    }

    fun safeFileName(raw: String): String {
        val base = raw.substringAfterLast('/').substringAfterLast('\\')
        return base
            .replace(UNSAFE_NAME_CHARS, "_")
            .trim()
            .trim('.')
            .take(200)
            .trim()
    }

    private fun nameFromUrl(url: String): String {
        val noQuery = url.substringBefore('?').substringBefore('#')
        val seg = noQuery.substringAfterLast('/').ifBlank { "download" }
        val decoded = runCatching { URLDecoder.decode(seg, "UTF-8") }.getOrDefault(seg)
        return safeFileName(decoded).ifBlank { "download" }
    }

    private fun dedupe(name: String): String {
        if (!File(saveDir, name).exists() && _items.value.none { it.fileName == name }) return name
        val dot = name.lastIndexOf('.')
        val base = if (dot > 0) name.substring(0, dot) else name
        val ext = if (dot > 0) name.substring(dot) else ""
        var i = 1
        while (true) {
            val candidate = "$base ($i)$ext"
            if (!File(saveDir, candidate).exists() && _items.value.none { it.fileName == candidate }) return candidate
            i++
        }
    }

    /**
     * `filename="x.mp4"` and RFC 5987 `filename*=UTF-8''x.mp4` forms. The
     * value is server-chosen: it is passed through [safeFileName] (the RFC
     * 5987 form is percent-decoded first, so an encoded `..%2F` is caught).
     */
    private fun dispositionName(header: String?): String? {
        if (header.isNullOrBlank()) return null
        Regex("filename\\*=UTF-8''([^;]+)", RegexOption.IGNORE_CASE).find(header)?.groupValues?.get(1)?.let {
            val decoded = runCatching { URLDecoder.decode(it.trim('\"'), "UTF-8") }.getOrNull() ?: return null
            return safeFileName(decoded).ifBlank { null }
        }
        Regex("filename=\"?([^\";]+)\"?", RegexOption.IGNORE_CASE).find(header)?.groupValues?.get(1)?.let {
            return safeFileName(it.trim()).ifBlank { null }
        }
        return null
    }

    /** "bytes 0-99/1234" — the slash part is the total; "*" or absent = unknown. */
    private fun parseContentRangeTotal(header: String?): Long? {
        if (header.isNullOrBlank()) return null
        return header.substringAfterLast('/', "").trim().toLongOrNull()
    }

    private fun newId(): String = UUID.randomUUID().toString().replace("-", "").substring(0, 12)

    companion object {
        private const val TAG = "HttpDownloadRepository"
        private const val PART_SUFFIX = ".part"
        private const val HLS_SIDECAR = ".hls"
        private const val MAX_ACTIVE = 2
        private const val MAX_ATTEMPTS = 4
        private const val SEGMENT_ATTEMPTS = 3
        private const val MAX_REDIRECTS = 6
        private const val MAX_SEGMENT_BYTES = 64 * 1024 * 1024
        private const val MAX_PLAYLIST_BYTES = 5 * 1024 * 1024
        const val USER_AGENT = "Mozilla/5.0 (Linux; Android 14) DeepFlux/1.2"

        /** Control characters, bidi overrides and zero-width marks — they can hide or disguise a name on screen. */
        private val UNSAFE_NAME_CHARS = Regex("[\\p{Cntrl}\\u200B-\\u200F\\u202A-\\u202E\\u2066-\\u2069\\uFEFF]")
    }
}
