package space.deepflux.android.iptv

import android.util.Log
import org.xmlpull.v1.XmlPullParser
import org.xmlpull.v1.XmlPullParserFactory
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * XMLTV EPG parser + fetcher — the Android twin of iptv/epg.py.
 * Parses standard XMLTV <programme> elements from an EPG URL and
 * builds a per-channel schedule.
 */
object EPGParser {

    data class Programme(
        val channelName: String,
        val title: String,
        val description: String = "",
        val startTime: Long,
        val endTime: Long,
    ) {
        val isNow: Boolean
            get() = System.currentTimeMillis() in startTime until endTime
    }

    data class EPGState(
        val programmes: Map<String, List<Programme>> = emptyMap(), // channelName -> sorted programmes
        val lastUpdated: Long = 0,
    )

    fun fetchAndParse(epgUrl: String): EPGState {
        val conn = URL(epgUrl).openConnection() as HttpURLConnection
        return try {
            conn.requestMethod = "GET"
            conn.connectTimeout = 15_000
            conn.readTimeout = 30_000
            if (conn.responseCode != 200) {
                throw IllegalStateException("EPG HTTP ${conn.responseCode}")
            }
            val parser = XmlPullParserFactory.newInstance().newPullParser()
            parser.setFeature(XmlPullParser.FEATURE_PROCESS_NAMESPACES, false)
            parser.setInput(InputStreamReader(conn.inputStream, Charsets.UTF_8))
            parseXMLTV(parser)
        } finally {
            conn.disconnect()
        }
    }

    private fun parseXMLTV(parser: XmlPullParser): EPGState {
        val programmes = mutableListOf<Programme>()
        var channelName = ""
        var title = ""
        var desc = ""
        var start = 0L
        var stop = 0L
        val channelNames = mutableMapOf<String, String>() // id -> display-name

        var event = parser.eventType
        var inProgramme = false
        var inChannel = false

        while (event != XmlPullParser.END_DOCUMENT) {
            when (event) {
                XmlPullParser.START_TAG -> when (parser.name) {
                    "channel" -> {
                        inChannel = true
                        val id = parser.getAttributeValue(null, "id") ?: ""
                        channelNames[id] = id
                    }
                    "display-name" -> if (inChannel) {
                        val id = parser.getAttributeValue(null, "id")
                            ?: channelNames.keys.lastOrNull() ?: ""
                        channelNames[id] = parser.nextText().orEmpty()
                    }
                    "programme" -> {
                        inProgramme = true
                        val chId = parser.getAttributeValue(null, "channel") ?: ""
                        channelName = channelNames[chId] ?: chId
                        start = parseXMLTVDate(parser.getAttributeValue(null, "start") ?: "")
                        stop = parseXMLTVDate(parser.getAttributeValue(null, "stop") ?: "")
                        title = ""
                        desc = ""
                    }
                    "title" -> if (inProgramme) title = parser.nextText().orEmpty()
                    "desc" -> if (inProgramme) desc = parser.nextText().orEmpty()
                }
                XmlPullParser.END_TAG -> when (parser.name) {
                    "channel" -> inChannel = false
                    "programme" -> {
                        inProgramme = false
                        if (title.isNotBlank() && start > 0 && stop > start) {
                            programmes.add(Programme(channelName, title, desc, start, stop))
                        }
                    }
                }
            }
            event = parser.next()
        }

        // Group by channel, sorted by start time
        val byChannel = programmes.groupBy { it.channelName }
            .mapValues { (_, list) -> list.sortedBy { it.startTime } }

        Log.i(TAG, "parsed ${programmes.size} programmes across ${byChannel.size} channels")
        return EPGState(programmes = byChannel, lastUpdated = System.currentTimeMillis())
    }

    /** XMLTV date format: "20261113120000 +0000" */
    private fun parseXMLTVDate(raw: String): Long {
        if (raw.isBlank()) return 0
        return runCatching {
            val clean = raw.trim().split(" ")[0] // strip timezone
            val fmt = SimpleDateFormat("yyyyMMddHHmmss", Locale.US)
            fmt.timeZone = TimeZone.getTimeZone("UTC")
            fmt.parse(clean)?.time ?: 0
        }.getOrDefault(0)
    }

    /** Get what's on now + next for a channel. */
    fun nowAndNext(state: EPGState, channelName: String): Pair<Programme?, Programme?> {
        val progs = state.programmes[channelName] ?: return null to null
        val now = System.currentTimeMillis()
        val current = progs.find { now in it.startTime until it.endTime }
        val next = progs.find { it.startTime > now }
        return current to next
    }

    private const val TAG = "EPGParser"
}
