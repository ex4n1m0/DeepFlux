package space.deepflux.android.engine

import java.net.URLDecoder
import java.util.Locale

/** Extracts the v1 info-hash from a magnet URI (40-char hex or 32-char base32). */
object MagnetParser {

    private const val B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    private val XT_RE = Regex("xt=urn:btih:([A-Za-z0-9]+)", RegexOption.IGNORE_CASE)
    private val MAGNET_RE = Regex("magnet:\\?xt=urn:btih:[A-Za-z0-9]+[^\\s]*", RegexOption.IGNORE_CASE)

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

    fun infoHashHex(magnet: String): String? {
        val raw = XT_RE.find(magnet)?.groupValues?.get(1) ?: return null
        return when {
            raw.length == 40 && raw.all { it.isDigit() || it.lowercaseChar() in 'a'..'f' } ->
                raw.lowercase(Locale.ROOT)
            raw.length == 32 ->
                runCatching { base32Decode(raw).toHexString() }.getOrNull()
            else -> null
        }
    }

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
