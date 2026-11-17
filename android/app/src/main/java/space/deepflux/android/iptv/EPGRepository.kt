package space.deepflux.android.iptv

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * EPG repository — fetches XMLTV data, caches to disk, exposes per-channel
 * programme data. Integrated with IPTVRepository so channels and EPG
 * share the same lifecycle.
 */
class EPGRepository(private val context: Context) {

    private val _state = MutableStateFlow(EPGParser.EPGState())
    val state: StateFlow<EPGParser.EPGState> = _state.asStateFlow()

    private val _epgUrl = MutableStateFlow("")
    val epgUrl: StateFlow<String> = _epgUrl.asStateFlow()

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val cacheFile: File get() = File(context.filesDir, "epg_cache.xml")

    fun setEpgUrl(url: String) {
        _epgUrl.value = url
        if (url.isNotBlank()) refresh()
    }

    fun refresh() {
        val url = _epgUrl.value
        if (url.isBlank()) return
        scope.launch {
            try {
                val epgState = EPGParser.fetchAndParse(url)
                // Cache raw XML for offline
                runCatching {
                    val conn = java.net.URL(url).openConnection() as java.net.HttpURLConnection
                    conn.inputStream.use { input -> cacheFile.outputStream().use { input.copyTo(it) } }
                    conn.disconnect()
                }
                _state.value = epgState
                Log.i(TAG, "EPG refreshed: ${epgState.programmes.size} channels")
            } catch (e: Exception) {
                Log.w(TAG, "EPG fetch failed: ${e.message}")
            }
        }
    }

    /** Restore cached EPG on boot. */
    fun restoreCache() {
        scope.launch {
            val cached = withContext(Dispatchers.IO) {
                // Size guard (OOM lesson 1.3.6, IPTV playlist crash): a
                // provider XMLTV cache can reach 100+ MB — materializing it
                // as one String can kill the app. Skip oversized caches.
                runCatching {
                    cacheFile.takeIf { it.exists() && it.length() < 24_000_000L }?.readText()
                }.getOrNull()
            } ?: return@launch
            runCatching {
                val parser = org.xmlpull.v1.XmlPullParserFactory.newInstance().newPullParser()
                // Re-use the parser by piping the cached text through a simple StringReader
                val state = parseCachedXml(cached)
                if (state.programmes.isNotEmpty()) {
                    _state.value = state
                    Log.i(TAG, "restored EPG from cache: ${state.programmes.size} channels")
                }
            }
        }
    }

    private fun parseCachedXml(xml: String): EPGParser.EPGState {
        // Write to a temp file and use the standard parser path
        val tmp = File(context.cacheDir, "epg_tmp.xml")
        tmp.writeText(xml)
        return runCatching {
            val parser = org.xmlpull.v1.XmlPullParserFactory.newInstance().newPullParser()
            java.io.FileReader(tmp).use { reader ->
                parser.setInput(reader)
                // Use reflection-free approach: just call the companion parse
                parseWithParser(parser)
            }
        }.getOrDefault(EPGParser.EPGState())
    }

    private fun parseWithParser(parser: org.xmlpull.v1.XmlPullParser): EPGParser.EPGState {
        // Duplicate the parsing logic inline (the companion object doesn't expose this)
        val programmes = mutableListOf<EPGParser.Programme>()
        val channelNames = mutableMapOf<String, String>()
        var event = parser.eventType
        var inProgramme = false
        var inChannel = false
        var channelName = ""
        var title = ""
        var desc = ""
        var start = 0L
        var stop = 0L

        while (event != org.xmlpull.v1.XmlPullParser.END_DOCUMENT) {
            when (event) {
                org.xmlpull.v1.XmlPullParser.START_TAG -> when (parser.name) {
                    "channel" -> {
                        inChannel = true
                        val id = parser.getAttributeValue(null, "id") ?: ""
                        channelNames[id] = id
                    }
                    "display-name" -> if (inChannel) {
                        val id = parser.getAttributeValue(null, "id") ?: channelNames.keys.lastOrNull() ?: ""
                        channelNames[id] = parser.nextText().orEmpty()
                    }
                    "programme" -> {
                        inProgramme = true
                        val chId = parser.getAttributeValue(null, "channel") ?: ""
                        channelName = channelNames[chId] ?: chId
                        start = parseDate(parser.getAttributeValue(null, "start") ?: "")
                        stop = parseDate(parser.getAttributeValue(null, "stop") ?: "")
                        title = ""; desc = ""
                    }
                    "title" -> if (inProgramme) title = parser.nextText().orEmpty()
                    "desc" -> if (inProgramme) desc = parser.nextText().orEmpty()
                }
                org.xmlpull.v1.XmlPullParser.END_TAG -> when (parser.name) {
                    "channel" -> inChannel = false
                    "programme" -> {
                        inProgramme = false
                        if (title.isNotBlank() && start > 0 && stop > start) {
                            programmes.add(EPGParser.Programme(channelName, title, desc, start, stop))
                        }
                    }
                }
            }
            event = parser.next()
        }
        return EPGParser.EPGState(
            programmes = programmes.groupBy { it.channelName }.mapValues { (_, l) -> l.sortedBy { it.startTime } },
            lastUpdated = System.currentTimeMillis()
        )
    }

    private fun parseDate(raw: String): Long {
        if (raw.isBlank()) return 0
        return runCatching {
            val clean = raw.trim().split(" ")[0]
            val fmt = java.text.SimpleDateFormat("yyyyMMddHHmmss", java.util.Locale.US)
            fmt.timeZone = java.util.TimeZone.getTimeZone("UTC")
            fmt.parse(clean)?.time ?: 0
        }.getOrDefault(0)
    }

    companion object {
        private const val TAG = "EPGRepository"
    }
}
