package space.deepflux.android.iptv

/**
 * The three browsing sections of a source — Live TV, Movies, Series — over
 * the per-entry rules of [Classify]: the section of every entry, counted,
 * and the filtered view the screen shows. Pure functions over a channel
 * list, so they are tested without Android.
 *
 * The sections are computed ONCE per load, off the main thread (a 75k-entry
 * playlist is a few hundred regex passes), and travel with the channels as
 * a parallel list — `channels[i]` is in `sections[i]`.
 */
object Sections {

    /** Order of the section chips; an entry's section is a [Classify] constant. */
    val ORDER = listOf(Classify.LIVE, Classify.MOVIES, Classify.SERIES)

    /** The section of every channel, in the list's own order. */
    fun sectionsOf(channels: List<M3UParser.Channel>, forced: String): List<String> =
        channels.map { Classify.section(it.groupId, it.url, it.name, forced) }

    /** How many entries each section has. */
    fun counts(sections: List<String>): Map<String, Int> {
        val out = LinkedHashMap<String, Int>()
        for (s in ORDER) out[s] = 0
        for (s in sections) out[s] = (out[s] ?: 0) + 1
        return out
    }

    /**
     * The entries to show: in [section] ("" = any), in group [category]
     * ("" = any), and whose name or group contains [query] (case-insensitive;
     * blank = any). List order is kept, so `#numbers` stay meaningful.
     */
    fun filter(
        channels: List<M3UParser.Channel>,
        sections: List<String>,
        section: String,
        category: String,
        query: String,
    ): List<M3UParser.Channel> {
        val q = query.trim()
        val bySection = section.isBlank()
        val byGroup = category.isBlank()
        val byQuery = q.isEmpty()
        if (bySection && byGroup && byQuery) return channels
        return channels.filterIndexed { i, c ->
            (bySection || sections.getOrNull(i) == section) &&
                (byGroup || c.groupId == category) &&
                (byQuery || c.name.contains(q, ignoreCase = true) || c.groupId.contains(q, ignoreCase = true))
        }
    }
}
