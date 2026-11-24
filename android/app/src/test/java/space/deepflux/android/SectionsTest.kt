package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.iptv.Classify
import space.deepflux.android.iptv.M3UParser
import space.deepflux.android.iptv.Sections

/**
 * Browsing sections over a loaded channel list — Live TV / Movies / Series —
 * and the three filters the Live TV screen combines (section, group, search).
 */
class SectionsTest {

    private fun ch(name: String, url: String = "http://x/$name.m3u8", group: String = "") =
        M3UParser.Channel(name = name, url = url, groupId = group)

    private val list = listOf(
        ch("UK: BBC One", group = "UK"),
        ch("Big Film", "http://vod.example/movie/big-film.mp4", "Cinema"),
        ch("Some Show S01E01", "http://vod.example/series/some-show-s01e01.mp4", "Cinema"),
        ch("US|ESPN HD", group = "Sports"),
        ch("UK: Film4 HD", group = "UK Movies"), // a 24/7 channel, not a film
        ch("Documentary 2024", "http://x/d/2024/09/06/123/index.m3u8"),
    )

    @Test
    fun everyEntryGetsASectionAndTheCountsAddUp() {
        val sections = Sections.sectionsOf(list, "auto")
        assertEquals(
            listOf(Classify.LIVE, Classify.MOVIES, Classify.SERIES, Classify.LIVE, Classify.LIVE, Classify.MOVIES),
            sections,
        )
        assertEquals(mapOf(Classify.LIVE to 3, Classify.MOVIES to 2, Classify.SERIES to 1), Sections.counts(sections))
    }

    @Test
    fun thePerSourceOverrideDecidesForTheWholeSource() {
        val asLive = Sections.sectionsOf(list, "live")
        assertTrue("every entry is a channel", asLive.all { it == Classify.LIVE })
        val asVod = Sections.sectionsOf(list, "vod")
        assertEquals(
            "every entry is a video; an SxxExx name still makes a series",
            listOf(Classify.MOVIES, Classify.MOVIES, Classify.SERIES, Classify.MOVIES, Classify.MOVIES, Classify.MOVIES),
            asVod,
        )
    }

    @Test
    fun anEmptyListHasEverySectionAtZero() {
        assertEquals(mapOf(Classify.LIVE to 0, Classify.MOVIES to 0, Classify.SERIES to 0), Sections.counts(emptyList()))
        assertTrue(Sections.sectionsOf(emptyList(), "auto").isEmpty())
    }

    @Test
    fun theFiltersCombineAndListOrderIsKept() {
        val s = Sections.sectionsOf(list, "auto")
        val names = { c: M3UParser.Channel -> c.name }
        assertEquals(
            listOf("UK: BBC One", "US|ESPN HD", "UK: Film4 HD"),
            Sections.filter(list, s, Classify.LIVE, "", "").map(names),
        )
        assertEquals(
            "a group inside a section",
            listOf("UK: Film4 HD"),
            Sections.filter(list, s, Classify.LIVE, "UK Movies", "").map(names),
        )
        assertEquals(listOf("UK: BBC One"), Sections.filter(list, s, Classify.LIVE, "UK", "").map(names))
        assertEquals(
            "search is case-insensitive over names",
            listOf("Big Film", "UK: Film4 HD"),
            Sections.filter(list, s, "", "", "FILM").map(names),
        )
        assertEquals(
            "…and over group names",
            listOf("Big Film", "Some Show S01E01"),
            Sections.filter(list, s, "", "", "cinema").map(names),
        )
        assertEquals(
            "all three at once",
            listOf("Big Film"),
            Sections.filter(list, s, Classify.MOVIES, "Cinema", "big").map(names),
        )
        assertTrue("a filter nothing matches is empty, not everything", Sections.filter(list, s, Classify.SERIES, "UK", "").isEmpty())
    }

    @Test
    fun noFilterIsNoWork() {
        val s = Sections.sectionsOf(list, "auto")
        assertSame("the list itself comes back — no copy of a 75k-entry playlist", list, Sections.filter(list, s, "", "", ""))
        assertSame(list, Sections.filter(list, s, "", "", "   "))
    }
}
