package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.engine.MagnetParser
import space.deepflux.android.engine.TorrentMeta
import java.security.MessageDigest

/**
 * 1.4.16 — a torrent's identity as the app reads it, checked against libtorrent itself.
 *
 * The engine names a torrent by 20 bytes: its v1 hash, or the FIRST 20 BYTES of its v2 one
 * (v2-only and hybrid torrents). Two things follow, and both went wrong before:
 *  * a v2-only magnet carries the WHOLE 64-hex v2 hash — it was "no magnet" to the app;
 *  * a magnet built from the 20-byte name alone is a magnet to no torrent for anything v2 —
 *    and that is what was saved for every torrent added from a .torrent file.
 *
 * The reference torrents ([RefTorrents]) were made by libtorrent 2.1.1; their hashes are the
 * library's, not this app's reader's.
 */
class TorrentIdentityV2Test {

    private val hybrid = RefTorrents.hybrid
    private val v2 = RefTorrents.v2

    private fun sha(algorithm: String, bytes: ByteArray) =
        MessageDigest.getInstance(algorithm).digest(bytes).joinToString("") { "%02x".format(it) }

    // ------------------------------------------------------------------ the app's own reader

    @Test
    fun aV1TorrentHasItsV1HashAndNoOther() {
        val info = TorrentMeta.readTorrent(RefTorrents.v1.bytes)!!
        assertEquals(RefTorrents.v1.v1, info.v1Hash)
        assertNull(info.v2Hash)
        assertEquals(RefTorrents.v1.libtorrentName, info.engineHash)
        assertEquals("Ref Pack", info.name)
    }

    @Test
    fun aHybridTorrentHasBothHashesAndIsNamedByTheFirstHalfOfItsV2One() {
        val info = TorrentMeta.readTorrent(hybrid.bytes)!!
        assertEquals(hybrid.v1, info.v1Hash)
        assertEquals(hybrid.v2, info.v2Hash)
        assertEquals("libtorrent's own name for it", hybrid.libtorrentName, info.engineHash)
        assertEquals("20 bytes are 40 hex characters", hybrid.v2!!.take(40), info.engineHash)
        assertEquals("the old accessor still answers the v1 hash", hybrid.v1, info.infoHash)
    }

    @Test
    fun aV2OnlyTorrentHasNoV1HashAndIsNamedByTheFirstHalfOfItsV2One() {
        val info = TorrentMeta.readTorrent(v2.bytes)!!
        assertNull("a v2-only torrent has no v1 info-hash", info.v1Hash)
        assertEquals(v2.v2, info.v2Hash)
        assertEquals(v2.libtorrentName, info.engineHash)
        assertEquals(
            "for a v2-only torrent the old accessor is the engine's name — until 1.4.15 it was the SHA-1 of the info dict, a value that names no torrent",
            v2.engineHash,
            info.infoHash,
        )
    }

    @Test
    fun theV2HashIsOfTheInfoDictionaryAlone_AndPieceLayersLiveOutsideIt() {
        // BEP 52: `piece layers` is a key of the metainfo's TOP dictionary, after `info` — not
        // inside it. So the v2 info-hash is the SHA-256 of the info dictionary as bencoded, with
        // nothing left out, and the layers are outside every info-hash.
        for ((label, ref) in listOf("v2-only" to RefTorrents.v2, "hybrid" to RefTorrents.hybrid)) {
            val bytes = ref.bytes
            val text = String(bytes, Charsets.ISO_8859_1)
            val infoStart = text.indexOf("4:info") + "4:info".length
            val layersKey = text.indexOf("12:piece layers")
            assertTrue("$label: the layers come after the info dictionary", layersKey > infoStart)
            // The info dictionary ends right before that top-level key: "...i<piece length>e" "e" "12:piece layers".
            val infoEnd = layersKey - 1                       // the dictionary's closing `e`
            assertEquals("d", text.substring(infoStart, infoStart + 1))
            assertEquals("e", text.substring(infoEnd, infoEnd + 1))
            val infoDictionary = bytes.copyOfRange(infoStart, infoEnd + 1)
            assertFalse("the layers are not inside the info dictionary", String(infoDictionary, Charsets.ISO_8859_1).contains("piece layers"))

            val info = TorrentMeta.readTorrent(bytes)!!
            assertEquals("$label: v2 = SHA-256 of the info dictionary, as it is", ref.v2, info.v2Hash)
            assertEquals(ref.v2, sha("SHA-256", infoDictionary))
            // Not of everything to the end of the file, layers included.
            assertNotEquals(ref.v2, sha("SHA-256", bytes.copyOfRange(infoStart, bytes.size - 1)))
        }
    }

