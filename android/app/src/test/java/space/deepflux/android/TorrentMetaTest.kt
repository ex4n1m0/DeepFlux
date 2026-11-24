package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.browser.Adblock
import space.deepflux.android.engine.MagnetParser
import space.deepflux.android.engine.TorrentMeta
import java.security.MessageDigest

/** Pure-JVM helpers from the fix batch (2026-11-23). */

/** The .torrent reader behind the torrent-add confirmation dialog. */
class TorrentMetaTest {

    private fun str(s: String): String {
        val bytes = s.toByteArray(Charsets.UTF_8)
        return "${bytes.size}:$s"
    }

    private fun torrent(name: String = "Ubuntu Test ISO"): String = "d" +
        str("announce") + str("udp://tracker.example:80") +
        str("info") + "d" +
        str("length") + "i1024e" +
        str("name") + str(name) +
        str("piece length") + "i16384e" +
        str("pieces") + "20:" + "A".repeat(20) +
        "e" +
        str("creation date") + "i1700000000e" +
        "e"

    /** The info dictionary, located by structure (independent of the reader). */
    private fun infoSlice(text: String): String {
        val start = text.indexOf("4:info") + "4:info".length
        val end = text.indexOf("13:creation date")
        return text.substring(start, end)
    }

    @Test
    fun readsNameAndInfoHash() {
        val text = torrent()
        val info = TorrentMeta.readTorrent(text.toByteArray())!!
        assertEquals("Ubuntu Test ISO", info.name)
        val expected = MessageDigest.getInstance("SHA-1").digest(infoSlice(text).toByteArray())
            .joinToString("") { "%02x".format(it) }
        assertEquals(expected, info.infoHash)
        assertEquals(40, info.infoHash.length)
    }

    @Test
    fun readsUtf8NamesAndNestedFileLists() {
        val text = "d" + str("info") + "d" +
            str("files") + "l" + "d" + str("length") + "i5e" + str("path") + "l" + str("a") + str("b.bin") + "e" + "e" + "e" +
            str("name") + str("测试 种子") +
            "e" + str("announce") + str("udp://x") + "e"
        assertEquals("测试 种子", TorrentMeta.readTorrent(text.toByteArray())!!.name)
    }

    @Test
    fun failClosedOnMalformedInput() {
        val good = torrent()
        assertNull("empty", TorrentMeta.readTorrent(ByteArray(0)))
        assertNull("not bencode", TorrentMeta.readTorrent("<html>login page</html>".toByteArray()))
        assertNull("top-level list", TorrentMeta.readTorrent("l4:infoe".toByteArray()))
        assertNull("no info dict", TorrentMeta.readTorrent(("d" + str("announce") + str("udp://x") + "e").toByteArray()))
        assertNull("info is not a dict", TorrentMeta.readTorrent(("d" + str("info") + "i5e" + "e").toByteArray()))
        assertNull("no name", TorrentMeta.readTorrent(("d" + str("info") + "d" + str("length") + "i1e" + "e" + "e").toByteArray()))
        // Truncated inside the info dict.
        assertNull("truncated", TorrentMeta.readTorrent(good.dropLast(40).toByteArray()))
        // A string length that runs past the end of the file.
        assertNull("overlong string", TorrentMeta.readTorrent(("d" + str("info") + "d" + str("name") + "999:x").toByteArray()))
        // Leading-zero / non-decimal lengths are not bencode.
        assertNull("leading zero length", TorrentMeta.readTorrent(("d" + str("info") + "d4:name05:abc").toByteArray()))
    }

    @Test
    fun infoHashChangesWithTheInfoDict() {
        val a = TorrentMeta.readTorrent(torrent("A").toByteArray())!!.infoHash
        val b = TorrentMeta.readTorrent(torrent("B").toByteArray())!!.infoHash
        assertNotEquals(a, b)
    }
}

/** Adblock never blanks a whole site and never breaks layout. */
class AdblockPolicyTest {

    @Test
    fun subResourcesOnListedHostsAreBlocked() {
        assertTrue(Adblock.shouldBlock(false, "matomo.org", "https://matomo.org/matomo.js"))
        assertTrue(Adblock.shouldBlock(false, "stats.matomo.org", "https://stats.matomo.org/piwik.php"))
        assertTrue(Adblock.shouldBlock(false, "doubleclick.net", "https://doubleclick.net/x.js"))
    }

    @Test
    fun mainFrameIsNeverBlocked() {
        assertFalse(Adblock.shouldBlock(true, "matomo.org", "https://matomo.org/"))
        assertFalse(Adblock.shouldBlock(true, "sentry.io", "https://sentry.io/welcome/"))
        assertFalse(Adblock.shouldBlock(true, "doubleclick.net", "https://doubleclick.net/"))
    }

    @Test
    fun stylesheetsAndFontsAreNeverBlocked() {
        for (ext in listOf(".css", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".CSS", ".css?v=3")) {
            assertFalse(ext, Adblock.shouldBlock(false, "doubleclick.net", "https://doubleclick.net/static/a$ext"))
        }
        assertFalse(Adblock.shouldBlock(false, "doubleclick.net", "https://doubleclick.net/a.css#x"))
    }

    @Test
    fun unlistedHostsAreUntouched() {
        assertFalse(Adblock.shouldBlock(false, "example.com", "https://example.com/ad.js"))
        assertFalse(Adblock.shouldBlock(false, "", "https://example.com/"))
    }
}

/** The magnet's `dn=` display name for the confirmation dialog. */
class MagnetDisplayNameTest {

    private val base = "magnet:?xt=urn:btih:0000000000000000000000000000000000000000"

    @Test
    fun decodesPlusAndPercentEscapes() {
        assertEquals("Ubuntu 24.04", MagnetParser.displayName("$base&dn=Ubuntu+24.04"))
        assertEquals("测试 电影", MagnetParser.displayName("$base&dn=%E6%B5%8B%E8%AF%95+%E7%94%B5%E5%BD%B1"))
        assertEquals("A & B", MagnetParser.displayName("$base&dn=A%20%26%20B"))
    }

    @Test
    fun blankWhenAbsentOrBroken() {
        assertEquals("", MagnetParser.displayName(base))
        assertEquals("", MagnetParser.displayName("$base&dn=%zz"))
        assertEquals("", MagnetParser.displayName("not a magnet"))
    }
}
