package space.deepflux.android

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.engine.TorrentMeta
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.ui.Strings
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Second review round (2026-11-24) on the torrent-add flow and the repository:
 *  - a page could push the user's own request out of the confirmation (and
 *    drop the user's slow .torrent as "older" when it finally arrived);
 *  - a page cycling through fresh magnets re-opened the modal dialog after
 *    every Cancel — a way to lock the user out of their own app;
 *  - an add that arrived while the engine was still starting failed;
 *  - a magnet whose metadata carries a symbolic link stayed in the engine;
 *  - the outcome of a .torrent import was not reported (RSS marked items
 *    seen before knowing), and removing the last "Pause all" torrent through
 *    the agent left a dead "Resume all" notice.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class AddRequestFlowTest {

    private val hB = "0000000000000000000000000000000000000012"
    private val magnetB = "magnet:?xt=urn:btih:$hB&dn=Ubuntu+24.04"
    private lateinit var context: RecordingContext
    private lateinit var engine: FakeTorrentEngine
    private val repos = mutableListOf<TorrentRepository>()
    private val collectors = mutableListOf<Job>()

    @Before
    fun setUp() {
        context = RecordingContext(ApplicationProvider.getApplicationContext())
        engine = FakeTorrentEngine()
    }

    @After
    fun tearDown() {
        collectors.forEach { it.cancel() }
        repos.forEach { it.shutdownForTests() }
    }

    private fun await(what: String, seconds: Long = 5, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + seconds * 1000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(25)
        }
    }

    /** Starts collecting the repository's snackbar notices BEFORE anything can emit. */
    private fun noticesOf(r: TorrentRepository): MutableList<String> {
        val seen = CopyOnWriteArrayList<String>()
        collectors += CoroutineScope(Dispatchers.Unconfined).launch { r.notices.collect { seen.add(it) } }
        return seen
    }

    private fun bstr(s: String): String {
        val bytes = s.toByteArray(Charsets.UTF_8)
        return "${bytes.size}:$s"
    }

    private fun torrentText(name: String): String = "d" + bstr("announce") + bstr("udp://tracker.example:80") +
        bstr("info") + "d" + bstr("length") + "i1024e" + bstr("name") + bstr(name) +
        bstr("piece length") + "i16384e" + bstr("pieces") + "20:" + "A".repeat(20) + "e" + "e"

    private fun staged(name: String, torrentName: String) =
        File(context.cacheDir, name).apply { writeText(torrentText(torrentName)) }

    private fun magnetOf(i: Int) = "magnet:?xt=urn:btih:%040x".format(0x1000L + i)

    // ---------------------------------------------------------------- the user's own request is never displaced

    @Test
    fun aPageFiredMagnetNeverPushesAsideTheUsersOwn() {
        val r = TorrentRepository(context, engine).also { repos += it }.also { it.start() }
        r.requestAddMagnet(magnetB, userInitiated = true)
        assertEquals("the user's request is showing", hB, r.pendingAdd.value?.infoHash)
        assertTrue(r.pendingAdd.value!!.userInitiated)

        val pageMagnet = magnetOf(1)
        r.requestAddMagnet(pageMagnet, sourceHost = "page.example")
        assertEquals("still the user's", hB, r.pendingAdd.value?.infoHash)
        // The user answers; the page's dropped request does not come back later.
        r.confirmPendingAdd()
        await("the user's add") { engine.magnets.any { it == magnetB } }
        assertNull(r.pendingAdd.value)
        assertTrue("the page's magnet never reached the engine", engine.magnets.none { it == pageMagnet })
    }

    @Test
    fun aPageFiredMagnetCannotOvertakeTheUsersSlowTorrentFile() {
        val release = CountDownLatch(1)
        val reading = CountDownLatch(1)
        val r = TorrentRepository(
            context, engine,
            readStagedTorrent = { f ->
                reading.countDown()
                release.await(10, TimeUnit.SECONDS)
                TorrentMeta.readTorrent(f.readBytes())
            },
        ).also { repos += it }.also { it.start() }
        val mine = staged("web-mine.torrent", "Mine")
        r.requestAddTorrentFile(mine, sourceHost = "page.example", userInitiated = true) // the links sheet
        assertTrue("the user's file is being fetched", reading.await(10, TimeUnit.SECONDS))

        r.requestAddMagnet(magnetOf(2), sourceHost = "page.example")
        assertNull("nothing of the page's appears while the user's own request is on its way", r.pendingAdd.value)

        release.countDown()
        await("the user's file shows") { r.pendingAdd.value != null }
        assertEquals("Mine", r.pendingAdd.value?.name)
        assertTrue(r.pendingAdd.value!!.userInitiated)
    }

    // ---------------------------------------------------------------- a volley of fresh magnets

    @Test
    fun aPageCyclingThroughFreshMagnetsGetsThreeDialogsAMinuteAndThenSilence() {
        var now = 1_000_000L
        val r = TorrentRepository(context, engine, clock = { now }).also { repos += it }.also { it.start() }
        val notices = noticesOf(r)

        r.requestAddMagnet(magnetOf(1), sourceHost = "page.example")
        r.cancelPendingAdd()
        r.requestAddMagnet(magnetOf(2), sourceHost = "page.example")
        r.cancelPendingAdd()
        r.requestAddMagnet(magnetOf(3), sourceHost = "page.example")
        r.cancelPendingAdd()
        // Each was a different torrent: the decline window alone never held any of them.
        assertEquals(0, notices.count { it == Strings.t.noticePageAddStorm })

        for (i in 4..8) r.requestAddMagnet(magnetOf(i), sourceHost = "page.example")
        assertNull("beyond three per minute nothing is shown", r.pendingAdd.value)
        assertEquals("and the user is told why — once", 1, notices.count { it == Strings.t.noticePageAddStorm })

        now += 61_000 // the window has passed
        r.requestAddMagnet(magnetOf(9), sourceHost = "page.example")
        assertEquals("asked for again after the window", "0000000000000000000000000000000000001009", r.pendingAdd.value?.infoHash)
        r.cancelPendingAdd()
    }

    // ---------------------------------------------------------------- the engine is still starting

    @Test
    fun anAddThatArrivesWhileTheEngineIsStillStartingWaitsForIt() {
        // The first attempt fails; the second (a moment later) succeeds. An
        // add issued at once used to run in the gap between the two — into
        // an engine with no session — and fail with "could not add".
        engine.startFailures = 1
        val r = TorrentRepository(context, engine, engineStartRetryMs = 40).also { repos += it }.also { it.start() }
        val notices = noticesOf(r)
        r.addMagnet(magnetB)
        val file = staged("web-early.torrent", "Early bird")
        var fileResult: Boolean? = null
        r.addTorrentFile(file) { fileResult = it } // the same moment, by file
        await("the adds") { engine.magnets.any { it == magnetB } && fileResult != null }
        assertEquals("the engine needed two tries", 2, engine.starts)
        assertEquals("the file import waited too, and got in", true, fileResult)
        assertTrue("not failed for arriving too early", notices.none { it == Strings.t.noticeCouldNotAddMagnet })
    }

    // ---------------------------------------------------------------- symbolic links in a torrent

    @Test
    fun aMagnetWhoseMetadataCarriesASymbolicLinkIsRemovedAgain() {
        val r = TorrentRepository(context, engine).also { repos += it }.also { it.start() }
        val notices = noticesOf(r)
        r.addMagnet(magnetB)
        await("the add") { engine.magnets.any { it == magnetB } }
        // What the engine reports once the metadata has arrived.
        engine.symlinkHashes.add(hB)

        runBlocking { r.removeSymlinkTorrent(hB) }

        assertEquals(listOf(hB), engine.removed)
        assertTrue("told why", notices.any { it == Strings.t.noticeTorrentHasSymlinks })
        await("the removal is saved") {
            runCatching { java.io.File(context.filesDir, "torrents_state.json").readText().contains(hB) }.getOrDefault(true).not()
        }
    }

    // ---------------------------------------------------------------- outcomes

    @Test
    fun theTorrentFileImportReportsWhetherTheTorrentGotIn() {
        val r = TorrentRepository(context, engine).also { repos += it }.also { it.start() }
        val good = staged("web-good.torrent", "Good")
        var goodResult: Boolean? = null
        r.addTorrentFile(good) { goodResult = it }
        await("the good import") { goodResult != null }
        assertEquals(true, goodResult)

        val bad = File(context.cacheDir, "web-bad.torrent").apply { writeText("d-not-really-bencode") }
        var badResult: Boolean? = null
        r.addTorrentFile(bad) { badResult = it }
        await("the bad import") { badResult != null }
        assertEquals("refused, and reported", false, badResult)
    }

    @Test
    fun removingTheLastPauseAllTorrentEndsResumeAllAndSaysSo() {
        val r = TorrentRepository(context, engine).also { repos += it }.also { it.start() }
        var changes = 0
        r.onPauseAllChanged = { changes++ }
        r.addMagnet(magnetB)
        await("the add") { savedSize() == 1 }
        r.pauseAll()
        assertTrue(r.pausedByPauseAll.value)
        val before = changes
        r.remove(hB)
        await("the notice hook fires") { changes > before }
        assertFalse("nothing waits for Resume all any more", r.pausedByPauseAll.value)
    }

    private fun savedSize(): Int = runCatching {
        org.json.JSONArray(java.io.File(context.filesDir, "torrents_state.json").readText()).length()
    }.getOrDefault(-1)
}
