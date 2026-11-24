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
