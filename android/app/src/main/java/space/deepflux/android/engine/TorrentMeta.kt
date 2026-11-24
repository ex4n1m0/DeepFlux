package space.deepflux.android.engine

import java.security.MessageDigest

/**
 * Reads just enough of a .torrent (bencode) to describe it BEFORE it is
 * added: the info-dict's `name` and the v1 info-hash (SHA-1 over the bencoded
 * info dictionary). Used by the torrent-add confirmation so the user sees
 * what a page / another app is handing over.
 *
 * Fail-closed: anything malformed yields null — never a half-read name. The
 * engine validates the file fully when it is actually added.
 */
object TorrentMeta {

    class TorrentInfo(val name: String, val infoHash: String)

    /** Largest .torrent this reads (the browser lane caps fetches at the same size). */
    private const val MAX_BYTES = 32 * 1024 * 1024
    private const val MAX_DEPTH = 32
    private const val MAX_STRING_LENGTH = 16L * 1024 * 1024

    fun readTorrent(bytes: ByteArray): TorrentInfo? {
        if (bytes.size < 2 || bytes.size > MAX_BYTES) return null
        return runCatching {
            val r = Reader(bytes)
            if (!r.consume('d')) return null
            var name: String? = null
            var info: Pair<Int, Int>? = null
            while (!r.end()) {
                if (r.consume('e')) break
                val key = r.string() ?: return null
                if (key == "info") {
                    if (!r.consume('d')) return null
                    val infoStart = r.pos - 1
                    while (true) {
                        if (r.end()) return null
                        if (r.consume('e')) break
                        val k = r.string() ?: return null
                        if (k == "name") {
                            name = r.string() ?: return null
                        } else if (r.skip(1) != true) {
                            return null
                        }
                    }
                    info = infoStart to r.pos
                    // Everything the dialog needs is in hand; trailing keys
                    // (announce-list, creation date…) are the engine's business.
                    break
                } else if (r.skip(1) != true) {
                    return null
                }
            }
            val infoName = name ?: return null
            val (from, to) = info ?: return null
            val sha1 = MessageDigest.getInstance("SHA-1")
            // The info-hash is SHA-1 over the bencoded info dictionary itself.
            sha1.update(bytes, from, to - from)
            val digest = sha1.digest()
            TorrentInfo(infoName, digest.joinToString("") { "%02x".format(it) })
        }.getOrNull()
    }

    /** Minimal bencode cursor: null from any primitive = malformed input. */
    private class Reader(private val b: ByteArray) {
        var pos = 0

        fun end(): Boolean = pos >= b.size

        fun consume(c: Char): Boolean {
            if (pos < b.size && b[pos] == c.code.toByte()) {
                pos++
                return true
            }
            return false
        }

        /** `<len>:<bytes>` — the length is decimal, no sign, no leading zero. */
        fun bytes(): ByteArray? {
            val colon = pos
            var digits = 0
            while (colon + digits < b.size && b[colon + digits] != ':'.code.toByte()) {
                if (digits >= 8 || b[colon + digits] !in '0'.code..'9'.code) return null
                digits++
            }
            if (digits == 0 || colon + digits >= b.size) return null
            val lenText = String(b, colon, digits, Charsets.ISO_8859_1)
            if (lenText.length > 1 && lenText[0] == '0') return null
            val len = lenText.toLong()
            if (len > MAX_STRING_LENGTH) return null
            val start = colon + digits + 1
            if (start + len > b.size) return null
            pos = (start + len).toInt()
            return b.copyOfRange(start, pos)
        }

        fun string(): String? = bytes()?.toString(Charsets.UTF_8)

        /** Skips one value of any type. False = malformed (true = skipped). */
        fun skip(depth: Int): Boolean {
            if (depth > MAX_DEPTH || end()) return false
            return when (b[pos]) {
                'i'.code.toByte() -> { // integer: i[-]digits e
                    pos++
                    if (!end() && b[pos] == '-'.code.toByte()) pos++
                    var digits = 0
                    while (!end() && b[pos] != 'e'.code.toByte()) {
                        if (b[pos] !in '0'.code..'9'.code) return false
                        digits++
                        pos++
                    }
                    consume('e') && digits > 0
                }
                'l'.code.toByte(), 'd'.code.toByte() -> skipContainer(isDict = b[pos] == 'd'.code.toByte(), depth = depth)
                else -> bytes() != null
            }
        }

        /** Skips a list or dictionary body (the opening byte is already consumed by the caller's check). */
        private fun skipContainer(isDict: Boolean, depth: Int): Boolean {
            pos++
            while (true) {
                if (end()) return false
                if (consume('e')) return true
                if (isDict && string() == null) return false // every dictionary entry starts with a string key
                if (!skip(depth + 1)) return false
            }
        }
    }
}
