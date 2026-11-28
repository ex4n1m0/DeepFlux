package space.deepflux.android.engine

import java.net.URLDecoder
import java.util.Locale

/**
 * The magnet's hashes and addresses — the app's first reading of text anyone may have written.
 * [infoHashHex] is the torrent's key in the app; [magnetFor] builds the magnet that is saved
 * with a torrent so a restart can ask for it again; [forEngine] is the magnet in the one
 * spelling the engine parses.
 *
 * A magnet carries a v1 info-hash as `xt=urn:btih:<40 hex | 32 base32>`, and a v2 one in either
 * of two spellings (BEP 52): `urn:btih:<64 hex>` — the widespread one — or the multihash
 * `urn:btmh:1220<64 hex>`. Both are accepted here; the engine parses only the second.
 */
object MagnetParser {

    private const val B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    private val XT_RE = Regex("xt=urn:bt(ih|mh):([A-Za-z0-9]+)", RegexOption.IGNORE_CASE)
    private val MAGNET_RE = Regex("magnet:\\?xt=urn:bt(?:ih|mh):[A-Za-z0-9]+[^\\s]*", RegexOption.IGNORE_CASE)

    /** Pulls the magnet URI out of arbitrary shared text ("here, grab this magnet:?... thanks"). */
    fun extractMagnet(text: String): String? = MAGNET_RE.find(text)?.value

    /**
     * The torrent's display name from the magnet's `dn=` parameter
     * (percent-decoded, "+" = space), or "" when the link carries none.
     */
    fun displayName(magnet: String): String {
        val m = DN_RE.find(magnet) ?: return ""
        return runCatching { URLDecoder.decode(m.groupValues[1], "UTF-8") }.getOrDefault("").trim()
    }

    private val DN_RE = Regex("[?&]dn=([^&]*)", RegexOption.IGNORE_CASE)

    private val XPE_RE = Regex("[?&]x\\.pe=([^&]+)", RegexOption.IGNORE_CASE)
    private val ESCAPE_RE = Regex("%([0-9A-Fa-f]{2})")
    private val LITERAL_RE = Regex("[0-9A-Fa-f:.]+")

    /** How many addresses one magnet may make the app try. No honest one needs more; a hostile one, unbounded, would be tried at every start. */
    private const val MAX_PEER_HINTS = 64

    /**
     * The addresses a magnet names as peers (`x.pe=host:port`; several, comma-separated
     * or repeated), the value read the way a URL is read — `%2C` is a comma, `%3A` a
     * colon ("+" stays what it is). The engine's own reading of a magnet decodes
     * nothing, so this can hand over MORE than the magnet add did — never fewer: an
     * encoded list must not lose the address the add had. A torrent restored from its
     * kept .torrent file has to be told them again: trackers are in that file, these
     * are not. Each address once, and only what can be one at all — digits, hex
     * letters, dots and colons, then a port 1..65535: a magnet is anyone's text, and
     * a name or a control character is left out here, not passed on to be logged.
     * Whether it really is an address is the engine's to say. At most
     * [MAX_PEER_HINTS], the first ones as listed — and nothing past them is even
     * read (the whole chain is lazy).
     */
    fun peerHints(magnet: String): List<Pair<String, Int>> =
        XPE_RE.findAll(magnet)
            .flatMap { unescape(it.groupValues[1]).split(',').asSequence() }
            .mapNotNull { entry ->
                val host = entry.substringBeforeLast(':', "").removeSurrounding("[", "]")
                val port = entry.substringAfterLast(':', "").toIntOrNull()
                if (host.isBlank() || !LITERAL_RE.matches(host) || port == null || port !in 1..65535) null else host to port
            }
            .distinct()
            .take(MAX_PEER_HINTS)
            .toList()

    /** Every `%XX` of the value as the character it stands for; anything else is left as it is. */
    private fun unescape(value: String): String =
        ESCAPE_RE.replace(value) { it.groupValues[1].toInt(16).toChar().toString() }

    /**
     * The torrent's name in THIS app — the key of its entry: 40 hex. It is the 20-byte name
     * every libtorrent client knows the torrent by: a v1 info-hash as written; a base32 one
     * decoded; and of a v2 info-hash — 64 hex, which a v2-ONLY magnet carries whole (BEP 52)
     * — its FIRST 20 BYTES.
     *
     * That is no loss. The engine reports the torrent by that same name (it is what the
     * library calls its best name, and what it looks a magnet's torrent up by — read from the
     * 2.1.0 binding), so an entry keyed by it lines up with everything the engine says, and
     * a v2-only torrent added by magnet gets the key it would have had from a .torrent file.
     * Until 1.4.16 the 64-hex form was no magnet to this app: a v2-only torrent could not be
     * added by magnet at all. Null for anything else.
     */
    fun infoHashHex(magnet: String): String? {
        val (raw, multihash) = xtOf(magnet) ?: return null
        val hex = raw.all { it.isDigit() || it in 'a'..'f' }
        return when {
            raw.length == 40 && hex -> raw
            raw.length == 64 && hex -> raw.take(40)
            // base32 is a spelling of the v1 family only (`btih`)
            raw.length == 32 && !multihash ->
                runCatching { base32Decode(raw).toHexString() }.getOrNull()
            else -> null
        }
    }