    @Test
    fun theOldKindIsStillReadAsBefore() {
        // The small v1 torrents the other tests are made of: v1 hash as it always was, no v2.
        val bytes = TestTorrents.bytes("One", "a.bin")
        val info = TorrentMeta.readTorrent(bytes)!!
        val text = String(bytes, Charsets.ISO_8859_1)
        val from = text.indexOf("4:info") + "4:info".length
        assertEquals(sha("SHA-1", bytes.copyOfRange(from, bytes.size - 1)), info.v1Hash)
        assertNull(info.v2Hash)
        assertEquals(info.v1Hash, info.engineHash)
    }

    // ------------------------------------------------------------------ magnets

    @Test
    fun aV2OnlyMagnetIsAcceptedAndKnownByTheEnginesNameForItsTorrent() {
        assertEquals(v2.engineHash, MagnetParser.infoHashHex(v2.siteMagnet + "&dn=V2Only"))
        assertEquals("some sites write it upper-case", v2.engineHash, MagnetParser.infoHashHex("magnet:?xt=urn:btih:${v2.v2!!.uppercase()}"))
        assertTrue(MagnetParser.extractMagnet("grab this ${v2.siteMagnet}&dn=x now")!!.endsWith("&dn=x"))
    }

    @Test
    fun aBtihThatIsNotWholeIsNoMagnet() {
        val h = v2.v2!!
        assertNull("63 characters", MagnetParser.infoHashHex("magnet:?xt=urn:btih:${h.dropLast(1)}"))
        assertNull("65 characters", MagnetParser.infoHashHex("magnet:?xt=urn:btih:${h}0"))
        assertNull("not hex", MagnetParser.infoHashHex("magnet:?xt=urn:btih:z${h.drop(1)}"))
        assertNull("an empty one", MagnetParser.infoHashHex("magnet:?xt=urn:btih:"))
    }

    @Test
    fun theOtherSpellingOfAV2MagnetIsAcceptedToo() {
        // BEP 52's multihash spelling, as some clients write it.
        val full = v2.v2!!
        assertEquals(v2.engineHash, MagnetParser.infoHashHex("magnet:?xt=urn:btmh:1220$full&dn=x"))
        assertEquals("without the length prefix", v2.engineHash, MagnetParser.infoHashHex("magnet:?xt=urn:btmh:$full"))
        assertTrue(MagnetParser.extractMagnet("grab magnet:?xt=urn:btmh:1220$full&dn=x now")!!.contains("btmh:1220$full"))
        assertTrue(MagnetParser.namesTorrent("magnet:?xt=urn:btmh:1220$full", null, full))
        assertNull(
            "a 32-character value is no base32 when it comes as a multihash",
            MagnetParser.infoHashHex("magnet:?xt=urn:btmh:MS4HZC6OYLQYJJFXCI2VJRY4UGFXWIMG"),
        )
    }

