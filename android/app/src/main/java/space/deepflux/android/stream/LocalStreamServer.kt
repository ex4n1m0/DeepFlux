package space.deepflux.android.stream

import android.util.Log
import fi.iki.elonen.NanoHTTPD
import java.io.File
import java.io.FileInputStream
import java.io.InputStream
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * Loopback HTTP server that serves torrent files to the in-app player
 * (ExoPlayer). The Android twin of the desktop's "stream while downloading":
 *
 *  - Files register under opaque ids (`/stream/<id>`); only registered
 *    absolute paths are ever served — no directory traversal.
 *  - HTTP Range requests are supported so seeking works.
 *  - GROWING files (download still running) are served "tail -f" style:
 *    the response has no Content-Length and the stream blocks at the
 *    current EOF until more bytes land (or a stall timeout closes it).
 *    Sequential download mode makes the head of the file arrive first,
 *    which is what lets playback start early.
 */
class LocalStreamServer {

    private var server: Server? = null
    private val files = ConcurrentHashMap<String, File>()
    private val reverse = ConcurrentHashMap<String, String>()

    /** A non-file byte source (e.g. a MediaStore content URI), opened per request. */
    class StreamSource(
        val name: String,
        val length: Long,
        val mime: String,
        val open: (start: Long) -> InputStream,
    )

    private val streams = ConcurrentHashMap<String, StreamSource>()

    val baseUrl: String?
        get() = server?.let { "http://127.0.0.1:${it.listeningPort}" }

    /** Starts the server if needed and returns the stream URL for the file. */
    fun registerAndGetUrl(file: File): String? {
        ensureServer() ?: return null
        val id = reverse[file.absolutePath] ?: newId().also {
            files[it] = file
            reverse[file.absolutePath] = it
        }
        return "${baseUrl}/stream/$id"
    }

    /** Same for a byte source that is not a plain file (MediaStore export). */
    fun registerStream(source: StreamSource): String? {
        ensureServer() ?: return null
        val id = newId()
        streams[id] = source
        return "${baseUrl}/stream/$id"
    }

    private fun ensureServer(): Server? {
        var srv = server
        if (srv == null || !srv.wasStarted()) {
            srv = Server(PORT)
            val ok = runCatching { srv.start() }.isSuccess
            if (!ok) {
                Log.e(TAG, "failed to start loopback server on $PORT")
                return null
            }
            server = srv
        }
        return srv
    }

    fun stop() {
        server?.stop()
        server = null
        files.clear()
        reverse.clear()
        streams.clear()
    }

    private fun newId(): String =
        java.util.UUID.randomUUID().toString().replace("-", "").take(16)

    /** Factory used by the nested Server (non-inner class can't see the outer TailInputStream ctor directly). */
    private fun createTail(file: File, start: Long, endInclusive: Long): InputStream =
        TailInputStream(file, start, endInclusive)

    private inner class Server(port: Int) : NanoHTTPD("127.0.0.1", port) {
        override fun serve(session: IHTTPSession): Response {
            val path = session.uri.trimEnd('/')
            if (!path.startsWith("/stream/")) {
                return newFixedLengthResponse(Response.Status.NOT_FOUND, "text/plain", "not found")
            }
            val range = session.headers["range"]
            val id = path.removePrefix("/stream/")
            val file = files[id]
            if (file != null) {
                if (!file.exists()) {
                    return newFixedLengthResponse(Response.Status.NOT_FOUND, "text/plain", "file missing")
                }
                return serveFile(file, range)
            }
            val stream = streams[id]
                ?: return newFixedLengthResponse(Response.Status.NOT_FOUND, "text/plain", "unknown stream")
            return serveStream(stream, range)
        }

        /** Content-source serving: full or one Range slice, freshly opened per request. */
        private fun serveStream(source: StreamSource, rangeHeader: String?): Response {
            val length = source.length
            var start = 0L
            var endIncl = length - 1
            var hasRange = false
            if (rangeHeader != null) {
                val m = Regex("bytes=(\\d*)-(\\d*)").find(rangeHeader)
                if (m != null) {
                    hasRange = true
                    if (m.groupValues[1].isNotEmpty()) start = m.groupValues[1].toLong()
                    endIncl = if (m.groupValues[2].isNotEmpty()) m.groupValues[2].toLong() else -1L
                }
            }
            if (hasRange) {
                if (endIncl < 0 || endIncl >= length) endIncl = length - 1
                if (start > endIncl || start >= length) {
                    val r = newFixedLengthResponse(Response.Status.RANGE_NOT_SATISFIABLE, source.mime, "")
                    r.addHeader("Content-Range", "bytes */$length")
                    return r
                }
                val body = ContentSlice(source, start, endIncl)
                val resp = newFixedLengthResponse(Response.Status.PARTIAL_CONTENT, source.mime, body, endIncl - start + 1)
                resp.addHeader("Content-Range", "bytes $start-$endIncl/$length")
                return resp
            }
            val body = ContentSlice(source, 0, length - 1)
            return newFixedLengthResponse(Response.Status.OK, source.mime, body, length)
        }

        private fun serveFile(file: File, rangeHeader: String?): Response {
            val length = file.length()
            val mime = mimeOf(file.name)
            var start = 0L
            var endIncl = length - 1
            var hasRange = false
            if (rangeHeader != null) {
                val m = Regex("bytes=(\\d*)-(\\d*)").find(rangeHeader)
                if (m != null) {
                    hasRange = true
                    if (m.groupValues[1].isNotEmpty()) start = m.groupValues[1].toLong()
                    endIncl = if (m.groupValues[2].isNotEmpty()) m.groupValues[2].toLong() else -1L
                }
            }
            if (hasRange) {
                if (endIncl < 0 || endIncl >= length) endIncl = length - 1
                if (start > endIncl || start >= length) {
                    val r = newFixedLengthResponse(
                        Response.Status.RANGE_NOT_SATISFIABLE, mime, ""
                    )
                    r.addHeader("Content-Range", "bytes */$length")
                    return r
                }
                val resp = newChunkedResponse(
                    Response.Status.PARTIAL_CONTENT, mime,
                    makeTail(file, start, endIncl)
                )
                resp.addHeader("Content-Range", "bytes $start-$endIncl/$length")
                return resp
            }
            // Full request on a possibly-growing file: no Content-Length,
            // stream until the file's final size is reached (chunked).
            return newChunkedResponse(Response.Status.OK, mime, makeTail(file, 0, Long.MAX_VALUE))
        }

        private fun makeTail(file: File, start: Long, endInclusive: Long): InputStream =
            createTail(file, start, endInclusive)

        private fun mimeOf(name: String): String = when {
            name.endsWith(".mp4", true) || name.endsWith(".m4v", true) -> "video/mp4"
            name.endsWith(".webm", true) -> "video/webm"
            name.endsWith(".mkv", true) -> "video/x-matroska"
            name.endsWith(".ts", true) -> "video/mp2t"
            else -> "application/octet-stream"
        }
    }

