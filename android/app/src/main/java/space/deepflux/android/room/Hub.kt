package space.deepflux.android.room

import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * HTTP client for the OnlyHumans rendezvous hub — the Kotlin port of the
 * desktop's Hub class (untrusted storage: every consumed payload is
 * signature-verified by the controller before use). Hub reads are capped
 * at 2 MiB (the desktop's streaming-cap rule).
 */
class Hub(private val baseUrl: String = HUB_BASE) {

    private var gkCache: Triple<String, ByteArray, Long>? = null

    /**
     * The hub validates canonical timestamps against ITS clock; a device
     * clock drifting more than its window gets "stale timestamp" (measured
     * live: this network's hub ran ~49 s ahead of the local clock). Every
     * response's Date header refreshes the offset; canonical ts values use
     * the corrected clock.
     */
    @Volatile private var serverOffsetMs = 0L

    private fun hubNowMs(): Long = System.currentTimeMillis() + serverOffsetMs

    private fun noteServerDate(conn: HttpURLConnection) {
        runCatching {
            val date = conn.getHeaderField("Date") ?: return
            val serverMs = java.text.SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss zzz", java.util.Locale.US)
                .parse(date)?.time ?: return
            val offset = serverMs - System.currentTimeMillis()
            // Ignore wild offsets (cached/proxy weirdness) but adopt sane ones.
            if (kotlin.math.abs(offset) < 5 * 60_000) serverOffsetMs = offset
        }
    }

    fun fetchGk(): Pair<String, ByteArray> {
        gkCache?.let { if (System.currentTimeMillis() - it.third < GK_TTL_MS) return it.first to it.second }
        val payload = request("GET", "/api/gk", headers = mapOf("cache-control" to "no-cache"))
        if (payload == null) throw RoomCrypto.RoomError("the room hub is not serving a room key yet")
        val gk = runCatching { RoomCrypto.unb64(payload.optString("gk_b64", "")) }
            .getOrElse { throw RoomCrypto.RoomError("room hub served a malformed key") }
        if (gk.size != 32) throw RoomCrypto.RoomError("room hub served a malformed key")
        val version = payload.optString("version", "")
        gkCache = Triple(version, gk, System.currentTimeMillis())
        return version to gk
    }

    fun reg(peerId: String, pubB64: String, sign: (ByteArray) -> ByteArray) {
        val ts = hubNowMs()
        val body = JSONObject()
            .put("peer_id", peerId)
            .put("public_key_b64", pubB64)
            .put("addrs", JSONArray())
            .put("ts_ms", ts)
            .put("sig_b64", RoomCrypto.b64(sign(regCanonical(peerId, pubB64, emptyList(), ts))))
        val code = raw("PUT", "/api/reg", body)
        if (code !in 200..299) throw RoomCrypto.RoomError("reg failed: $code")
    }

    fun lookupRoom(roomHex: String): JSONObject? {
        val (code, payload) = rawWithBody("GET", "/api/room/$roomHex")
        if (code == 404) return null
        if (code !in 200..299) throw RoomCrypto.RoomError("room lookup failed: $code")
        return payload
    }

    /** Claim hosting; false on 409 (a live record names someone else). */
    fun registerRoom(roomHex: String, peerId: String, pubB64: String, sign: (ByteArray) -> ByteArray): Boolean {
        val ts = hubNowMs()
        val body = JSONObject()
            .put("room_id", roomHex)
            .put("host_peer_id", peerId)
            .put("host_public_key_b64", pubB64)
            .put("ts_ms", ts)
            .put("sig_b64", RoomCrypto.b64(sign(roomCanonical(roomHex, peerId, pubB64, ts))))
        val code = raw("PUT", "/api/room", body)
        if (code == 409) return false
        if (code !in 200..299) throw RoomCrypto.RoomError("room register failed: $code")
        return true
    }

    /** Chunked pushes (hub caps one push at 16 items); a 429 waits one window out. */
    fun mailPushBatch(peerId: String, pubB64: String, sign: (ByteArray) -> ByteArray, batch: List<Pair<String, JSONObject>>) {
        var i = 0
        while (i < batch.size) {
            val items = JSONArray()
            for ((to, env) in batch.subList(i, minOf(i + MAIL_CHUNK, batch.size))) {
                val envJson = env.toString()
                val ts = hubNowMs()
                items.put(
                    JSONObject()
                        .put("to", to).put("from", peerId).put("public_key_b64", pubB64)
                        .put("env_json", envJson).put("ts_ms", ts)
                        .put("sig_b64", RoomCrypto.b64(sign(mailCanonical(peerId, to, ts, envJson)))),
                )
            }
            var code = raw("PUT", "/api/inbox", JSONObject().put("items", items))
            if (code == 429) {
                Thread.sleep(MAIL_THROTTLE_MS)
                code = raw("PUT", "/api/inbox", JSONObject().put("items", items))
            }
            if (code !in 200..299) throw RoomCrypto.RoomError("mail push failed: $code")
            i += MAIL_CHUNK
        }
    }

    fun mailDrain(peerId: String, sign: (ByteArray) -> ByteArray): List<JSONObject> {
        val ts = hubNowMs()
        val sig = RoomCrypto.b64(sign(drainCanonical(peerId, ts)))
        val payload = request("GET", "/api/inbox/$peerId?ts_ms=$ts&sig_b64=$sig")
            ?: throw RoomCrypto.RoomError("mail drain failed: empty")
        val items = payload.optJSONArray("items") ?: return emptyList()
        val out = mutableListOf<JSONObject>()
        for (i in 0 until items.length()) {
            (items.opt(i) as? JSONObject)?.let { out.add(it) }
        }
        return out
    }

    // ------------------------------------------------------------- plumbing

    private fun request(method: String, path: String, body: JSONObject? = null, headers: Map<String, String> = emptyMap()): JSONObject? {
        val (code, payload) = rawWithBody(method, path, body, headers)
        if (code !in 200..299) {
            if (code == 503 && path == "/api/gk") throw RoomCrypto.RoomError("the room hub is not serving a room key yet")
            throw RoomCrypto.RoomError("hub $path failed: $code")
        }
        return payload
    }

    private fun raw(method: String, path: String, body: JSONObject?): Int = rawWithBody(method, path, body).first

    private fun rawWithBody(
        method: String,
        path: String,
        body: JSONObject? = null,
        headers: Map<String, String> = emptyMap(),
    ): Pair<Int, JSONObject?> {
        val conn = URL(baseUrl.trimEnd('/') + path).openConnection() as HttpURLConnection
        return try {
            conn.requestMethod = method
            conn.connectTimeout = 5_000
            conn.readTimeout = 10_000
            headers.forEach { (k, v) -> conn.setRequestProperty(k, v) }
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val code = conn.responseCode
            noteServerDate(conn)
            val stream: InputStream? = if (code in 200..299) conn.inputStream else conn.errorStream
            val bytes = stream?.use { readCapped(it) }
            code to bytes?.let { runCatching { JSONObject(String(it, Charsets.UTF_8)) }.getOrNull() }
        } finally {
            conn.disconnect()
        }
    }

    private fun readCapped(input: InputStream): ByteArray {
        val out = ByteArrayOutputStream()
        val buf = ByteArray(16 * 1024)
        input.use { src ->
            while (true) {
                val n = src.read(buf)
                if (n == -1) break
                if (out.size() + n > BODY_LIMIT) throw RoomCrypto.RoomError("hub response exceeded the size limit")
                out.write(buf, 0, n)
            }
        }
        return out.toByteArray()
    }

    companion object {
        const val HUB_BASE = "https://onlyhumans.deepflux.space"
        private const val BODY_LIMIT = 2 * 1024 * 1024
        private const val GK_TTL_MS = 60_000L
        private const val MAIL_CHUNK = 16
        private const val MAIL_THROTTLE_MS = 4_200L

        fun mailCanonical(sender: String, to: String, tsMs: Long, envJson: String): ByteArray =
            "OH1-mail-v1|$sender|$to|$tsMs|$envJson".toByteArray(Charsets.UTF_8)

        fun drainCanonical(peer: String, tsMs: Long): ByteArray =
            "OH1-drain-v1|$peer|$tsMs".toByteArray(Charsets.UTF_8)

        fun regCanonical(peer: String, pub: String, addrs: List<String>, tsMs: Long): ByteArray =
            "OH1-reg|$peer|$pub|${addrs.joinToString(",")}|$tsMs".toByteArray(Charsets.UTF_8)

        fun roomCanonical(room: String, host: String, pub: String, tsMs: Long): ByteArray =
            "OH1-room|$room|$host|$pub|$tsMs".toByteArray(Charsets.UTF_8)
    }
}

/** History JSONL (one file per room, capped at HISTORY_KEEP lines). */
class RoomHistory(directory: File, roomHex: String) {
    private val file = File(File(directory, "rooms"), "$roomHex.jsonl")

    fun append(ts: Long, nick: String, text: String) {
        runCatching {
            file.parentFile?.mkdirs()
            file.appendText(
                JSONObject().put("ts", ts).put("nick", nick).put("text", text).toString() + "\n",
                Charsets.UTF_8,
            )
            trim()
        }
    }

    fun load(limit: Int = 200): List<Triple<Long, String, String>> {
        val lines = runCatching { file.readLines() }.getOrDefault(emptyList())
        return lines.takeLast(limit).mapNotNull { line ->
            runCatching {
                val o = JSONObject(line)
                Triple(o.optLong("ts"), o.optString("nick"), o.optString("text"))
            }.getOrNull()
        }
    }

    private fun trim() {
        runCatching {
            val lines = file.readLines()
            if (lines.size > 800) file.writeText(lines.takeLast(800).joinToString("\n") + "\n")
        }
    }
}