    /**
     * The magnet's `xt` value: the hash lower-cased as it stands for the torrent, and whether it
     * came as a multihash (`btmh`). A multihash names its length in bytes first (`1220` is a
     * 32-byte sha-256); that prefix is left off here, so a v2 hash is 64 hex however it was
     * spelled. Null without an `xt` of either family.
     */
    private fun xtOf(magnet: String): Pair<String, Boolean>? {
        val m = XT_RE.find(magnet) ?: return null
        val raw = m.groupValues[2].lowercase(Locale.ROOT)
        val multihash = m.groupValues[1].equals("mh", ignoreCase = true)
        return (if (multihash && raw.startsWith("1220")) raw.drop(4) else raw) to multihash
    }

    /** The magnet's hash as 64 hex when its xt carries a whole v2 one (either spelling); null otherwise. */
    private fun hexHashOf(magnet: String): String? =
        xtOf(magnet)?.first?.takeIf { HEX64.matches(it) }

    private val HEX64 = Regex("^[0-9a-f]{64}$")

    /**
     * Whether [magnet] names the torrent whose hashes these are — by its v1 info-hash (hex or
     * base32), or by its WHOLE v2 one. A 40-hex `btih` that is the first half of the v2 hash
     * does not count: read by any client it is a v1 info-hash, and names no torrent at all.
     */
    fun namesTorrent(magnet: String, v1Hash: String?, v2Hash: String?): Boolean {
        if (v2Hash != null && hexHashOf(magnet) == v2Hash.lowercase(Locale.ROOT)) return true
        val key = infoHashHex(magnet) ?: return false
        return v1Hash != null && key == v1Hash.lowercase(Locale.ROOT)
    }

    /**
     * The magnet that names a torrent at a restart (1.4.16): by the hash a site links it with —
     * its v1 info-hash when it has one (every hybrid torrent is linked that way, and every
     * client understands it), else its whole v2 hash. Null without either.
     *
     * Never from the 20-byte name alone (see [namesTorrent]). "magnet:?xt=urn:btih:" + the
     * engine's name is exactly what was saved for every torrent added from a .torrent file
     * until 1.4.15 — right for a v1 torrent, a magnet to no torrent for a v2-only or hybrid
     * one. It did no harm while the kept .torrent existed; without that it restored nothing.
     */
    fun magnetFor(v1Hash: String?, v2Hash: String?): String? =
        v1Hash?.lowercase(Locale.ROOT)?.takeIf { it.isNotEmpty() }?.let(::magnetOf)
            ?: v2Hash?.lowercase(Locale.ROOT)?.takeIf { it.isNotEmpty() }?.let(::magnetOf)

    fun magnetOf(hash: String): String = "magnet:?xt=urn:btih:$hash"

    /**
     * The magnet as THE ENGINE takes it (1.4.16). libtorrent parses a v2 info-hash in a magnet
     * only as a multihash — `xt=urn:btmh:1220<64 hex>` (BEP 52's other spelling; 0x12 is the
     * sha-256 code, 0x20 the length in bytes) — and answers "invalid info-hash" to the
     * widespread `urn:btih:<64 hex>`. Measured on libtorrent 2.1.1 with parse_magnet_uri, and
     * then through the app on the emulator: the confirmation accepted a v2-only magnet and the
     * engine refused the add. (The unit tests could not show it: their engine is a stand-in
     * that takes the magnet string. What a FORMAT really means is proven with the library
     * itself — or on the emulator.)
     *
     * So a v2 hash is respelled for the engine call only, whichever way the magnet had it.
     * The magnet the user has — and the one saved with the torrent, and the key the torrent is
     * listed by — stay as they are; the engine names the torrent by the same first 20 bytes
     * either way. Everything else in the magnet (name, trackers, peer addresses) is kept
     * exactly as written.
     */
    fun forEngine(magnet: String): String =
        XT_V2.replace(magnet) { m -> "xt=urn:btmh:1220" + m.groupValues[1].lowercase(Locale.ROOT) }

    /** A 64-hex hash after `btih:` or `btmh:` — not the first 64 of a longer value. */
    private val XT_V2 = Regex("""xt=urn:bt(?:ih|mh):([0-9A-Fa-f]{64})(?![0-9A-Fa-f])""", RegexOption.IGNORE_CASE)

    private fun base32Decode(s: String): ByteArray {
        var bits = 0
        var value = 0
        val out = ArrayList<Byte>(20)
        for (c in s.uppercase(Locale.ROOT)) {
            val idx = B32_ALPHABET.indexOf(c)
            require(idx >= 0) { "invalid base32 character: $c" }
            value = (value shl 5) or idx
            bits += 5
            if (bits >= 8) {
                bits -= 8
                out.add(((value shr bits) and 0xff).toByte())
            }
        }
        require(out.size == 20) { "base32 decoded to ${out.size} bytes, expected 20" }
        return out.toByteArray()
    }

    private fun ByteArray.toHexString(): String =
        joinToString("") { "%02x".format(it) }
}
