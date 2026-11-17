package space.deepflux.android.rss

import org.xmlpull.v1.XmlPullParser
import org.xmlpull.v1.XmlPullParserFactory
import java.io.ByteArrayInputStream
import java.io.InputStream
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL

/**
 * Minimal RSS 2.0 / Atom reader for the feeds feature (plan §13 #4) —
 * the Android twin of the desktop's RSS tooling, restricted to what a
 * torrent feed carries: title, link, guid, pubDate, enclosure, and the
 * magnet many feeds embed in the description instead of the link.
 *
 * Desktop rules kept: the body is STREAMED with a hard 5 MiB cap (never
 * buffer an unbounded remote body), and every feed read is bounded in
 * time.
 */
object RssClient {

    data class RssItem(
        val guid: String,
        val title: String,
        val link: String,
        /** A magnet found in the description, when the feed embeds one. */
        val magnet: String?,
        val publishedAt: Long,
    )

    data class RssFeedContent(
        val title: String,
        val items: List<RssItem>,
    )

    fun fetch(url: String): RssFeedContent {
        val conn = URL(url).openConnection() as HttpURLConnection
        val bytes = try {
            conn.requestMethod = "GET"
            conn.connectTimeout = 10_000
            conn.readTimeout = 20_000
            conn.setRequestProperty("User-Agent", UA)
            conn.setRequestProperty("Accept", "application/rss+xml, application/atom+xml, application/xml, text/xml, */*")
            val code = conn.responseCode
            if (code != 200) throw IllegalStateException("HTTP $code")
            readCapped(conn.inputStream, MAX_BODY_BYTES)
        } finally {
            conn.disconnect()
        }
        if (bytes.isEmpty()) throw IllegalStateException("feed body over ${MAX_BODY_BYTES / 1048576} MiB or empty")
        return parse(ByteArrayInputStream(bytes))
    }

    /** Over-cap bodies fail closed (return empty → caller reports an error). */
    private fun readCapped(input: InputStream, cap: Int): ByteArray {
        val out = java.io.ByteArrayOutputStream()
        val buf = ByteArray(16 * 1024)
        input.use { src ->
            while (true) {
                val n = src.read(buf)
                if (n == -1) break
                if (out.size() + n > cap) return ByteArray(0)
                out.write(buf, 0, n)
            }
        }
        return out.toByteArray()
    }

    private fun parse(input: InputStream): RssFeedContent {
        val parser = XmlPullParserFactory.newInstance().newPullParser()
        parser.setFeature(XmlPullParser.FEATURE_PROCESS_NAMESPACES, false)
        parser.setInput(InputStreamReader(input, Charsets.UTF_8))

        var feedTitle = ""
        val items = mutableListOf<RssItem>()
        var inItem = false // RSS <item> or Atom <entry>
        var text = StringBuilder()
        var title = ""
        var link = ""
        var guid = ""
        var pubDate = ""
        var enclosure = ""
        var magnet = ""

        var event = parser.eventType
        while (event != XmlPullParser.END_DOCUMENT) {
            when (event) {
                XmlPullParser.START_TAG -> when (parser.name.lowercase()) {
                    "item", "entry" -> {
                        inItem = true; title = ""; link = ""; guid = ""; pubDate = ""; enclosure = ""; magnet = ""
                    }
                    "enclosure" -> if (inItem) enclosure = parser.getAttributeValue(null, "url") ?: ""
                    "link" -> if (inItem) {
                        // Atom: <link rel="alternate" href="..."/> (skip rel=self);
                        // RSS: <link>text</link>.
                        val rel = parser.getAttributeValue(null, "rel")
                        if (rel == null || rel == "alternate") {
                            parser.getAttributeValue(null, "href")?.let { link = it }
                        }
                    }
                }
                // Append ALWAYS (not just inItem): the CHANNEL <title> sits
                // before the first item, and the per-element reset keeps
                // sibling elements from bleeding into each other.
                XmlPullParser.TEXT -> text.append(parser.text)
                XmlPullParser.END_TAG -> when (parser.name.lowercase()) {
                    "item", "entry" -> {
                        inItem = false
                        // Torrent feeds often leave <link> as a details page
                        // and embed the real magnet in the description —
                        // prefer whichever is directly downloadable.
                        val bestLink = when {
                            link.startsWith("magnet:") -> link
                            magnet.isNotBlank() && !isDownloadable(link) -> magnet
                            link.isBlank() && magnet.isNotBlank() -> magnet
                            else -> link
                        }
                        items.add(
                            RssItem(
                                guid = guid.ifBlank { bestLink.ifBlank { title } },
                                title = title.trim(),
                                link = bestLink.trim(),
                                magnet = magnet.ifBlank { null },
                                publishedAt = parseDate(pubDate),
                            )
                        )
                        if (items.size >= MAX_ITEMS) return RssFeedContent(feedTitle, items)
                    }
                    "title" -> {
                        if (inItem) title = text.toString() else feedTitle = text.toString().trim()
                    }
                    "link" -> if (inItem && link.isBlank()) link = text.toString().trim()
                    "guid", "id" -> if (inItem) guid = text.toString().trim()
                    "pubdate", "published", "updated" -> if (inItem) pubDate = text.toString().trim()
                    // The text buffer resets on the next START_TAG, so the
                    // magnet must be lifted out HERE, at description close.
                    "description", "summary", "content" ->
                        if (inItem && magnet.isBlank()) MAGNET_RE.find(text.toString())?.let { magnet = it.value }
                }
            }
            // Reset the text buffer at the start of every element so each
            // END_TAG sees only its own character data.
            if (event == XmlPullParser.START_TAG) text = StringBuilder()
            event = parser.next()
        }
        return RssFeedContent(feedTitle, items)
    }

    /** Best-effort RFC 822 / ISO 8601 → epoch ms; 0 when unreadable. */
    private fun parseDate(raw: String): Long {
        if (raw.isBlank()) return 0L
        val s = raw.trim()
        runCatching {
            return java.text.SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss zzz", java.util.Locale.US).parse(s)?.time ?: 0L
        }
        runCatching {
            return java.time.OffsetDateTime.parse(s).toInstant().toEpochMilli()
        }
        runCatching {
            return java.time.LocalDateTime.parse(s).atZone(java.time.ZoneId.systemDefault()).toInstant().toEpochMilli()
        }
        return 0L
    }

    private const val MAX_BODY_BYTES = 5 * 1024 * 1024 // desktop rule: 5 MiB RSS cap
    private const val MAX_ITEMS = 200
    private const val UA = space.deepflux.android.dlmgr.HttpDownloadRepository.USER_AGENT
    private val MAGNET_RE = Regex("magnet:\\?xt=urn:btih:[a-zA-Z0-9]+[^\\s\"<>]*")

    /** Is this link directly loadable by an engine (magnet/torrent/http)? */
    internal fun isDownloadable(link: String): Boolean =
        link.startsWith("magnet:", true) ||
            link.startsWith("http://", true) || link.startsWith("https://", true)
}
