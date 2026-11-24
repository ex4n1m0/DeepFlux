package space.deepflux.android

import java.io.File

/**
 * Small, valid v1 .torrent files for tests: a name, a few empty files, no
 * pieces. Enough for the engine fake (which reads them the way the real one
 * does — through [space.deepflux.android.engine.TorrentMeta]) and for a
 * restart to re-add them. [TestTorrentsTest] decodes the output with an
 * independent decoder: a malformed test torrent once made tests pass for
 * the wrong reason (the app's pre-parser is lenient).
 */
object TestTorrents {

    private fun str(s: String): String {
        val bytes = s.toByteArray(Charsets.UTF_8)
        return "${bytes.size}:" + s
    }

    fun bytes(name: String, vararg paths: String): ByteArray {
        val files = paths.joinToString("") { path ->
            "d" + str("length") + "i0e" + str("path") + "l" + path.split('/').joinToString("") { str(it) } + "ee"
        }
        // dictionary keys in bencode order
        val info = "d" + str("files") + "l" + files + "e" + str("name") + str(name) +
            str("piece length") + "i16384e" + str("pieces") + "0:" + "e"
        return ("d" + str("announce") + str("http://127.0.0.1:9/announce") + str("info") + info + "e").toByteArray(Charsets.UTF_8)
    }

    fun write(to: File, name: String, vararg paths: String): File {
        to.parentFile?.mkdirs()
        to.writeBytes(bytes(name, *paths))
        return to
    }
}
