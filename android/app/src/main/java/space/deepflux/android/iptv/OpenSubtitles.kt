package space.deepflux.android.iptv

import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.io.RandomAccessFile
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * OpenSubtitles.com REST v1 client (plan §13 #8) — the Android port of the
 * desktop's iptv/opensubtitles.py: search (by the classic file hash for
 * exact matches, falling back to a title query) → download (POST /download
 * for a short-lived link, then a plain GET). The consumer Api-Key comes
 * from Settings (nothing is embedded — same stance as the desktop).
 */
object OpenSubtitles {

    class OSException(message: String) : Exception(message)

    data class SubtitleResult(
        val fileId: Long,
        val release: String,
        val language: String,
        val downloads: Int,
        val rating: Double,
        val title: String,
        val year: String,
        val hashMatch: Boolean,
    )

    private const val API_BASE = "https://api.opensubtitles.com/api/v1"
    private const val TIMEOUT = 20_000
    private fun ua(): String = "DeepFlux v${space.deepflux.android.BuildConfig.VERSION_NAME}"

    /** The classic OpenSubtitles movie hash: size + 64-bit LE sums of the first/last 64 KiB. */
    fun opensubtitlesHash(path: String): String {
        val f = File(path)
        if (!f.isFile || f.length() < 64 * 1024) throw OSException("file too small to hash")
        fun sum64(rf: RandomAccessFile, start: Long): ULong {
            rf.seek(start)
            var total = 0UL
            val buf = ByteArray(8)
            repeat(64 * 1024 / 8) {
                if (rf.read(buf) != 8) throw OSException("short read while hashing")
                var v = 0UL
                for (i in 7 downTo 0) v = (v shl 8) or buf[i].toUByte().toULong()
                total += v
            }
            return total
        }
        RandomAccessFile(f, "r").use { rf ->
            var h = f.length().toULong() + sum64(rf, 0)
            h += sum64(rf, f.length() - 64 * 1024)
            return "%016x".format(h and ULong.MAX_VALUE)
        }
    }

    /** "'Some.Movie.2024.1080p.mkv' → 'Some Movie 2024 1080p'". */
    fun cleanMediaQuery(name: String): String {
        var base = File(name).nameWithoutExtension
        for (sep in "._") base = base.replace(sep, ' ')
        return base.split(' ').filter { it.isNotBlank() }.joinToString(" ")
    }

    fun search(
        apiKey: String,
        query: String,
        filePath: String? = null,
        languages: String = "en",
        limit: Int = 20,
    ): List<SubtitleResult> {
        if (apiKey.isBlank()) throw OSException("no OpenSubtitles API key — add one in Settings")
        var results: List<SubtitleResult> = emptyList()
        if (filePath != null) {
            results = runCatching {
                searchOnce(
                    apiKey,
                    mapOf(
                        "moviehash" to opensubtitlesHash(filePath),
                        "moviebytesize" to File(filePath).length().toString(),
                        "languages" to languages,
                    ),
                    limit,
                    hashMatch = true,
                )
            }.getOrDefault(emptyList())
        }
        if (results.isEmpty() && query.isNotBlank()) {
            results = searchOnce(apiKey, mapOf("query" to query, "languages" to languages), limit, hashMatch = false)
        }
        return results
    }

    private fun searchOnce(
        apiKey: String,
        params: Map<String, String>,
        limit: Int,
        hashMatch: Boolean,
    ): List<SubtitleResult> {
        val qs = params.entries.joinToString("&") {
            URLEncoder.encode(it.key, "UTF-8") + "=" + URLEncoder.encode(it.value, "UTF-8")
        }
        val data = getJson(apiKey, "GET", "$API_BASE/subtitles?$qs") { conn ->
            conn.setRequestProperty("Accept", "application/json")
        }.optJSONArray("data") ?: return emptyList()
        val out = mutableListOf<SubtitleResult>()
        for (i in 0 until data.length().coerceAtMost(limit)) {
            val item = data.optJSONObject(i) ?: continue
            val attr = item.optJSONObject("attributes") ?: continue
            val files = attr.optJSONArray("files") ?: continue
            val fileId = files.optJSONObject(0)?.optLong("file_id", -1L) ?: -1L
            if (fileId <= 0) continue
            val feature = attr.optJSONObject("feature_details")
            out.add(
                SubtitleResult(
                    fileId = fileId,
                    release = attr.optString("release").ifBlank { files.optJSONObject(0)?.optString("file_name").orEmpty() },
                    language = attr.optString("language"),
                    downloads = attr.optInt("download_count", 0),
                    rating = attr.optDouble("ratings", 0.0),
                    title = feature?.optString("title").orEmpty(),
                    year = feature?.optInt("year", 0)?.takeIf { it > 0 }?.toString().orEmpty(),
                    hashMatch = hashMatch,
                ),
            )
        }
        return out
    }

    /** Download one subtitle file; returns the bytes (caller places the file). */
    fun download(apiKey: String, fileId: Long): ByteArray {
        val resp = getJson(apiKey, "POST", "$API_BASE/download") { conn ->
            conn.setRequestProperty("Content-Type", "application/json")
            conn.doOutput = true
            conn.outputStream.use { it.write(JSONObject().put("file_id", fileId).toString().toByteArray()) }
        }
        val link = resp.optString("link")
        if (link.isBlank()) throw OSException("download request returned no link")
        val conn = URL(link).openConnection() as HttpURLConnection
        return try {
            conn.connectTimeout = TIMEOUT
            conn.readTimeout = TIMEOUT
            conn.setRequestProperty("User-Agent", ua())
            if (conn.responseCode != 200) throw OSException("subtitle fetch failed: HTTP ${conn.responseCode}")
            readCapped(conn.inputStream, MAX_SUBTITLE_BYTES)
        } finally {
            conn.disconnect()
        }
    }

    private fun getJson(
        apiKey: String,
        method: String,
        url: String,
        configure: (HttpURLConnection) -> Unit = {},
    ): JSONObject {
        val conn = URL(url).openConnection() as HttpURLConnection
        return try {
            conn.requestMethod = method
            conn.connectTimeout = TIMEOUT
            conn.readTimeout = TIMEOUT
            conn.setRequestProperty("Api-Key", apiKey)
            conn.setRequestProperty("User-Agent", ua())
            configure(conn)
            when (conn.responseCode) {
                401 -> throw OSException("OpenSubtitles rejected the API key — check Settings")
                406 -> throw OSException("download quota exhausted — try again tomorrow")
                429 -> throw OSException("OpenSubtitles rate limit hit — try again later")
            }
            if (conn.responseCode != 200) throw OSException("OpenSubtitles error: HTTP ${conn.responseCode}")
            JSONObject(String(readCapped(conn.inputStream, 4 * 1024 * 1024), Charsets.UTF_8))
        } finally {
            conn.disconnect()
        }
    }

    private fun readCapped(input: InputStream, cap: Int): ByteArray {
        val out = java.io.ByteArrayOutputStream()
        val buf = ByteArray(16 * 1024)
        input.use { src ->
            while (true) {
                val n = src.read(buf)
                if (n == -1) break
                if (out.size() + n > cap) throw OSException("subtitle response too large")
                out.write(buf, 0, n)
            }
        }
        return out.toByteArray()
    }

    private const val MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
}