    @Test
    fun theEngineGetsAV2MagnetInTheOnlySpellingItParses() {
        // libtorrent's parse_magnet_uri answers "invalid info-hash" to `urn:btih:<64 hex>` and
        // takes `urn:btmh:1220<64 hex>` (measured on 2.1.1; then through the app on the
        // emulator). The app keeps the user's magnet and respells it for the engine call only.
        val full = v2.v2!!
        assertEquals(
            "magnet:?xt=urn:btmh:1220$full&dn=V2Only&x.pe=10.0.2.2:47644",
            MagnetParser.forEngine("magnet:?xt=urn:btih:$full&dn=V2Only&x.pe=10.0.2.2:47644"),
        )
        assertEquals(
            "upper-case, as some sites write it",
            "magnet:?xt=urn:btmh:1220$full&dn=x",
            MagnetParser.forEngine("magnet:?xt=urn:btih:${full.uppercase()}&dn=x"),
        )
        assertEquals(
            "parameters in another order keep their place",
            "magnet:?dn=x&xt=urn:btmh:1220$full",
            MagnetParser.forEngine("magnet:?dn=x&xt=urn:btih:$full"),
        )
        assertEquals("the multihash spelling gets its length prefix", "magnet:?xt=urn:btmh:1220$full", MagnetParser.forEngine("magnet:?xt=urn:btmh:$full"))
        for (untouched in listOf(
            RefTorrents.v1.siteMagnet + "&dn=ubuntu",
            "magnet:?xt=urn:btih:MS4HZC6OYLQYJJFXCI2VJRY4UGFXWIMG",
            "magnet:?xt=urn:btmh:1220$full",
            "magnet:?xt=urn:btih:${full}0",
            "not a magnet",
        )) {
            assertEquals(untouched, MagnetParser.forEngine(untouched))
        }
        assertEquals("the spelling changes nothing about the key", v2.engineHash, MagnetParser.infoHashHex(MagnetParser.forEngine(v2.siteMagnet + "&dn=x")))
    }

    @Test
    fun aMagnetNamesATorrentByAWholeHashNeverByTheFirstHalfOfItsV2One() {
        val (v1, full) = hybrid.v1!! to hybrid.v2!!
        assertTrue(MagnetParser.namesTorrent("magnet:?xt=urn:btih:$v1&tr=udp%3A%2F%2Ftracker", v1, full))
        assertTrue(MagnetParser.namesTorrent("magnet:?xt=urn:btih:$full", v1, full))
        assertFalse("the engine's 20-byte name, written as a btih, is a magnet to NO torrent", MagnetParser.namesTorrent("magnet:?xt=urn:btih:${full.take(40)}", v1, full))
        assertFalse("another torrent's v1 hash", MagnetParser.namesTorrent(RefTorrents.v1.siteMagnet, v1, full))
        assertFalse(MagnetParser.namesTorrent("not a magnet", v1, full))

        val only = v2.v2!!
        assertTrue(MagnetParser.namesTorrent("magnet:?xt=urn:btih:$only", null, only))
        assertFalse(MagnetParser.namesTorrent("magnet:?xt=urn:btih:${only.take(40)}", null, only))
    }

    @Test
    fun theMagnetSavedForATorrentIsBuiltFromAHashThatNamesIt() {
        assertEquals("a hybrid torrent by its v1 hash — the form every client understands", "magnet:?xt=urn:btih:${hybrid.v1}", MagnetParser.magnetFor(hybrid.v1, hybrid.v2))
        assertEquals("a v2-only torrent by its whole v2 hash", "magnet:?xt=urn:btih:${v2.v2}", MagnetParser.magnetFor(null, v2.v2))
        assertEquals("magnet:?xt=urn:btih:${RefTorrents.v1.v1}", MagnetParser.magnetFor(RefTorrents.v1.v1, null))
        assertEquals("written upper-case somewhere, saved lower-case", "magnet:?xt=urn:btih:${hybrid.v1}", MagnetParser.magnetFor(hybrid.v1!!.uppercase(), hybrid.v2!!.uppercase()))
        assertNull(MagnetParser.magnetFor(null, null))
        assertNull(MagnetParser.magnetFor("", ""))
    }
}
