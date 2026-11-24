package space.deepflux.android.iptv

import android.util.Log
import org.xmlpull.v1.XmlPullParser
import org.xmlpull.v1.XmlPullParserFactory
import java.io.Reader
import java.text.Normalizer
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

/**
 * XMLTV EPG parser — the Android twin of iptv/epg.py.
 * Parses standard XMLTV <programme> elements into per-channel schedules.
 * The same [parse] entry serves the network stream and the disk cache (the
 * repository used to carry a second, copy-pasted parser for the cache).
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

    /**
     * One parsed guide.
     *
     * [programmes] is keyed by the XMLTV `<channel id>`; [names] maps that id
     * to its first display name and [aliases] every display name back to its
     * id. Lookups go through [programmesFor]: id first, exact names, then the
     * normalized form ("BBC One HD" ≈ "bbc one").
     */
    data class EPGState(
        val programmes: Map<String, List<Programme>> = emptyMap(), // channel id -> sorted programmes
        val names: Map<String, String> = emptyMap(),               // channel id -> first display name
        val aliases: Map<String, String> = emptyMap(),             // every display name -> channel id
        val lastUpdated: Long = 0,
    ) {
        val channelCount: Int get() = programmes.size

        /** Normalized display name -> channel id (first wins). Built once per state. */
        private val byNormalizedName: Map<String, String> by lazy {
            val out = HashMap<String, String>()
            for ((name, id) in aliases) {
                val key = normalizeName(name)
                if (key.isNotBlank()) out.putIfAbsent(key, id)
            }
            out
        }

        internal fun idByNormalizedName(key: String): String? = byNormalizedName[key]
    }

    // ------------------------------------------------------------------ parsing

    /** A pull parser over [reader], with or without XML namespaces. */
    fun newParser(reader: Reader): XmlPullParser {
        val parser = XmlPullParserFactory.newInstance().newPullParser()
        runCatching { parser.setFeature(XmlPullParser.FEATURE_PROCESS_NAMESPACES, false) }
        parser.setInput(reader)
        return parser
    }

    /**
     * Parse one XMLTV document from a prepared parser (network stream or
     * cache file).
     *
     * Bounded by RELEVANCE, not only by count (second review, 2026-11-24):
     * the download is capped at 256 MB, but the parse used to keep every
     * programme whole — a guide that size is more objects than the app has
     * heap. Only what the screen can need is kept: programmes that have not
     * ended more than [pastWindowMs] before [now] and start within
     * [horizonMs] after it (the cache refreshes every few hours, so the
     * window moves along), at most [maxProgrammes] of them, and no text
     * field longer than [maxText] characters.
     */
    fun parse(
        parser: XmlPullParser,
        now: Long = System.currentTimeMillis(),
        pastWindowMs: Long = PAST_WINDOW_MS,
        horizonMs: Long = HORIZON_MS,
        maxProgrammes: Int = MAX_PROGRAMMES,
        maxText: Int = MAX_TEXT,
    ): EPGState {
        val earliest = now - pastWindowMs
        val latest = now + horizonMs
        val names = LinkedHashMap<String, String>()          // channel id -> first display name
        val aliases = LinkedHashMap<String, String>()        // display name -> channel id (first wins)
        val perChannel = LinkedHashMap<String, MutableList<Programme>>()
        var currentId: String? = null
        var inChannel = false
        var inProgramme = false
        var channelId = ""
        var title = ""
        var desc = ""
        var start = 0L
        var stop = 0L
        var kept = 0
        var overLimit = 0

        var event = runCatching { parser.eventType }.getOrDefault(XmlPullParser.START_DOCUMENT)
        while (event != XmlPullParser.END_DOCUMENT) {
            when (event) {
                XmlPullParser.START_TAG -> when (parser.name) {
                    "channel" -> {
                        inChannel = true
                        val id = parser.getAttributeValue(null, "id").orEmpty()
                        currentId = id
                        names.putIfAbsent(id, "")
                    }
                    "display-name" -> if (inChannel && currentId != null) {
                        val displayName = parser.innerText().orEmpty().trim().take(maxText)
                        if (displayName.isNotBlank()) {
                            // First non-blank name is THE name; every one is an alias.
                            if (names[currentId].isNullOrEmpty()) names[currentId] = displayName
                            aliases.putIfAbsent(displayName, currentId)
                        }
                    }
                    "programme" -> {
                        inProgramme = true
                        channelId = parser.getAttributeValue(null, "channel").orEmpty()
                        start = parseXmltvDate(parser.getAttributeValue(null, "start").orEmpty())
                        stop = parseXmltvDate(parser.getAttributeValue(null, "stop").orEmpty())
                        title = ""
                        desc = ""
                    }
                    "title" -> if (inProgramme) title = parser.innerText().orEmpty().trim().take(maxText)
                    "desc" -> if (inProgramme) desc = parser.innerText().orEmpty().trim().take(maxText)
                }
                XmlPullParser.END_TAG -> when (parser.name) {
                    "channel" -> {
                        inChannel = false
                        currentId = null
                    }
                    "programme" -> {
                        inProgramme = false
                        if (title.isNotBlank() && start > 0 && stop > start && stop >= earliest && start <= latest) {
                            if (kept < maxProgrammes) {
                                kept++
                                perChannel.getOrPut(channelId) { ArrayList() }
                                    .add(Programme(names[channelId] ?: channelId, title, desc, start, stop))
                            } else {
                                overLimit++
                            }
                        }
                    }
                }
            }
            event = parser.next()
        }

        val sorted = perChannel.mapValues { (_, list) -> list.sortedBy { it.startTime } }
        Log.i(TAG, "parsed $kept programmes across ${sorted.size} channels" + if (overLimit > 0) " ($overLimit more skipped: over the limit)" else "")
        return EPGState(programmes = sorted, names = names, aliases = aliases, lastUpdated = System.currentTimeMillis())
    }

    /**
     * The text of the element just opened, up to its closing tag — child
     * elements included, as their text. `nextText()` throws on a `<desc>`
     * with markup in it (some guides bold a word or nest a span), and one
     * such line used to cost the whole guide.
     */
    private fun XmlPullParser.innerText(): String? {
        val sb = StringBuilder()
        var depth = 1
        while (depth > 0) {
            when (next()) {
                XmlPullParser.TEXT -> sb.append(text)
                XmlPullParser.CDSECT -> sb.append(text)
                XmlPullParser.START_TAG -> depth++
                XmlPullParser.END_TAG -> depth--
                XmlPullParser.END_DOCUMENT -> return null
            }
        }
        return sb.toString()
    }

    /**
     * XMLTV date: "20261113120000 +0800". The offset is HONORED — it used
     * to be stripped and the wall-clock time read as UTC, which shifted
     * every non-UTC guide by its offset. Without an offset the spec-default
     * UTC applies.
     */
    fun parseXmltvDate(raw: String): Long {
        val text = raw.trim()
        if (text.isEmpty()) return 0
        return synchronized(UTC_FORMAT) {
            runCatching {
                val space = text.indexOf(' ')
                if (space < 0) {
                    UTC_FORMAT.parse(text)?.time ?: 0L
                } else {
                    val zone = text.substring(space + 1).trim()
                    val offset = when {
                        // "Z"/"UTC"/"GMT" are RFC 3339 spellings some guides use.
                        zone.isEmpty() || zone.equals("Z", true) || zone.equals("UTC", true) || zone.equals("GMT", true) -> "+0000"
                        else -> zone
                    }
                    OFFSET_FORMAT.parse(text.substring(0, space) + " " + offset)?.time ?: 0L
                }
            }.getOrDefault(0L)
        }
    }

    // ------------------------------------------------------------------ lookup

    /**
     * The schedule for one playlist channel, or null when the guide does not
     * know it. Order: the playlist's `tvg-id` (the XMLTV channel id), then
     * `tvg-name`/display name as an id or as a guide display name, then the
     * normalized display name.
     */
    fun programmesFor(state: EPGState, channel: M3UParser.Channel): List<Programme>? {
        if (channel.tvgId.isNotBlank()) state.programmes[channel.tvgId]?.let { return it }
        for (key in listOf(channel.tvgName, channel.name)) {
            if (key.isBlank()) continue
            state.programmes[key]?.let { return it }
            state.aliases[key]?.let { id -> state.programmes[id]?.let { return it } }
        }
        val normalized = normalizeName(channel.tvgName.ifBlank { channel.name })
        if (normalized.isBlank()) return null
        return state.idByNormalizedName(normalized)?.let { state.programmes[it] }
    }

    /** What is on now + what is next for a channel (either may be null). */
    fun nowAndNext(state: EPGState, channel: M3UParser.Channel): Pair<Programme?, Programme?> {
        val progs = programmesFor(state, channel) ?: return null to null
        val now = System.currentTimeMillis()
        val current = progs.find { now in it.startTime until it.endTime }
        val next = progs.find { it.startTime > now }
        return current to next
    }

    /** Name-only overload (a channel with no EPG attributes). */
    fun nowAndNext(state: EPGState, channelName: String): Pair<Programme?, Programme?> =
        nowAndNext(state, M3UParser.Channel(name = channelName, url = ""))

    /**
     * Match key for channel names: case-, accent- and punctuation-insensitive,
     * with the tokens playlists add and guides omit dropped — quality tags
     * (HD/FHD/UHD/SD/4K/resolutions) and "(backup n)"/"[backup]" markers.
     */
    fun normalizeName(raw: String): String {
        val base = MARKS.replace(Normalizer.normalize(raw, Normalizer.Form.NFD), "")
        val lowered = base.lowercase(Locale.ROOT)
        val noMarkers = BACKUP_MARKERS.replace(lowered, " ")
        val noQuality = QUALITY_TOKENS.replace(noMarkers, " ")
        return NON_ALNUM.replace(noQuality, "")
    }

    // ------------------------------------------------------------------ internals

    // SimpleDateFormat is not thread-safe: the repository parses from one
    // worker, but the parser is a process-wide object — guard the formats.
    private val UTC_FORMAT = SimpleDateFormat("yyyyMMddHHmmss", Locale.US).apply {
        timeZone = TimeZone.getTimeZone("UTC")
        isLenient = false
    }

    private val OFFSET_FORMAT = SimpleDateFormat("yyyyMMddHHmmss Z", Locale.US).apply {
        isLenient = false
    }

    /** Combining marks left by the NFD decomposition of accented letters. */
    private val MARKS = Regex("\\p{M}+")
    private val NON_ALNUM = Regex("[^a-z0-9]+")

    /** "(backup n)" / "[Backup 2]" — iptv-org style duplicate markers. */
    private val BACKUP_MARKERS = Regex("[\\(\\[][^\\)\\]]*backup[^\\)\\]]*[\\)\\]]")

    /** Quality tags at word boundaries — "BBC One HD" is "BBC One" in the guide. */
    private val QUALITY_TOKENS =
        Regex("(^|[^a-z0-9])(hd|fhd|uhd|sd|4k|8k|1080[pi]|720[pi]|2160[pi]|hevc|h265|backup[ ]?\\d*)(?=$|[^a-z0-9])")

    /** Programmes that ended this long ago are still kept (a "now" that started earlier). */
    const val PAST_WINDOW_MS = 12 * 60 * 60 * 1000L

    /** Programmes starting within this window are kept; the guide refreshes every few hours, so it moves along. */
    const val HORIZON_MS = 7 * 24 * 60 * 60 * 1000L

    /** The hard ceiling on kept programmes, whatever the window holds. */
    const val MAX_PROGRAMMES = 400_000

    /** Longest title / description kept (characters). */
    const val MAX_TEXT = 400

    private const val TAG = "EPGParser"
}
