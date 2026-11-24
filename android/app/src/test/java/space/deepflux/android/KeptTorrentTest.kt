package space.deepflux.android

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.IndependentBencode.Value
import space.deepflux.android.engine.KeptTorrent
import space.deepflux.android.engine.TorrentMeta

/**
 * 1.4.14 — the .torrent the app keeps for a torrent added by magnet, once
 * its metadata has arrived.
 *
 * The first writer of this file lived inside the engine, where no unit test
 * can run it. It left out the dictionary's opening mark; every test was
 * green; the file was no torrent. These decode what is written with a
 * bencoder written from the spec, from nothing in the app.
 */
class KeptTorrentTest {

    private fun sectionOf(torrent: ByteArray): ByteArray {
        val (top, end) = IndependentBencode.decode(torrent)
        assertEquals("the test torrent is one value", torrent.size, end)
        return IndependentBencode.encode(top.at("info"))
    }

    private val threeFiles = TestTorrents.bytes("Clean Name", "README", "Season 1/ep1.mkv", "pack/data.zip")

    @Test
    fun theFileIsOneDictionaryHoldingTheInfoDictVerbatim() {
        val section = sectionOf(threeFiles)
        val bytes = KeptTorrent.bytes(
            trackersByTier = listOf(
                listOf("udp://tracker.example:80/announce", "http://tracker.example/announce"),
                listOf("udp://backup.example/announce"),
            ),
            webSeeds = listOf("http://seeds.example/Clean%20Name.zip"),
            infoSection = section,
        )

        val (top, end) = IndependentBencode.decode(bytes)

        assertEquals("the whole file is that one value — nothing before it, nothing after", bytes.size, end)
        assertEquals(setOf("announce", "announce-list", "info", "url-list"), top.keyTexts())
        assertEquals("udp://tracker.example:80/announce", (top.at("announce") as Value.Bytes).text())
        // The info dict exactly as the engine has it: the decoder parses it (it is
        // a dictionary), so the bytes themselves are looked at — the stretch of
        // the file that ends just before its final mark.
        assertTrue(top.at("info") is Value.Dict)
        assertEquals('e'.code, bytes.last().toInt())
        assertArrayEquals(
            "the info dict exactly as the engine has it",
            section,
            bytes.copyOfRange(bytes.size - 1 - section.size, bytes.size - 1),
        )
        val tiers = (top.at("announce-list") as Value.ListOf).items.map { tier ->
            (tier as Value.ListOf).items.map { (it as Value.Bytes).text() }
        }
        assertEquals(
            listOf(
                listOf("udp://tracker.example:80/announce", "http://tracker.example/announce"),
                listOf("udp://backup.example/announce"),
            ),
            tiers,
        )
        assertEquals(
            listOf("http://seeds.example/Clean%20Name.zip"),
            (top.at("url-list") as Value.ListOf).items.map { (it as Value.Bytes).text() },
        )
    }

    @Test
    fun theAppReadsItAsTheSameTorrent() {
        val original = TestTorrents.bytes("Clean Name", "README")
        val expected = TorrentMeta.readTorrent(original)!!

        val kept = TorrentMeta.readTorrent(KeptTorrent.bytes(emptyList(), emptyList(), sectionOf(original)))!!

        assertEquals(expected.infoHash, kept.infoHash)
        assertEquals("Clean Name", kept.name)
    }

    @Test
    fun withNothingElseThereIsOnlyTheInfo() {
        val bytes = KeptTorrent.bytes(listOf(emptyList()), emptyList(), sectionOf(threeFiles))
        val (top, end) = IndependentBencode.decode(bytes)
        assertEquals(bytes.size, end)
        assertEquals(setOf("info"), top.keyTexts())
    }

    @Test
    fun anAddressIsTakenAsItIs() {
        // Whatever comes out of a tracker list or a magnet: bencode's own marks,
        // colons and digits, non-Latin scripts. Lengths are in bytes, not characters.
        val odd = listOf(
            "udp://d4:infoe.example/announce",
            "http://tracker.example/12:x",
            "udp://种子.example/announce",
            "http://a.example/c?d=e&f=g",
        )
        val bytes = KeptTorrent.bytes(listOf(odd), listOf(odd.last()), sectionOf(threeFiles))
        val (top, end) = IndependentBencode.decode(bytes)
        assertEquals(bytes.size, end)
        val listed = (top.at("announce-list") as Value.ListOf).items.single().let { it as Value.ListOf }.items.map { (it as Value.Bytes).text() }
        assertEquals(odd, listed)
        assertEquals(odd.first(), (top.at("announce") as Value.Bytes).text())
        assertEquals(odd.last(), (top.at("url-list") as Value.ListOf).items.map { (it as Value.Bytes).text() }.single())
    }

    @Test
    fun theSameAddressIsListedOnce() {
        val one = "udp://tracker.example/announce"
        val bytes = KeptTorrent.bytes(listOf(listOf(one, one), listOf(one)), listOf(one, one), sectionOf(threeFiles))
        val (top, _) = IndependentBencode.decode(bytes)
        val tiers = (top.at("announce-list") as Value.ListOf).items.map { it as Value.ListOf }.map { it.items.size }
        assertEquals(listOf(1, 1), tiers)
        assertEquals(1, (top.at("url-list") as Value.ListOf).items.size)
    }
}
