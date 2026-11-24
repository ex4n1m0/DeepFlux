package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.engine.MagnetParser

/** P8 (1.4.5): magnet intake is the app's first parsing of untrusted text. */
class MagnetParserTest {

    private val hexHash = "0123456789abcdef0123456789abcdef01234567"

    @Test
    fun peerAddressesOfAMagnetAreParsedAndOnlyWellFormedOnes() {
        val hints = MagnetParser.peerHints(
            "magnet:?xt=urn:btih:$hexHash&x.pe=10.0.2.2:47632,192.168.1.4:0,[::1]:51413,192.168.1.5:70000" +
                "&dn=x&x.pe=10.0.2.2:47632",
        )
        assertEquals("a port outside 1..65535 is no address, a repeat is dropped", listOf("10.0.2.2" to 47632, "::1" to 51413), hints)
        assertTrue(MagnetParser.peerHints("magnet:?xt=urn:btih:$hexHash&dn=x").isEmpty())
    }

    @Test
    fun anEscapedCommaOrColonInThePeerAddressesIsReadAsThatCharacter() {
        // Some makers of magnets URL-encode the value: `a:1%2Cb:2` is a list. Read raw it is one junk
        // address — and the torrent restored from its kept file lost the address the magnet add had
        // used (libtorrent 2.1's own reading takes the first of a raw list and decodes nothing;
        // measured 2026-11-25).
        val hints = MagnetParser.peerHints(
            "magnet:?xt=urn:btih:$hexHash&x.pe=192.168.1.6%3A6881%2C192.168.1.7%3A6881&x.pe=10.0.2.2%3A47633",
        )
        assertEquals(listOf("192.168.1.6" to 6881, "192.168.1.7" to 6881, "10.0.2.2" to 47633), hints)
        // A lower-case escape is one too; what it decodes to is still held to the same rules.
        assertEquals(
            listOf("10.0.2.2" to 47632),
            MagnetParser.peerHints("magnet:?xt=urn:btih:$hexHash&x.pe=10.0.2.2:47632%2c192.168.1.4%3A0"),
        )
    }

    @Test
    fun aNameOrAControlCharacterIsNoPeerAddress() {
        // A magnet is anyone's text: what cannot be a literal address is left out here, so the engine
        // is never asked about it and it never reaches a log line (an escaped line break included).
        assertEquals(
            listOf("10.0.2.2" to 47632),
            MagnetParser.peerHints("magnet:?xt=urn:btih:$hexHash&x.pe=example.com:80,10.0.2.2%0A:47632,10.0.2.2:47632"),
        )
    }

    @Test
    fun noMagnetMakesTheAppTryMoreAddressesThanAnyHonestOneNeeds() {
        // A magnet is anyone's text, and the addresses are handed to the engine at every start.
        val many = (1..300).joinToString(",") { "10.0.2.$it:6881" }
        val hints = MagnetParser.peerHints("magnet:?xt=urn:btih:$hexHash&x.pe=$many")

        assertEquals(64, hints.size)
        assertEquals("the first ones, as listed", listOf("10.0.2.1" to 6881, "10.0.2.64" to 6881), listOf(hints.first(), hints.last()))
    }

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
