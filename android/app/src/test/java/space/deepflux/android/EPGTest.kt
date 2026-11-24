package space.deepflux.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.iptv.EPGParser
import space.deepflux.android.iptv.EPGRepository
import space.deepflux.android.iptv.M3UParser
import java.io.File
import java.time.ZoneOffset
import java.time.ZonedDateTime

private val GUIDE = """
    <?xml version="1.0" encoding="UTF-8"?>
    <tv source-info-name="Test">
      <channel id="bbc.one"><display-name>BBC One</display-name><display-name>BBC One HD</display-name></channel>
      <channel id="news"><display-name>Sky News</display-name></channel>
      <programme start="20261123120000 +0800" stop="20261123130000 +0800" channel="bbc.one"><title>Morning Show</title></programme>
      <programme start="20261123130000 +0800" stop="20261123140000 +0800" channel="bbc.one"><title>Afternoon</title></programme>
      <programme start="20261123120000 +0000" stop="20261123130000 +0000" channel="news"><title>Paper Review</title></programme>
    </tv>
""".trimIndent()

private fun utc(hour: Int, minute: Int = 0): Long =
    ZonedDateTime.of(2026, 11, 23, hour, minute, 0, 0, ZoneOffset.UTC).toInstant().toEpochMilli()

/** "Now" while the fixed guide above is on the air: 04:30 UTC on its day. */
private val GUIDE_NOW = utc(4, 30)

private val HOUR = 3_600_000L

/** The parser keeps only programmes near "now", so a guide served to the repository is built around the real clock. */
private fun guideAround(now: Long, firstTitle: String = "Morning Show", secondTitle: String = "Afternoon"): String {
    fun stamp(ms: Long) = java.text.SimpleDateFormat("yyyyMMddHHmmss Z", java.util.Locale.US).format(java.util.Date(ms))
    return "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n" +
        "<tv source-info-name=\"Test\">\n" +
        "  <channel id=\"bbc.one\"><display-name>BBC One</display-name><display-name>BBC One HD</display-name></channel>\n" +
        "  <channel id=\"news\"><display-name>Sky News</display-name></channel>\n" +
        "  <programme start=\"${stamp(now - HOUR)}\" stop=\"${stamp(now + HOUR)}\" channel=\"bbc.one\"><title>$firstTitle</title></programme>\n" +
        "  <programme start=\"${stamp(now + HOUR)}\" stop=\"${stamp(now + 2 * HOUR)}\" channel=\"bbc.one\"><title>$secondTitle</title></programme>\n" +
        "  <programme start=\"${stamp(now - HOUR)}\" stop=\"${stamp(now + HOUR)}\" channel=\"news\"><title>Paper Review</title></programme>\n" +
        "</tv>\n"
}

/** One programme on channel "c", from [start] to [stop] (epoch ms). */
private fun programme(start: Long, stop: Long, title: String = "Show", desc: String = ""): String =
    "  <programme start=\"${java.text.SimpleDateFormat("yyyyMMddHHmmss Z", java.util.Locale.US).format(java.util.Date(start))}\" " +
        "stop=\"${java.text.SimpleDateFormat("yyyyMMddHHmmss Z", java.util.Locale.US).format(java.util.Date(stop))}\" " +
        "channel=\"c\"><title>$title</title><desc>$desc</desc></programme>\n"