    /**
     * One [start, endInclusive] slice of a [StreamSource]: opens the source
     * fresh, skips to the offset (sources are content-resolver streams whose
     * skip cost is paid per seek — the same trade the file tail makes).
     */
    private class ContentSlice(
        private val source: StreamSource,
        start: Long,
        private val endInclusive: Long,
    ) : InputStream() {
        private val input = source.open(start)
        private var pos = start

        override fun read(): Int {
            val b = ByteArray(1)
            return if (read(b, 0, 1) == -1) -1 else b[0].toInt() and 0xff
        }

        override fun read(buffer: ByteArray, off: Int, len: Int): Int {
            if (pos > endInclusive) return -1
            val n = input.read(buffer, off, minOf(len.toLong(), endInclusive - pos + 1).toInt())
            if (n > 0) pos += n
            return n
        }

        override fun close() {
            runCatching { input.close() }
        }
    }

    /**
     * InputStream over [file] from [start] to [endInclusive] that FOLLOWS
     * the file while it grows: at current EOF it waits for more bytes, up
     * to [TAIL_STALL_MS] without growth before ending the stream.
     */
    private class TailInputStream(
        private val file: File,
        start: Long,
        private val endInclusive: Long,
    ) : InputStream() {

        private val input = FileInputStream(file)
        private var skippedTotal = 0L
        private var lastGrow = System.currentTimeMillis()
        private var knownLength = file.length()

        init {
            var toSkip = start
            while (toSkip > 0) {
                val n = input.skip(toSkip)
                if (n <= 0) break
                toSkip -= n
                skippedTotal += n
            }
        }

        private fun limit(): Long {
            val cap = if (endInclusive == Long.MAX_VALUE) Long.MAX_VALUE else endInclusive + 1
            return minOf(knownLength, cap)
        }

        override fun read(): Int {
            val b = ByteArray(1)
            return if (read(b, 0, 1) == -1) -1 else b[0].toInt() and 0xff
        }

        override fun read(buffer: ByteArray, off: Int, len: Int): Int {
            while (true) {
                val pos = skippedTotal
                if (pos >= limit()) {
                    // At EOF of what we know: wait for growth (or end).
                    val now = System.currentTimeMillis()
                    if (now - lastGrow > TAIL_STALL_MS) return -1
                    val grown = file.length()
                    if (grown > knownLength) {
                        knownLength = grown
                        lastGrow = now
                        continue
                    }
                    if (endInclusive != Long.MAX_VALUE && file.length() >= endInclusive + 1) {
                        // Range fully downloaded but we're still short — wait a bit.
                        Thread.sleep(TAIL_POLL_MS)
                        continue
                    }
                    Thread.sleep(TAIL_POLL_MS)
                    continue
                }
                val n = input.read(buffer, off, minOf(len.toLong(), limit() - pos).toInt())
                if (n > 0) {
                    skippedTotal += n
                    return n
                }
                return -1
            }
        }

        override fun close() {
            runCatching { input.close() }
        }
    }

    companion object {
        private const val TAG = "LocalStreamServer"
        private const val PORT = 8198
        private const val SOCKET_TIMEOUT_MS = 0L // no timeout while streaming
        private const val TAIL_POLL_MS = 400L
        private const val TAIL_STALL_MS = 25_000L
    }
}
