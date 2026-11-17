package space.deepflux.android.dlmgr

import java.net.URL

/**
 * Minimal HLS (RFC 8216) playlist reader for the download engine — just
 * enough to turn a VOD stream into one file: pick the best variant of a
 * master playlist, then list the media playlist's segments with their
 * AES-128 key/IV and optional fMP4 init section.
 *
 * Deliberately NOT handled (each raises [Unsupported] with a message the
 * Downloads card shows verbatim, instead of producing a broken file):
 * live playlists, EXT-X-BYTERANGE, SAMPLE-AES/DRM, and variants whose
 * audio lives in a separate rendition (muxing two streams needs a muxer
 * this build does not have).
 */
object HlsPlaylist {

    class Unsupported(message: String) : Exception(message)

    /** AES-128 key reference; `iv == null` means "use the segment's sequence number". */
    class Key(val uri: String, val iv: ByteArray?)

    class Segment(val url: String, val key: Key?, val sequence: Long)

    class Media(val segments: List<Segment>, val initUrl: String?, val ended: Boolean)

    fun isPlaylist(text: String): Boolean = text.trimStart().startsWith("#EXTM3U")

    fun isMaster(text: String): Boolean = text.contains("#EXT-X-STREAM-INF")

    /** Highest-BANDWIDTH variant that carries its own audio. Returns its absolute URL. */
    fun pickVariant(text: String, baseUrl: String): String {
        val lines = text.lines().map { it.trim() }
        // Audio groups delivered as a SEPARATE playlist (they have a URI).
        val separateAudio = lines.filter { it.startsWith("#EXT-X-MEDIA:") }
            .map { attrs(it) }
            .filter { it["TYPE"] == "AUDIO" && !it["URI"].isNullOrBlank() }
            .mapNotNull { it["GROUP-ID"] }
            .toSet()

        var best: Pair<Long, String>? = null
        var sawVariant = false
        var i = 0
        while (i < lines.size) {
            val line = lines[i]
            if (line.startsWith("#EXT-X-STREAM-INF:")) {
                val a = attrs(line)
                // The URI is the next line that is neither blank nor a tag.
                var j = i + 1
                while (j < lines.size && (lines[j].isEmpty() || lines[j].startsWith("#"))) j++
                if (j < lines.size) {
                    sawVariant = true
                    val needsSeparateAudio = a["AUDIO"]?.let { it in separateAudio } ?: false
                    if (!needsSeparateAudio) {
                        val bw = a["BANDWIDTH"]?.toLongOrNull() ?: 0L
                        if (best == null || bw > best!!.first) best = bw to lines[j]
                    }
                }
                i = j
            }
            i++
        }
        val chosen = best ?: throw Unsupported(
            if (sawVariant) "audio is in a separate track — not supported yet"
            else "master playlist lists no streams"
        )
        return resolve(baseUrl, chosen.second)
    }

    fun parseMedia(text: String, baseUrl: String): Media {
        val segments = ArrayList<Segment>()
        var sequence = 0L
        var key: Key? = null
        var initUrl: String? = null
        var ended = false
        for (raw in text.lines()) {
            val line = raw.trim()
            when {
                line.isEmpty() -> {}
                line.startsWith("#EXT-X-MEDIA-SEQUENCE:") ->
                    sequence = line.substringAfter(':').trim().toLongOrNull() ?: 0L
                line.startsWith("#EXT-X-KEY:") -> {
                    val a = attrs(line)
                    key = when (val method = a["METHOD"] ?: "NONE") {
                        "NONE" -> null
                        "AES-128" -> Key(
                            resolve(baseUrl, a["URI"] ?: throw Unsupported("encrypted stream without a key URI")),
                            a["IV"]?.let { parseIv(it) },
                        )
                        else -> throw Unsupported("protected stream ($method) — cannot be downloaded")
                    }
                }
                line.startsWith("#EXT-X-MAP:") -> {
                    val a = attrs(line)
                    if (a.containsKey("BYTERANGE")) throw Unsupported("byte-range HLS — not supported yet")
                    initUrl = resolve(baseUrl, a["URI"] ?: throw Unsupported("init section without a URI"))
                }
                line.startsWith("#EXT-X-BYTERANGE") -> throw Unsupported("byte-range HLS — not supported yet")
                line.startsWith("#EXT-X-ENDLIST") -> ended = true
                line.startsWith("#") -> {} // every other tag is irrelevant to a plain download
                else -> {
                    segments.add(Segment(resolve(baseUrl, line), key, sequence))
                    sequence++
                }
            }
        }
        return Media(segments, initUrl, ended)
    }

    /** RFC 8216 §5.2: without an IV attribute, the IV is the big-endian sequence number. */
    fun sequenceIv(sequence: Long): ByteArray {
        val iv = ByteArray(16)
        for (i in 0 until 8) iv[15 - i] = (sequence ushr (8 * i)).toByte()
        return iv
    }

    private fun parseIv(value: String): ByteArray {
        val hex = value.removePrefix("0x").removePrefix("0X").padStart(32, '0')
        if (hex.length != 32) throw Unsupported("malformed encryption IV")
        return ByteArray(16) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    }

    private val ATTR = Regex("([A-Z0-9-]+)=(\"[^\"]*\"|[^,]*)")

    /** `#TAG:A=1,B="x,y"` → {A=1, B=x,y} (quotes stripped, commas inside quotes kept). */
    private fun attrs(line: String): Map<String, String> =
        ATTR.findAll(line.substringAfter(':')).associate {
            it.groupValues[1] to it.groupValues[2].trim().removeSurrounding("\"")
        }

    private fun resolve(base: String, ref: String): String = URL(URL(base), ref).toString()
}
