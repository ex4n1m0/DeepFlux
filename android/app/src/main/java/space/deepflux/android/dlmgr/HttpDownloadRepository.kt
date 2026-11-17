package space.deepflux.android.dlmgr

import android.content.Context
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
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
) {

    private val lock = Any()

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
     */
    fun add(
        url: String,
        headers: Map<String, String> = emptyMap(),
        fileName: String? = null,
        hls: Boolean = false,
    ): HttpDownload? {
        val trimmed = url.trim()
        if (!trimmed.startsWith("http://", true) && !trimmed.startsWith("https://", true)) return null
        awaitLoaded()
        synchronized(lock) {
            val existing = _items.value.find { it.url == trimmed && it.state != HttpState.ERROR }
            if (existing != null) {
                notify("Already in the list")
                return existing
            }
            val item = HttpDownload(
                id = newId(),
                url = trimmed,
                fileName = dedupe(fileName ?: nameFromUrl(trimmed)),
                headers = headers,
                hls = hls || trimmed.substringBefore('?').endsWith(".m3u8", ignoreCase = true),
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
                if (it.state == HttpState.RUNNING || it.state == HttpState.QUEUED)
                    it.copy(state = HttpState.PAUSED, speed = 0) else it
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
                    it.copy(state = HttpState.QUEUED, error = "", speed = 0) else it
            }
            // Deliberately NOT clearing the old worker's pause flag: it must
            // still unwind. The re-queued item gets a fresh worker (and a
            // fresh Control) from scheduleLocked once the old one is gone.
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
        if (!gateOk()) return // wifi/charging gate closed: nothing starts; workers also self-pause
        for (item in _items.value) {
            if (slots <= 0) break
            if (item.state != HttpState.QUEUED) continue
            // Its previous worker is still unwinding (pause → quick resume):
            // that worker's exit re-runs this scheduler and picks it up then.
            if (item.id in activeWorkers) continue
            updateLocked(item.id) { it.copy(state = HttpState.RUNNING, speed = 0) }
            val ctl = Control()
            controls[item.id] = ctl
            activeWorkers.add(item.id)
            pool.execute { runWorker(item.id, ctl) }
            slots--
        }
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
                        update(id) {
                            it.copy(state = HttpState.ERROR, error = (e.message ?: "network error").take(160), speed = 0)
                        }
                        persist()
                        notify("Download failed: ${snapshot.fileName}")
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
            // semantics as the torrent engine's session pause.
            ctl.pauseRequested = true
            update(id) {
                if (it.state == HttpState.RUNNING)
                    it.copy(state = HttpState.PAUSED, speed = 0, doneBytes = part.length()) else it
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
        var have = part.length()
        val conn = openFollowing(item.url, item.headers, have)
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
                    val deduped = dedupe(dn)
                    val newPart = File(saveDir, deduped + PART_SUFFIX)
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
            var text = fetchText(item.url, item.headers)
            if (!HlsPlaylist.isPlaylist(text)) throw PermanentFailure("not an HLS playlist")
            var mediaUrl = item.url
            if (HlsPlaylist.isMaster(text)) {
                mediaUrl = HlsPlaylist.pickVariant(text, item.url)
                text = fetchText(mediaUrl, item.headers)
            }
            HlsPlaylist.parseMedia(text, mediaUrl)
        } catch (e: HlsPlaylist.Unsupported) {
            throw PermanentFailure(e.message ?: "unsupported stream")
        }
        if (!media.ended) throw PermanentFailure("live stream — cannot be downloaded")
        val total = media.segments.size
        if (total == 0) throw PermanentFailure("stream has no segments")

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
                val init = fetchBytes(initUrl, item.headers, MAX_SEGMENT_BYTES)
                raf.write(init)
                have += init.size
            }
            while (index < total) {
                if (shouldStop(id, ctl, part)) return
                val seg = media.segments[index]
                var bytes = fetchSegment(seg.url, item.headers, ctl) ?: return // stop asked mid-retry
                val k = seg.key
                if (k != null) {
                    val key = keys.getOrPut(k.uri) { fetchBytes(k.uri, item.headers, 1024) }
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
    private fun fetchSegment(url: String, headers: Map<String, String>, ctl: Control): ByteArray? {
        var attempt = 0
        while (true) {
            try {
                return fetchBytes(url, headers, MAX_SEGMENT_BYTES)
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
    private fun fetchBytes(url: String, headers: Map<String, String>, cap: Int): ByteArray {
        val conn = openFollowing(url, headers, 0)
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

    private fun fetchText(url: String, headers: Map<String, String>): String =
        String(fetchBytes(url, headers, MAX_PLAYLIST_BYTES), Charsets.UTF_8)

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
        notify("Download finished: ${finalFile.name}")
        Thread({
            runCatching { MediaStoreExport.exportToDownloads(context, finalFile) }
        }, "http-dl-export").apply { isDaemon = true }.start()
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

    private fun openFollowing(url: String, headers: Map<String, String>, offset: Long): okhttp3.Response {
        var current = url
        var hops = 0
        while (true) {
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
                )
            }
        }.getOrDefault(emptyList())
        if (loaded.isEmpty()) return
        synchronized(lock) {
            _items.value = loaded.map {
                val partLen = File(saveDir, it.fileName + PART_SUFFIX).length()
                if (it.state == HttpState.QUEUED) it.copy(doneBytes = partLen) else it
            }
            persistLocked()
            scheduleLocked()
        }
        Log.i(TAG, "restored ${loaded.size} http downloads")
    }

    // ------------------------------------------------------------------ naming

    private fun nameFromUrl(url: String): String {
        val noQuery = url.substringBefore('?').substringBefore('#')
        val seg = noQuery.substringAfterLast('/').ifBlank { "download" }
        val decoded = runCatching { URLDecoder.decode(seg, "UTF-8") }.getOrDefault(seg)
        return decoded.replace(Regex("[\\\\/:*?\"<>|]"), "_").take(120).ifBlank { "download" }
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

    /** `filename="x.mp4"` and RFC 5987 `filename*=UTF-8''x.mp4` forms. */
    private fun dispositionName(header: String?): String? {
        if (header.isNullOrBlank()) return null
        Regex("filename\\*=UTF-8''([^;]+)", RegexOption.IGNORE_CASE).find(header)?.groupValues?.get(1)?.let {
            return runCatching { URLDecoder.decode(it.trim('\"'), "UTF-8") }.getOrNull()
        }
        Regex("filename=\"?([^\";]+)\"?", RegexOption.IGNORE_CASE).find(header)?.groupValues?.get(1)?.let {
            return it.trim().replace(Regex("[\\\\/:*?\"<>|]"), "_").ifBlank { null }
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
    }
}
