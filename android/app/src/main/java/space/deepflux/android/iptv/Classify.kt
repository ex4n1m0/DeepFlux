package space.deepflux.android.iptv

/**
 * Content classification — Live TV / Movies / Series — the Android twin of
 * the desktop's `iptv/classify.py` (rules copied one-to-one; keep the two
 * in step when a rule changes there).
 *
 * An entry is classified by the first matching signal:
 *  1. an SxxExx name is a strong "series episode" signal;
 *  2. live-channel naming ("UK: Sky Cinema FHD" — a country prefix and no
 *     release year) beats group keywords, because providers file 24/7 movie
 *     *channels* under groups like "UK Movies";
 *  3. group keywords / URL patterns / file extensions for series and movies;
 *  4. a dated upload path ("/video/m3u8/2025/09/06/<id>/index.m3u8") is VOD —
 *     live streams never carry an upload date;
 *  5. everything else is live.
 *
 * A per-source [forced] type lets the user decide for a whole playlist
 * (owner 2026-11-22): "live" pins every entry to channels, "vod" promotes
 * every entry, "auto" runs the heuristics.
 */
object Classify {

    const val LIVE = "live"
    const val MOVIES = "movies"
    const val SERIES = "series"

    private val SERIES_KEYWORDS = listOf(
        "series", "tv show", "tv shows", "shows", "seriya", "dizi", "serial", "serials", "soap",
    )
    private val SERIES_URL = listOf("/series/", "/tv/", "series/")

    private val MOVIE_KEYWORDS = listOf(
        "movie", "movies", "film", "films", "cinema", "vod", "vodu", "pelicula", "peliculas",
    )
    private val MOVIE_URL = listOf("/movie/", "movie/", ".mkv", ".mp4", ".avi", ".mov")
    private val MOVIE_EXT = listOf(".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v")

    /** Extensions that strongly imply a LIVE stream (or no extension at all). */
    private val LIVE_EXT = setOf(".ts", ".m3u8", ".m3u")

    private val LIVE_NAME_PREFIX = Regex("^[A-Z]{2,4}[:|]\\s*")
    private val NAME_YEAR = Regex("""\b(19\d{2}|20\d{2})\b""")

    /** Dated upload path — the only reliable VOD signal for providers whose group names match no keyword. */
    private val VOD_DATE_PATH = Regex("""/(?:19|20)\d{2}/\d{1,2}/\d{1,2}/""")

    /** Episode-numbered name — the strong series signal. */
    val SXXEXX = Regex("""[Ss]\d{1,2}\s?[Ee]\d{1,3}""")

    /** The section one playlist entry belongs to, heuristically. */
    fun classifyEntry(group: String, url: String, name: String): String {
        val g = group.lowercase()
        val u = url.lowercase()
        val ext = extensionOf(u)

        // Series first: an SxxExx name is a strong series signal regardless of group.
        if (SXXEXX.containsMatchIn(name)) {
            if (SERIES_URL.any { it in u } || SERIES_KEYWORDS.any { it in g } || ext !in LIVE_EXT) return SERIES
        }

        // Live-channel naming beats group keywords (24/7 channels are not VOD).
        if (looksLikeLiveChannel(name)) return LIVE

        // Group keywords / URL patterns / file extensions — series before movies.
        if (SERIES_KEYWORDS.any { it in g } || SERIES_URL.any { it in u }) return SERIES
        if (MOVIE_KEYWORDS.any { it in g } || MOVIE_URL.any { it in u } || (ext.isNotEmpty() && ext in MOVIE_EXT)) {
            return MOVIES
        }

        // Dated upload path → VOD.
        if (VOD_DATE_PATH.containsMatchIn(u)) {
            return if (SXXEXX.containsMatchIn(name)) SERIES else MOVIES
        }
        return LIVE
    }

    /**
     * The section under a per-source override:
     *  - "live": every entry is a channel (explicit user intent wins);
     *  - "vod":  every entry is VOD — Series for an SxxExx name, Movies otherwise;
     *  - anything else: the heuristics.
     */
    fun section(group: String, url: String, name: String, forced: String = "auto"): String =
        when (forced.trim().lowercase()) {
            "live" -> LIVE
            "vod" -> if (SXXEXX.containsMatchIn(name)) SERIES else MOVIES
            else -> classifyEntry(group, url, name)
        }

    /** "UK: Sky Cinema FHD", "US|ESPN HD" — a country prefix and no release year. */
    fun looksLikeLiveChannel(name: String): Boolean =
        LIVE_NAME_PREFIX.containsMatchIn(name) && !NAME_YEAR.containsMatchIn(name)

    /**
     * File extension of a URL's path, lowercased with the query and fragment
     * removed (".m3u8?token=…" is ".m3u8"); "" when there is none.
     */
    fun extensionOf(url: String): String {
        val path = url.substringBefore('?').substringBefore('#')
        val file = path.substringAfterLast('/')
        val dot = file.lastIndexOf('.')
        return if (dot <= 0) "" else file.substring(dot).lowercase()
    }
}
