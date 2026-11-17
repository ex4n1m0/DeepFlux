package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import space.deepflux.android.engine.MagnetParser

/** P8 (1.4.5): magnet intake is the app's first parsing of untrusted text. */
class MagnetParserTest {

    private val hexHash = "0123456789abcdef0123456789abcdef01234567"

    @Test
    fun extractsMagnetFromSurroundingText() {
        val magnet = "magnet:?xt=urn:btih:$hexHash&dn=ubuntu"
        assertEquals(magnet, MagnetParser.extractMagnet("here, grab this $magnet thanks"))
    }

    @Test
    fun extractIgnoresPlainText() {
        assertNull(MagnetParser.extractMagnet("no magnets here"))
        assertNull(MagnetParser.extractMagnet("magnet:?xt=urn:sha1:deadbeef")) // not btih
    }

    @Test
    fun hexHashLowercased() {
        assertEquals(
            hexHash,
            MagnetParser.infoHashHex("magnet:?xt=urn:btih:${hexHash.uppercase()}&dn=x"),
        )
    }

    @Test
    fun base32HashDecodesToHex() {
        // Roundtrip against an independent RFC 4648 base32 encoder written
        // here — no hand-computed vector to rot.
        val b32 = base32Encode(hexHash.chunked(2).map { it.toInt(16).toByte() }.toByteArray())
        assertEquals(32, b32.length)
        assertEquals(hexHash, MagnetParser.infoHashHex("magnet:?xt=urn:btih:$b32"))
    }

    private fun base32Encode(data: ByteArray): String {
        val alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        val sb = StringBuilder()
        var bits = 0
        var value = 0
        for (b in data) {
            value = (value shl 8) or (b.toInt() and 0xff)
            bits += 8
            while (bits >= 5) {
                bits -= 5
                sb.append(alphabet[(value shr bits) and 0x1f])
            }
        }
        if (bits > 0) sb.append(alphabet[(value shl (5 - bits)) and 0x1f])
        return sb.toString()
    }

    @Test
    fun rejectsBadLengths() {
        assertNull(MagnetParser.infoHashHex("magnet:?xt=urn:btih:0123456789")) // 10 chars
        assertNull(MagnetParser.infoHashHex("not a magnet"))
    }
}
