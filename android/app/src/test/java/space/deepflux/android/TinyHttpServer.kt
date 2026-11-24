package space.deepflux.android

import java.io.BufferedReader
import java.io.Closeable
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger

/**
 * Minimal HTTP/1.1 server for unit tests (the JDK's own `com.sun.net.httpserver`
 * is not on Android's unit-test classpath, and a real socket exercises the
 * production HTTP stack under Robolectric).
 *
 * One request per connection, `Connection: close`. Handlers may delay the
 * response body to model a slow transfer. Counts every request it serves.
 */
class TinyHttpServer : Closeable {

    class Request(val method: String, val path: String, val headers: Map<String, String>) {
        val range: String? get() = headers["range"]
    }

    class Response(
        val status: Int = 200,
        val body: ByteArray = ByteArray(0),
        val headers: Map<String, String> = mapOf("Content-Type" to "application/octet-stream"),
        /** Wait this long before answering — models a slow server. */
        val delayMs: Long = 0,
        /** Write the body in chunks of this size, sleeping [chunkDelayMs] between them. */
        val chunkSize: Int = 0,
        val chunkDelayMs: Long = 0,
    )

    private val server = ServerSocket(0, 64, InetAddress.getByName("127.0.0.1"))
    // Routes are registered on the test thread and read on connection
    // threads — a plain map let a connection see the route table empty (404).
    private val handlers = ConcurrentHashMap<String, (Request) -> Response>()
    private val counter = AtomicInteger()

    /** Every request served, in arrival order — lets a test assert on Range headers etc. */
    val requests = CopyOnWriteArrayList<Request>()

    val port: Int get() = server.localPort
    val hits: Int get() = counter.get()

    private val acceptor = Thread({ acceptLoop() }, "tiny-http").apply {
        isDaemon = true
        start()
    }

    fun route(path: String, handler: (Request) -> Response) {
        handlers[path] = handler
    }

    fun url(path: String) = "http://127.0.0.1:$port$path"

    private fun acceptLoop() {
        while (!server.isClosed) {
            val socket = runCatching { server.accept() }.getOrNull() ?: break
            Thread({ serve(socket) }, "tiny-http-conn").apply {
                isDaemon = true
                start()
            }
        }
    }

    private fun serve(socket: Socket) {
        socket.use { sock ->
            runCatching {
                sock.soTimeout = 15_000
                val reader = BufferedReader(InputStreamReader(sock.getInputStream(), Charsets.ISO_8859_1))
                val requestLine = reader.readLine() ?: return
                val headers = LinkedHashMap<String, String>()
                while (true) {
                    val line = reader.readLine() ?: break
                    if (line.isEmpty()) break
                    val colon = line.indexOf(':')
                    if (colon > 0) headers[line.substring(0, colon).trim().lowercase()] = line.substring(colon + 1).trim()
                }
                counter.incrementAndGet()
                // "GET /path?query HTTP/1.1"
                val parts = requestLine.split(' ')
                val method = parts.getOrNull(0) ?: return
                val path = parts.getOrNull(1)?.substringBefore('?') ?: return
                val request = Request(method, path, headers)
                requests.add(request)
                val response = handlers[path]?.invoke(request)?.let { slicedByRange(it, request.range) }
                    ?: Response(status = 404)
                if (response.delayMs > 0) Thread.sleep(response.delayMs)
                val out = sock.getOutputStream()
                val head = buildString {
                    append("HTTP/1.1 ").append(response.status).append(' ').append(reason(response.status)).append("\r\n")
                    response.headers.forEach { (k, v) -> append(k).append(": ").append(v).append("\r\n") }
                    append("Content-Length: ").append(response.body.size).append("\r\n")
                    append("Connection: close\r\n\r\n")
                }
                out.write(head.toByteArray(Charsets.ISO_8859_1))
                if (response.chunkSize > 0) {
                    var offset = 0
                    while (offset < response.body.size) {
                        val end = minOf(offset + response.chunkSize, response.body.size)
                        out.write(response.body, offset, end - offset)
                        out.flush()
                        offset = end
                        if (response.chunkDelayMs > 0 && offset < response.body.size) Thread.sleep(response.chunkDelayMs)
                    }
                } else {
                    out.write(response.body)
                }
                out.flush()
            }
        }
    }

    override fun close() {
        server.close()
    }

    private val RANGE = Regex("bytes=(\\d+)-(\\d*)")

    /**
     * Single-range support ("bytes=N-" resumes, as the download engine sends
     * them): a 200 body becomes 206 with a Content-Range header.
     */
    private fun slicedByRange(response: Response, range: String?): Response {
        if (range == null || response.status != 200) return response
        val m = RANGE.find(range) ?: return response
        val total = response.body.size
        val from = m.groupValues[1].toLong()
        val to = m.groupValues[2].takeIf { it.isNotEmpty() }?.toLong() ?: (total - 1L)
        if (from > to || from >= total) return Response(status = 416)
        return Response(
            status = 206,
            body = response.body.copyOfRange(from.toInt(), (to + 1).toInt()),
            headers = response.headers + mapOf(
                "Content-Range" to "bytes $from-$to/$total",
                "Accept-Ranges" to "bytes",
            ),
        )
    }

    private fun reason(status: Int): String = when (status) {
        200 -> "OK"
        206 -> "Partial Content"
        302 -> "Found"
        404 -> "Not Found"
        416 -> "Range Not Satisfiable"
        else -> "Status"
    }
}