/**
 * Fix batch 2026-11-23 — EPG correctness: XMLTV date offsets are honored
 * (they used to be stripped, shifting every non-UTC guide) and channels are
 * matched to the guide by id / name / normalized name instead of an exact
 * display-name hit.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class EPGParserTest {

    @Test
    fun dateOffsetIsHonored() {
        // 12:00 at +0800 is 04:00 UTC.
        assertEquals(utc(4), EPGParser.parseXmltvDate("20261123120000 +0800"))
        // 12:00 at -0500 is 17:00 UTC.
        assertEquals(utc(17), EPGParser.parseXmltvDate("20261123120000 -0500"))
    }

    @Test
    fun dateWithoutOffsetIsUtc() {
        assertEquals(utc(12), EPGParser.parseXmltvDate("20261123120000"))
        assertEquals(utc(12), EPGParser.parseXmltvDate("20261123120000 Z"))
        assertEquals(utc(12), EPGParser.parseXmltvDate("20261123120000 +0000"))
    }

    @Test
    fun badDatesAreZeroNotExceptions() {
        assertEquals(0L, EPGParser.parseXmltvDate(""))
        assertEquals(0L, EPGParser.parseXmltvDate("not a date"))
    }

    @Test
    fun parsesChannelsAliasesAndProgrammes() {
        val state = EPGParser.parse(EPGParser.newParser(GUIDE.reader()), now = GUIDE_NOW)
        assertEquals(2, state.channelCount)
        assertEquals("BBC One", state.names["bbc.one"])
        assertEquals("bbc.one", state.aliases["BBC One HD"])
        val bbc = state.programmes.getValue("bbc.one")
        assertEquals(listOf("Morning Show", "Afternoon"), bbc.map { it.title })
        assertEquals(utc(4), bbc[0].startTime) // +0800 applied to the programme times too
    }

    @Test
    fun markupInsideADescriptionOrTitleDoesNotCostTheGuide() {
        // Some guides bold a word or nest a span. nextText() throws on a
        // child element — one such line used to fail the whole parse.
        val markedUp = GUIDE.replace(
            "<title>Morning Show</title>",
            "<title>Morning <b>Show</b></title>",
        ).replace(
            "</programme>",
            "<desc>Plain <span>nested</span> &amp; tail</desc></programme>",
        )
        val state = EPGParser.parse(EPGParser.newParser(markedUp.reader()), now = GUIDE_NOW)
        val bbc = state.programmes.getValue("bbc.one")
        assertEquals(listOf("Morning Show", "Afternoon"), bbc.map { it.title })
        assertEquals("child elements' text is kept, in order", "Plain nested & tail", bbc[0].description)
        assertEquals("Plain nested & tail", bbc[1].description)
    }

    // ---------------------------------------------------------------- bounds (second review, 2026-11-24)

    private fun guide(vararg programmes: String) =
        "<?xml version=\"1.0\"?><tv><channel id=\"c\"><display-name>C</display-name></channel>${programmes.joinToString("")}</tv>"

    @Test
    fun onlyProgrammesNearNowAreKept() {
        val now = utc(12)
        val state = EPGParser.parse(
            EPGParser.newParser(
                guide(
                    programme(now - 2 * HOUR, now - HOUR, "Still listed (started earlier)"),
                    programme(now - 2 * 24 * HOUR, now - 2 * 24 * HOUR + HOUR, "Two days ago"),
                    programme(now + 6 * 24 * HOUR, now + 6 * 24 * HOUR + HOUR, "In six days"),
                    programme(now + 10 * 24 * HOUR, now + 10 * 24 * HOUR + HOUR, "In ten days"),
                ).reader(),
            ),
            now = now,
        )
        // The channel is still known; only its relevant programmes are kept.
        assertEquals("C", state.names["c"])
        assertEquals(listOf("Still listed (started earlier)", "In six days"), state.programmes.getValue("c").map { it.title })
    }

    @Test
    fun longTextsAreCutAndTheCountHasACeiling() {
        val now = utc(12)
        val many = (1..30).joinToString("") { programme(now - HOUR, now + HOUR, "Show $it") }
        val state = EPGParser.parse(
            EPGParser.newParser(
                guide(programme(now - HOUR, now + HOUR, "T".repeat(1000), "D".repeat(1000)) + many).reader(),
            ),
            now = now,
            maxProgrammes = 10,
            maxText = 50,
        )
        val kept = state.programmes.getValue("c")
        assertEquals("no more than the ceiling", 10, kept.size)
        assertEquals(50, kept[0].title.length)
        assertEquals(50, kept[0].description.length)
        assertEquals("the first ones in document order", "T".repeat(50), kept[0].title)
    }

    private fun state(): EPGParser.EPGState {
        val now = System.currentTimeMillis()
        fun prog(title: String, start: Long, end: Long) = EPGParser.Programme("X Channel HD", title, "", start, end)
        return EPGParser.EPGState(
            programmes = mapOf(
                "x.tv" to listOf(prog("Now", now - 60_000, now + 600_000), prog("Next", now + 600_000, now + 1_200_000)),
            ),
            names = mapOf("x.tv" to "X Channel HD"),
            aliases = mapOf("X Channel HD" to "x.tv", "X Channel" to "x.tv"),
        )
    }

    @Test
    fun channelMatchesByTvgIdFirst() {
        val channel = M3UParser.Channel(name = "Totally different display name", url = "", tvgId = "x.tv")
        val (now, next) = EPGParser.nowAndNext(state(), channel)
        assertEquals("Now", now?.title)
        assertEquals("Next", next?.title)
    }

    @Test
    fun channelMatchesByExactName() {
        val byName = EPGParser.nowAndNext(state(), M3UParser.Channel(name = "X Channel HD", url = ""))
        assertEquals("Now", byName.first?.title)
        val byTvgName = EPGParser.nowAndNext(state(), M3UParser.Channel(name = "?", url = "", tvgName = "X Channel"))
        assertEquals("Now", byTvgName.first?.title)
    }

    @Test
    fun channelMatchesByNormalizedDisplayName() {
        val s = state()
        for (name in listOf("X Channel", "x channel fhd", "X Channel (backup)", "X  Channel [Backup 2]")) {
            assertEquals("match for '$name'", "Now", EPGParser.nowAndNext(s, M3UParser.Channel(name = name, url = "")).first?.title)
        }
    }

    @Test
    fun unrelatedChannelDoesNotMatch() {
        val (now, next) = EPGParser.nowAndNext(state(), M3UParser.Channel(name = "Y Channel HD", url = ""))
        assertEquals(null, now)
        assertEquals(null, next)
    }

    @Test
    fun normalizeNameDropsAccentsQualityAndBackupMarkers() {
        assertEquals("bbc", EPGParser.normalizeName(" BBC "))
        assertEquals("channel5", EPGParser.normalizeName("Channel 5 HD"))
        assertEquals("channel5", EPGParser.normalizeName("Channel 5 (Backup 1)"))
        assertEquals("canaldecale", EPGParser.normalizeName("Canal+ Décalé"))
        // A token only counts at word boundaries.
        assertEquals("4kun", EPGParser.normalizeName("4KUN"))
    }
}

/**
 * The guide cache. 2026-11-23: it is actually READ again (the restore path
 * had no caller), a fresh cache skips the network, and a refresh downloads
 * the guide once (it used to download it twice).
 *
 * Review round 2026-11-24 — the cache belongs to ONE guide. There used to be
 * a single cache file for whichever guide had been downloaded last, so
 * switching to a playlist with another guide showed the previous playlist's
 * listings for as long as that file was fresh.
 *
 * Second review round, same day: after one failed fetch the loader was left
 * "in flight" forever (every later `ensure` concluded a load was on its way),
 * a guide served to the repository is built around the real clock (the
 * parser keeps only what is near "now").
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class EPGRepositoryTest {

    private lateinit var context: Context
    private lateinit var server: TinyHttpServer
    private var previousLogStream: java.io.PrintStream? = null

    /** A second guide, recognisable by its first title. */
    private fun otherGuide() = guideAround(System.currentTimeMillis(), firstTitle = "Other Guide Title")

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        server = TinyHttpServer()
        // The repository reports failures only through android.util.Log.
        previousLogStream = org.robolectric.shadows.ShadowLog.stream
        org.robolectric.shadows.ShadowLog.stream = System.out
    }

    @After
    fun tearDown() {
        server.close()
        org.robolectric.shadows.ShadowLog.stream = previousLogStream
    }

    private fun serve(path: String, text: String): String {
        server.route(path) { TinyHttpServer.Response(body = text.toByteArray(), headers = mapOf("Content-Type" to "text/xml")) }
        return server.url(path)
    }

    private fun hits(path: String): Int = server.requests.count { it.path == path }

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 10_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) {
                val seen = server.requests.joinToString { "${it.method} ${it.path}" }
                throw AssertionError("timed out waiting for $what (server saw ${server.hits} request(s): $seen)")
            }
            Thread.sleep(50)
        }
    }

    /** The first title of guide channel "bbc.one", or null while no guide (or not this one) is loaded. */
    private fun firstTitle(repo: EPGRepository): String? = repo.state.value.programmes["bbc.one"]?.firstOrNull()?.title

    private fun cacheOf(repo: EPGRepository, url: String) = File(context.filesDir, repo.cacheNameFor(url))

    /** The partial download of [url]'s load, wherever its own name has put it (each load has its own). */
    private fun partialOf(repo: EPGRepository, url: String): File? =
        context.filesDir.listFiles().orEmpty().firstOrNull { it.name.startsWith(repo.cacheNameFor(url) + ".") && it.name.endsWith(".tmp") }

    // ---------------------------------------------------------------- cache basics

    @Test
    fun aFreshCacheOfTheSelectedGuideIsServedWithoutTheNetwork() {
        val url = serve("/guide.xml", guideAround(System.currentTimeMillis()))
        val repo = EPGRepository(context)
        // Left by an earlier run of THIS guide, with a different title.
        cacheOf(repo, url).writeText(guideAround(System.currentTimeMillis(), firstTitle = "Cached title"))
        repo.setEpgUrl(url)
        await("cache restore") { repo.state.value.channelCount > 0 }
        assertEquals(0, hits("/guide.xml"))
        assertEquals("Cached title", firstTitle(repo))
        assertFalse("no fetch, no spinner", repo.loading.value)
    }

    @Test
    fun aCacheLeftByAnotherGuideIsNotPassedOffAsTheSelectedOne() {
        val other = server.url("/other.xml")
        val url = serve("/guide.xml", guideAround(System.currentTimeMillis()))
        val repo = EPGRepository(context)
        cacheOf(repo, other).writeText(otherGuide())
        // And the single-name cache of older builds, whose guide nobody knows.
        File(context.filesDir, "epg_cache.xml").writeText(otherGuide())
        repo.setEpgUrl(url)
        await("network load") { repo.state.value.channelCount > 0 }
        assertEquals("the selected guide is fetched", 1, hits("/guide.xml"))
        assertEquals("Morning Show", firstTitle(repo))
        assertFalse("the orphaned cache of older builds is removed", File(context.filesDir, "epg_cache.xml").exists())
    }

    @Test
    fun anUnusableCacheIsNoGuideAndTheNetworkIsAsked() {
        val url = serve("/guide.xml", guideAround(System.currentTimeMillis()))
        val repo = EPGRepository(context)
        cacheOf(repo, url).writeText("<tv><channel id=\"x\" /></tv>") // valid, but no programmes
        repo.setEpgUrl(url)
        await("network load") { hits("/guide.xml") == 1 && repo.state.value.channelCount > 0 }
        assertEquals("Morning Show", firstTitle(repo))
    }

    @Test
    fun refreshDownloadsOnceAndCaches() {
        val url = serve("/guide.xml", guideAround(System.currentTimeMillis()))
        val repo = EPGRepository(context)
        repo.setEpgUrl(url)
        await("network load") { repo.state.value.channelCount > 0 }
        assertEquals("one download, not two", 1, hits("/guide.xml"))
        assertTrue(cacheOf(repo, url).isFile)
        assertEquals("Morning Show", firstTitle(repo))

        // The same URL again is a no-op, and a forced refresh downloads once more.
        repo.setEpgUrl(url)
        Thread.sleep(300)
        assertEquals(1, hits("/guide.xml"))
        repo.refresh()
        await("forced refresh") { hits("/guide.xml") == 2 && !repo.loading.value }
        assertEquals(2, hits("/guide.xml"))
    }

    @Test
    fun blankUrlClearsTheGuide() {
        val url = serve("/guide.xml", guideAround(System.currentTimeMillis()))
        val repo = EPGRepository(context)
        repo.setEpgUrl(url)
        await("network load") { repo.state.value.channelCount > 0 }
        repo.setEpgUrl("")
        assertEquals(0, repo.state.value.channelCount)
        assertFalse(repo.loading.value)
        repo.setEpgUrl("") // again: nothing to do
        assertEquals(0, repo.state.value.channelCount)
    }

    // ---------------------------------------------------------------- switching guides

    @Test
    fun switchingGuidesNeverShowsThePreviousPlaylistsListings() {
        val a = serve("/a.xml", guideAround(System.currentTimeMillis()))
        val b = serve("/b.xml", otherGuide())
        val repo = EPGRepository(context)
        repo.setEpgUrl(a)
        await("guide a") { repo.state.value.channelCount > 0 }
        assertEquals(1, hits("/a.xml"))

        repo.setEpgUrl(b)
        assertEquals("the old guide is dropped the moment the URL changes", 0, repo.state.value.channelCount)
        await("guide b") { repo.state.value.channelCount > 0 }
        assertEquals("Other Guide Title", firstTitle(repo))
        assertEquals("the new guide is fetched although a fresh cache exists", 1, hits("/b.xml"))

        // Back again: its own cache serves it, no second download.
        repo.setEpgUrl(a)
        await("guide a again") { firstTitle(repo) == "Morning Show" }
        assertEquals(1, hits("/a.xml"))
        assertEquals(1, hits("/b.xml"))
    }

    @Test
    fun aFailedFetchOfTheNewGuideNeverFallsBackToThePreviousOne() {
        var otherIsUp = false
        server.route("/b.xml") {
            if (otherIsUp) TinyHttpServer.Response(body = otherGuide().toByteArray()) else TinyHttpServer.Response(status = 500)
        }
        val a = serve("/a.xml", guideAround(System.currentTimeMillis()))
        val b = server.url("/b.xml")
        val repo = EPGRepository(context)
        repo.setEpgUrl(a)
        await("guide a") { repo.state.value.channelCount > 0 }

        repo.setEpgUrl(b)
        await("the failed fetch to settle") { hits("/b.xml") == 1 && !repo.loading.value }
        assertEquals("guide A's listings are not shown under guide B", 0, repo.state.value.channelCount)

        repo.setEpgUrl(b) // a recomposition: same URL, inside the back-off window
        Thread.sleep(300)
        assertEquals("no hammering a dead guide server", 1, hits("/b.xml"))
        assertEquals(0, repo.state.value.channelCount)

        otherIsUp = true
        repo.refresh()
        await("guide b") { repo.state.value.channelCount > 0 }
        assertEquals(2, hits("/b.xml"))
        assertEquals("Other Guide Title", firstTitle(repo))
    }

    /**
     * The second review's HIGH: after the back-off exit above, the finished
     * load was never cleared, so every later `ensure()` saw "a load is on its
     * way" and the guide never loaded again in that process.
     */
    @Test
    fun afterAFailedFetchTheGuideLoadsAgainOnceTheBackOffIsOver() {
        var up = false
        server.route("/b.xml") {
            if (up) TinyHttpServer.Response(body = otherGuide().toByteArray()) else TinyHttpServer.Response(status = 500)
        }
        val b = server.url("/b.xml")
        var now = System.currentTimeMillis()
        val repo = EPGRepository(context, clock = { now })
        repo.setEpgUrl(b)
        await("the failed fetch to settle") { hits("/b.xml") == 1 && !repo.loading.value }

        repo.setEpgUrl(b) // inside the back-off window: nothing happens…
        Thread.sleep(300)
        assertEquals(1, hits("/b.xml"))
        assertFalse("the finished load is not left 'in flight'", repo.loading.value)

        up = true
        now += EPGRepository.RETRY_BACKOFF_MS + 1_000 // the window is over
        repo.setEpgUrl(b) // a plain recomposition — no manual refresh
        await("guide b") { repo.state.value.channelCount > 0 }
        assertEquals(2, hits("/b.xml"))
        assertEquals("Other Guide Title", firstTitle(repo))
        assertFalse(repo.loading.value)
    }

    @Test
    fun switchingFromALoadingGuideToACachedOneEndsTheLoadingState() {
        server.route("/a.xml") {
            TinyHttpServer.Response(body = guideAround(System.currentTimeMillis()).toByteArray() + ByteArray(256 * 1024) { ' '.code.toByte() }, chunkSize = 8 * 1024, chunkDelayMs = 100)
        }
        val a = server.url("/a.xml")
        val repo = EPGRepository(context)
        val b = server.url("/b.xml")
        cacheOf(repo, b).writeText(otherGuide())
        repo.setEpgUrl(a)
        await("guide a loading") { partialOf(repo, a)?.let { it.length() > 0 } == true }

        repo.setEpgUrl(b)
        await("guide b from its cache") { firstTitle(repo) == "Other Guide Title" }
        assertFalse("the spinner follows the CURRENT guide", repo.loading.value)
    }

    @Test
    fun aSlowFetchOfThePreviousGuideIsDiscardedAndDoesNotDelayTheNewOne() {
        // Guide A trickles for about five seconds.
        val slow = guideAround(System.currentTimeMillis()).toByteArray() + ByteArray(400 * 1024) { ' '.code.toByte() }
        server.route("/a.xml") { TinyHttpServer.Response(body = slow, chunkSize = 8 * 1024, chunkDelayMs = 100) }
        val a = server.url("/a.xml")
        val b = serve("/b.xml", otherGuide())
        val repo = EPGRepository(context)
        repo.setEpgUrl(a)
        await("guide A's download to be under way") { partialOf(repo, a)?.let { it.length() > 0 } == true }

        val t0 = System.currentTimeMillis()
        repo.setEpgUrl(b)
        await("guide b") { repo.state.value.channelCount > 0 }
        assertEquals("Other Guide Title", firstTitle(repo))
        assertTrue(
            "guide B did not wait for guide A's download: took ${System.currentTimeMillis() - t0} ms",
            System.currentTimeMillis() - t0 < 2500,
        )

        // The discarded fetch ends — aborted, or read to its end and thrown
        // away — and removes its own partial file.
        await("the discarded fetch to finish") { partialOf(repo, a) == null }
        assertEquals("guide A's late answer replaced nothing", "Other Guide Title", firstTitle(repo))
        assertFalse("the fetch the user switched away from was never cached", cacheOf(repo, a).exists())
        assertFalse(repo.loading.value)
        assertEquals(1, hits("/a.xml"))
        assertEquals(1, hits("/b.xml"))
    }

    @Test
    fun onlyTheMostRecentFewGuidesStayCached() {
        val repo = EPGRepository(context)
        val urls = (1..5).map { n -> serve("/g$n.xml", guideAround(System.currentTimeMillis(), firstTitle = "Guide $n")) }
        for ((i, u) in urls.withIndex()) {
            repo.setEpgUrl(u)
            await("guide ${i + 1}") { firstTitle(repo) == "Guide ${i + 1}" }
        }
        val cached = context.filesDir.listFiles().orEmpty().filter { it.name.startsWith("epg_") && it.name.endsWith(".xml") }
        assertEquals("guides reach 100+ MB: three stay, the rest goes", urls.takeLast(3).map { repo.cacheNameFor(it) }.toSet(), cached.map { it.name }.toSet())
        repo.setEpgUrl(urls[0]) // the oldest was dropped: it is fetched again
        await("guide 1 again") { hits("/g1.xml") == 2 && firstTitle(repo) == "Guide 1" }
    }
}
