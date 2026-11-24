package space.deepflux.android

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.util.concurrent.CopyOnWriteArrayList
import space.deepflux.android.ui.Strings
import kotlinx.coroutines.launch
import kotlinx.coroutines.Job
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CoroutineScope
import space.deepflux.android.dlmgr.HttpDownloadRepository
import space.deepflux.android.dlmgr.HttpState
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.rss.RssRepository
import java.io.File

/**
 * Review round 2026-11-24 — where an RSS item's link may lead. The link is
 * the FEED's, and auto-download follows it with nobody watching: it used to
 * be fetched unchecked, redirects included, so a feed could make the app
 * download from the user's own network (router, NAS) — as a plain file or
 * as a ".torrent" that was then added.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class RssLinkPolicyTest {

    private lateinit var context: RecordingContext
    private lateinit var engine: FakeTorrentEngine
    private lateinit var torrents: TorrentRepository
    private lateinit var server: TinyHttpServer
    private lateinit var http: HttpDownloadRepository
    private lateinit var rss: RssRepository

    private val outside = "Local and private network URLs are not allowed"

    /** The one address the policy treats as public: a test's own start URL. Everything else is "the user's network". */
    @Volatile
    private var allowedUrl = ""

    @Before
    fun setUp() {
        context = RecordingContext(ApplicationProvider.getApplicationContext())
        engine = FakeTorrentEngine()
        torrents = TorrentRepository(context, engine).also { it.start() }
        server = TinyHttpServer()
        http = HttpDownloadRepository(
            context, File(context.filesDir, "dl"), gateOk = { true }, notify = {},
            urlPolicy = { u -> if (u == allowedUrl) null else outside },
        )
        rss = RssRepository(context, torrents, http, feedUrlPolicy = { u -> if (u == allowedUrl) null else outside })
    }

    @After
    fun tearDown() {
        collectors.forEach { it.cancel() }
        server.close()
        torrents.shutdownForTests()
    }

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 10_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(25)
        }
    }

    private fun hits(path: String) = server.requests.count { it.path == path }

    /** Serves a one-item feed (guid "g0") and returns its URL. */
    private fun serveFeed(title: String, link: String): String {
        val xml = "<?xml version=\"1.0\"?><rss version=\"2.0\"><channel><title>Test feed</title>" +
            "<item><title>$title</title><link>$link</link><guid>g0</guid></item></channel></rss>"
        server.route("/feed.xml") { TinyHttpServer.Response(body = xml.toByteArray(), headers = mapOf("Content-Type" to "application/rss+xml")) }
        return server.url("/feed.xml")
    }

    private fun serveEmptyFeed(): String {
        server.route("/feed.xml") {
            TinyHttpServer.Response(
                body = "<?xml version=\"1.0\"?><rss version=\"2.0\"><channel><title>Test feed</title></channel></rss>".toByteArray(),
                headers = mapOf("Content-Type" to "application/rss+xml"),
            )
        }
        return server.url("/feed.xml")
    }

    /** [path] answers 302 → a "LAN" address; returns the start URL. */
    private fun redirectIntoLan(path: String): String {
        server.route("/lan-secret") { TinyHttpServer.Response(body = ByteArray(64) { 7 }) }
        server.route(path) { TinyHttpServer.Response(status = 302, headers = mapOf("Location" to server.url("/lan-secret"))) }
        return server.url(path)
    }

    // ---------------------------------------------------------------- file links

    @Test
    fun anItemLinkIsNotFollowedIntoTheUsersNetwork() {
        val start = redirectIntoLan("/start")
        allowedUrl = start
        val f = rss.addFeed(serveFeed("Innocent file", start)).getOrThrow()

        assertTrue("queued — the refusal is the engine's to report, in the list", rss.downloadItem(f.id, "g0"))
        await("the refusal") { http.items.value.any { it.state == HttpState.ERROR } }
        val item = http.items.value.single()
        assertTrue(item.error, item.error.startsWith(outside))
        assertEquals("the LAN address was never requested", 0, hits("/lan-secret"))
    }

    @Test
    fun anItemLinkIsNotFollowedIntoTheUsersNetworkByAutoDownloadEither() {
        val start = redirectIntoLan("/start")
        allowedUrl = start
        val f = rss.addFeed(serveEmptyFeed(), keywords = listOf("innocent"), autoDownload = true).getOrThrow()
        serveFeed("Innocent file", start) // published after the feed was added

        rss.refresh(f.id).getOrThrow()
        await("the auto-download's refusal") { http.items.value.any { it.state == HttpState.ERROR && it.error.startsWith(outside) } }
        assertEquals("the LAN address was never requested", 0, hits("/lan-secret"))
    }

    // ---------------------------------------------------------------- .torrent links

    /** A valid single-file torrent. */
    private fun bstr(s: String): String {
        val bytes = s.toByteArray(Charsets.UTF_8)
        return "${bytes.size}:$s"
    }

    private val torrentBytes: ByteArray = (
        "d" + bstr("announce") + bstr("udp://tracker.example:80") +
            bstr("info") + "d" + bstr("length") + "i1024e" + bstr("name") + bstr("From the feed") +
            bstr("piece length") + "i16384e" + bstr("pieces") + "20:" + "A".repeat(20) + "e" + "e"
        ).toByteArray()

    @Test
    fun aTorrentLinkIsNotFetchedFromTheUsersNetwork() {
        val start = redirectIntoLan("/start.torrent")
        allowedUrl = start
        val f = rss.addFeed(serveFeed("Innocent torrent", start)).getOrThrow()

        assertFalse("not accepted — and so not marked seen", rss.downloadItem(f.id, "g0"))
        assertEquals("the LAN address was never requested", 0, hits("/lan-secret"))
        assertTrue("no torrent was added", engine.files.isEmpty() && engine.magnets.isEmpty())
    }

    @Test
    fun aPublicTorrentLinkIsFetchedAndAdded() {
        server.route("/ok.torrent") {
            TinyHttpServer.Response(body = torrentBytes, headers = mapOf("Content-Type" to "application/x-bittorrent"))
        }
        val start = server.url("/ok.torrent")
        allowedUrl = start
        val f = rss.addFeed(serveFeed("Good torrent", start)).getOrThrow()

        assertTrue(rss.downloadItem(f.id, "g0"))
        await("the torrent added") { engine.files.size == 1 }
        assertEquals(1, hits("/ok.torrent"))
    }

    @Test
    fun aTorrentLinkThatIsNotATorrentIsNotAdded() {
        server.route("/page.torrent") { TinyHttpServer.Response(body = "<html>login page</html>".toByteArray()) }
        val start = server.url("/page.torrent")
        allowedUrl = start
        val f = rss.addFeed(serveFeed("Not a torrent", start)).getOrThrow()

        assertFalse(rss.downloadItem(f.id, "g0"))
        assertTrue(engine.files.isEmpty() && engine.magnets.isEmpty())
    }

    // ---------------------------------------------------------------- a refused item is reported

    private val collectors = mutableListOf<Job>()

    private fun notices(): MutableList<String> {
        val seen = CopyOnWriteArrayList<String>()
        collectors += CoroutineScope(Dispatchers.Unconfined).launch { torrents.notices.collect { seen.add(it) } }
        return seen
    }

    @Test
    fun aRefusedTorrentLinkIsReportedNotSilent() {
        val notices = notices()
        val start = redirectIntoLan("/start.torrent")
        allowedUrl = start
        val f = rss.addFeed(serveFeed("Innocent torrent", start)).getOrThrow()

        assertFalse(rss.downloadItem(f.id, "g0"))

        assertEquals("the button does something visible", listOf(Strings.t.noticeRssItemRefused), notices.toList())
    }

    @Test
    fun anAcceptedItemIsNotComplainedAbout() {
        val notices = notices()
        server.route("/ok.torrent") {
            TinyHttpServer.Response(body = torrentBytes, headers = mapOf("Content-Type" to "application/x-bittorrent"))
        }
        val start = server.url("/ok.torrent")
        allowedUrl = start
        val f = rss.addFeed(serveFeed("Good torrent", start)).getOrThrow()

        assertTrue(rss.downloadItem(f.id, "g0"))
        await("the torrent added") { engine.files.size == 1 }
        assertTrue(notices.none { it == Strings.t.noticeRssItemRefused })
    }

    // ---------------------------------------------------------------- the feed's own address (second review round)

    /** A feed whose address redirects onto a "LAN" address: returns the feed's start URL. */
    private fun feedRedirectingIntoLan(path: String): String {
        val feed = ("<?xml version=\"1.0\"?><rss version=\"2.0\"><channel><title>Redirected feed</title>" +
            "<item><title>Old item</title><link>http://x/old</link><guid>g9</guid></item></channel></rss>").toByteArray()
        server.route("/lan-feed") { TinyHttpServer.Response(body = feed, headers = mapOf("Content-Type" to "application/rss+xml")) }
        server.route(path) { TinyHttpServer.Response(status = 302, headers = mapOf("Location" to server.url("/lan-feed"))) }
        return server.url(path)
    }

    @Test
    fun aFeedTheAgentAddedIsNotFetchedViaARedirectIntoTheUsersNetwork() {
        val start = feedRedirectingIntoLan("/feed-start")
        allowedUrl = start

        val refused = rss.addFeed(start, publicOnly = true)
        assertTrue("refused: ${refused.exceptionOrNull()}", refused.isFailure)
        assertTrue(refused.exceptionOrNull()!!.message.orEmpty().contains(outside))
        assertEquals("the LAN address was never requested", 0, hits("/lan-feed"))
        assertTrue("not saved", rss.feeds.value.isEmpty())
    }

    @Test
    fun aFeedTheUserTypedMayRedirectWhereverTheyChose() {
        val start = feedRedirectingIntoLan("/feed-start")
        allowedUrl = start

        val feed = rss.addFeed(start).getOrThrow()
        assertEquals("Redirected feed", feed.title)
        assertEquals(1, hits("/lan-feed"))
        assertFalse("the user's own choice carries no restriction", feed.publicOnly)
    }

    @Test
    fun theRestrictionIsSavedAndHoldsOnEveryLaterPoll() {
        val start = feedRedirectingIntoLan("/feed-start")
        allowedUrl = start
        // What an earlier process saved for an agent-added feed.
        File(context.filesDir, "rss_feeds.json").writeText(
            org.json.JSONArray().put(
                org.json.JSONObject().put("id", "f1").put("url", start).put("title", "Redirected feed")
                    .put("auto", false).put("keywords", org.json.JSONArray()).put("at", 1).put("fetched", 0)
                    .put("err", "").put("seen", org.json.JSONArray()).put("po", true),
            ).toString(),
        )
        val later = RssRepository(context, torrents, http, feedUrlPolicy = { u -> if (u == allowedUrl) null else outside })
        assertTrue(later.feeds.value.single().publicOnly)

        val poll = later.refresh("f1")
        assertTrue("the poll is refused the same way", poll.isFailure)
        assertEquals("the LAN address is never requested", 0, hits("/lan-feed"))
        assertTrue("the failure shows in the feed's error line", later.feeds.value.single().lastError.contains(outside.take(20)))
    }

    @Test
    fun anAgentAddedFeedKeepsItsRestrictionAcrossRestarts() {
        val direct = serveEmptyFeed()
        allowedUrl = direct
        val added = rss.addFeed(direct, publicOnly = true).getOrThrow()
        assertTrue(added.publicOnly)
        val later = RssRepository(context, torrents, http, feedUrlPolicy = { u -> if (u == allowedUrl) null else outside })
        assertTrue(later.feeds.value.single().publicOnly)
    }

    // ---------------------------------------------------------------- seen only once the engine has it

    @Test
    fun aTorrentItemIsTriedAgainWhenItsImportFailed() {
        // Passes the "starts with d" check; the torrent reader then rejects it.
        server.route("/bad.torrent") { TinyHttpServer.Response(body = "d-this-is-not-bencode".toByteArray()) }
        val link = server.url("/bad.torrent")
        allowedUrl = link
        val f = rss.addFeed(serveEmptyFeed(), keywords = listOf("innocent"), autoDownload = true).getOrThrow()
        serveFeed("Innocent torrent", link) // published after the feed was added

        rss.refresh(f.id).getOrThrow()
        await("the first attempt") { hits("/bad.torrent") == 1 }
        Thread.sleep(400) // the failed import settles
        rss.refresh(f.id).getOrThrow()
        await("tried again — it was never marked seen") { hits("/bad.torrent") == 2 }
        assertTrue("and of course nothing was added", engine.files.isEmpty() && engine.magnets.isEmpty())
    }
}
