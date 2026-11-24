package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.engine.TorrentMeta
import java.io.ByteArrayOutputStream
import java.security.MessageDigest

/**
 * The test torrents themselves. A malformed one once let three tests pass
 * for the wrong reason — the app's pre-parser accepted the broken structure
 * and reported a name and a (bogus) hash — and then failed a fourth test
 * with a bare null. These tests decode the helper's output independently.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class TestTorrentsTest {

    /** An independent bencoder/decoder — written from the spec, not from the app's code. */
    private fun encode(value: Any): ByteArray = when (value) {
        is Int -> "i${value}e".toByteArray()
        is ByteArray -> ByteArrayOutputStream().apply { write(value.size.toString().toByteArray()); write(':'.code); write(value) }.toByteArray()
        is List<*> -> ByteArrayOutputStream().apply { write('l'.code); value.forEach { write(encode(it!!)) }; write('e'.code) }.toByteArray()
        is Map<*, *> -> {
            // bencode order: keys as raw bytes, plain lexicographic (not by length)
            val sorted = value.entries.sortedBy { (it.key as ByteArray).toString(Charsets.ISO_8859_1) }
            ByteArrayOutputStream().apply {
                write('d'.code)
                sorted.forEach { e -> write(encode(e.key as ByteArray)); write(encode(e.value!!)) }
                write('e'.code)
            }.toByteArray()
        }
        is String -> encode(value.toByteArray())
        else -> throw IllegalArgumentException()
    }.let { it }

    private class Cursor(val b: ByteArray) {
        var pos = 0
        fun decode(): Any {
            return when (b[pos].toInt().toChar()) {
                'i' -> { pos++; val end = indexOf('e'); val v = String(b, pos, end - pos).toInt(); pos = end + 1; v }
                'l' -> { pos++; val out = ArrayList<Any>(); while (b[pos].toInt().toChar() != 'e') out.add(decode()); pos++; out }
                'd' -> {
                    pos++
                    val out = LinkedHashMap<ByteArray, Any>()
                    while (b[pos].toInt().toChar() != 'e') {
                        val k = decode() as ByteArray
                        out[k] = decode()
                    }
                    pos++
                    out
                }
                else -> { val colon = indexOf(':'); val len = String(b, pos, colon - pos).toInt(); pos = colon + 1; b.copyOfRange(pos, pos + len).also { pos += len } }
            }
        }

        private fun indexOf(c: Char): Int { var i = pos; while (b[i].toInt().toChar() != c) i++; return i }
    }

    /** A map keyed by byte arrays: `ByteArray` compares by identity, so look entries up by content. */
    private fun Map<ByteArray, Any>.at(key: String): Any =
        entries.first { it.key.contentEquals(key.toByteArray()) }.value

    @Test
    fun theTorrentIsWellFormedAndSaysWhatWasAsked() {
        for (paths in listOf(listOf("README"), listOf("README", "link.txt"), listOf("Season 1", "ep1.mkv", "pack/data.zip"))) {
            val bytes = TestTorrents.bytes("Clean Name", *paths.toTypedArray())
            val cursor = Cursor(bytes)
            val top = cursor.decode() as Map<ByteArray, Any>
            assertEquals("the whole file is one value", bytes.size, cursor.pos)
            assertEquals(setOf("announce", "info"), top.keys.map { it.toString(Charsets.ISO_8859_1) }.toSet())
            val info = top.at("info") as Map<ByteArray, Any>
            assertEquals("Clean Name", (info.at("name") as ByteArray).toString(Charsets.UTF_8))
            val files = info.at("files") as List<*>
            assertEquals(paths, files.map { f -> ((f as Map<ByteArray, Any>).at("path") as List<*>).joinToString("/") { (it as ByteArray).toString(Charsets.UTF_8) } })
            // encoded back with the independent encoder, byte for byte
            assertEquals(String(bytes, Charsets.ISO_8859_1), String(encode(top), Charsets.ISO_8859_1))
        }
    }

    @Test
    fun theAppReadsTheSameNameAndTheRealInfoHash() {
        val bytes = TestTorrents.bytes("Clean Name", "README", "link.txt")
        val top = Cursor(bytes).decode() as Map<ByteArray, Any>
        val infoBytes = encode(top.at("info"))
        val expectedHash = MessageDigest.getInstance("SHA-1").digest(infoBytes).joinToString("") { "%02x".format(it) }

        val info = TorrentMeta.readTorrent(bytes)!!

        assertEquals("Clean Name", info.name)
        assertEquals(expectedHash, info.infoHash)
    }

    @Test
    fun twoDifferentTorrentsHaveDifferentHashes() {
        val a = TorrentMeta.readTorrent(TestTorrents.bytes("One", "a.bin"))!!
        val b = TorrentMeta.readTorrent(TestTorrents.bytes("Two", "a.bin"))!!
        assertTrue(a.infoHash != b.infoHash)
        assertNull("not a torrent", TorrentMeta.readTorrent("garbage".toByteArray()))
    }
}
