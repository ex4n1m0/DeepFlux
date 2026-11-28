package space.deepflux.android.engine

import java.security.MessageDigest

/**
 * Reads just enough of a .torrent (bencode) to describe it BEFORE it is
 * added: the info-dict's `name` and the torrent's info-hashes. Used by the
 * torrent-add confirmation so the user sees what a page / another app is
 * handing over, and (1.4.16) to name the torrent the way the engine will.
 *
 * Fail-closed: anything malformed yields null — never a half-read name. The
 * engine validates the file fully when it is actually added.
 */
object TorrentMeta {

    /**
     * What a .torrent says of itself. Each hash is what the protocol says it is:
     *  * [v1Hash] — the SHA-1 of the info dictionary, of a v1 or a HYBRID torrent (BEP 3);
     *  * [v2Hash] — its SHA-256 computed WITHOUT the `piece layers` entry, of a v2-only or a
     *    hybrid torrent (BEP 52), all 64 hex;
     *  * [engineHash] — the 20-byte name every libtorrent client knows the torrent by: its v1
     *    hash, or the FIRST 20 bytes of its v2 one.
     * Checked against libtorrent 2.1 itself (the reference torrents in the tests).
     */
    class TorrentInfo(val name: String, val v1Hash: String?, val v2Hash: String?) {

        val engineHash: String get() = v2Hash?.take(40) ?: v1Hash!!

        /**
         * The v1 info-hash — and, for a v2-only torrent, which has none, the name the engine
         * knows it by. (Until 1.4.15 this was the SHA-1 of the info dictionary whatever the
         * torrent was: for a v2-only torrent, a value that names no torrent.)
         */
        val infoHash: String get() = v1Hash ?: engineHash
    }

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
            // Of the info dictionary's entries (1.4.16): whether it is of the v2 kind at all,
            // and whether it has a `pieces` string.
            var isV2 = false
            var hasPieces = false
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
                        } else {
                            if (r.skip(1) != true) {
                                return null
                            }
                            when (k) {
                                "meta version" -> isV2 = true
                                "pieces" -> hasPieces = true
                            }
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
            val infoBytes = bytes.copyOfRange(from, to)
            // A v2-only torrent has no v1 info-hash (BEP 52: no `pieces`). One of the old
            // kind is hashed as it is, pieces or no pieces — as this reader always did.
            val v1 = if (!isV2 || hasPieces) hex(MessageDigest.getInstance("SHA-1").digest(infoBytes)) else null
            val v2 = if (isV2) {
                // BEP 52: the SHA-256 of the bencoded info dictionary, as it is. The
                // `piece layers` that make a v2 torrent large are NO part of `info` — they sit
                // in the metainfo's top dictionary, after it — which is why no info-hash covers
                // them. (I first wrote an exclusion of them from inside `info`, where they never
                // are: dead code, found by a mutation that removed it and changed nothing —
                // TorrentIdentityV2Test.)
                hex(MessageDigest.getInstance("SHA-256").digest(infoBytes))
            } else {
                null
            }
            TorrentInfo(infoName, v1, v2)
        }.getOrNull()
    }

    private fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }

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
