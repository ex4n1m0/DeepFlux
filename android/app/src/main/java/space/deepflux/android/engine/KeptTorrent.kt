package space.deepflux.android.engine

import java.io.ByteArrayOutputStream

/**
 * The bytes of the .torrent the app keeps for a torrent that was added by
 * magnet, once its metadata has arrived: the trackers as the torrent itself
 * would list them, its web seeds, and the info dict exactly as the engine
 * has it.
 *
 * Pure — no engine, no Android — so a test decodes what this writes with a
 * decoder of its own ([space.deepflux.android.KeptTorrentTest]). That
 * matters: the first version of this lived inside the engine, where no unit
 * test can run it, left out the dictionary's opening mark, and every test
 * stayed green while the file was no torrent at all (2026-11-25).
 */
object KeptTorrent {

    /**
     * [trackersByTier] in tier order, each tier's trackers once; [webSeeds]
     * once each; [infoSection] is the bencoded info dict, byte for byte.
     */
    fun bytes(trackersByTier: List<List<String>>, webSeeds: List<String>, infoSection: ByteArray): ByteArray {
        val out = ByteArrayOutputStream()
        fun string(value: String) {
            val bytes = value.toByteArray(Charsets.UTF_8)
            out.write(bytes.size.toString().toByteArray())
            out.write(':'.code)
            out.write(bytes)
        }
        fun list(values: List<String>) {
            out.write('l'.code)
            values.forEach(::string)
            out.write('e'.code)
        }

        out.write('d'.code) // the dictionary's opening mark
        val tiers = trackersByTier.map { it.distinct() }.filter { it.isNotEmpty() }
        if (tiers.isNotEmpty()) {
            string("announce")
            string(tiers.first().first())
            string("announce-list")
            out.write('l'.code)
            tiers.forEach(::list)
            out.write('e'.code)
        }
        if (webSeeds.isNotEmpty()) {
            string("url-list")
            list(webSeeds.distinct())
        }
        string("info")
        out.write(infoSection)
        out.write('e'.code)
        return out.toByteArray()
    }
}
