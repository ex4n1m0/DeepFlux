package space.deepflux.android

import android.app.Application
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
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
 * Review round 2026-11-24 — the torrent-add confirmation and the start-up
 * order of the torrent repository:
 *  - a .torrent request that finishes READING after a newer request used to
 *    replace the newer dialog with its own;
 *  - a page could re-open the dialog the user had just dismissed with every
 *    redirect round;
 *  - a .torrent shared by another app was copied without a size limit, and
 *    staged files from confirmations that never came stayed in cacheDir;
 *  - one failed engine start was final (nothing downloaded until the next
 *    app start), and the saved list waited for that start.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class TorrentRequestOrderTest {

    private val hA = "0000000000000000000000000000000000000011"
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

    private fun newRepo(): TorrentRepository = TorrentRepository(context, engine).also { repos += it }

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

    /** A valid single-file torrent; the name makes its info-hash unique. */
    private fun torrentText(name: String): String = "d" + bstr("announce") + bstr("udp://tracker.example:80") +
        bstr("info") + "d" + bstr("length") + "i1024e" + bstr("name") + bstr(name) +
        bstr("piece length") + "i16384e" + bstr("pieces") + "20:" + "A".repeat(20) + "e" + "e"

    private fun staged(name: String, torrentName: String) =
        File(context.cacheDir, name).apply { writeText(torrentText(torrentName)) }

    private fun savedState(hash: String) = """[{"h":"$hash","m":"magnet:?xt=urn:btih:$hash","n":"Saved","s":false,"a":1}]"""

    // ---------------------------------------------------------------- order of requests

    /** A repository whose staged-file read blocks until [release] is counted down. */
    private fun repoWithBlockingRead(release: CountDownLatch, reading: CountDownLatch): TorrentRepository =
        TorrentRepository(
            context, engine,
            readStagedTorrent = { f ->
                reading.countDown()
                release.await(10, TimeUnit.SECONDS)
                TorrentMeta.readTorrent(f.readBytes())
            },
        ).also { repos += it }

    @Test
    fun aFileRequestThatFinishesReadingLateDoesNotReplaceANewerOne() {
        val release = CountDownLatch(1)
        val reading = CountDownLatch(1)
        val r = repoWithBlockingRead(release, reading).also { it.start() }
        val fileA = staged("web-a.torrent", "Request A")
        r.requestAddTorrentFile(fileA, sourceHost = "slow.example")
        assertTrue("request A arrived and is still reading", reading.await(10, TimeUnit.SECONDS))

        r.requestAddMagnet(magnetB, sourceHost = "fast.example") // arrives later, ready at once
        assertEquals(hB, r.pendingAdd.value?.infoHash)

        release.countDown() // A's read ends — after B is already shown
        await("A's staged file discarded") { !fileA.exists() }
        Thread.sleep(150) // the same worker would show A right after the read
        assertEquals("the newer request stays on screen", hB, r.pendingAdd.value?.infoHash)
    }

    @Test
    fun aFileRequestOlderThanOneTheUserAnsweredIsNotShown() {
        val release = CountDownLatch(1)
        val reading = CountDownLatch(1)
        val r = repoWithBlockingRead(release, reading).also { it.start() }
        val fileA = staged("web-a.torrent", "Request A")
        r.requestAddTorrentFile(fileA, sourceHost = "slow.example")
        assertTrue(reading.await(10, TimeUnit.SECONDS))
        r.requestAddMagnet(magnetB, sourceHost = "fast.example")
        r.cancelPendingAdd() // the user answered B
        assertNull(r.pendingAdd.value)

        release.countDown()
        await("A's staged file discarded") { !fileA.exists() }
        Thread.sleep(150)
        assertNull("what the user already answered is not asked again, in an older form", r.pendingAdd.value)
    }

    // ---------------------------------------------------------------- just declined

    @Test
    fun aPageCannotReAskForATorrentTheUserJustDeclined() {
        var now = 1_000_000L
        val r = TorrentRepository(context, engine, clock = { now }).also { repos += it }.also { it.start() }
        r.requestAddMagnet(magnetB, sourceHost = "page.example")
        assertNotNull(r.pendingAdd.value)
        r.cancelPendingAdd()

        r.requestAddMagnet(magnetB, sourceHost = "page.example")
        assertNull("the page re-firing the same magnet right after Cancel", r.pendingAdd.value)
        now += 3_000
        r.requestAddMagnet(magnetB, sourceHost = "page.example")
        assertNull("still inside the quiet time (3 s of 5)", r.pendingAdd.value)
        now += 3_000 // 6 s after the Cancel: the window is fixed, not restarted by the asks it swallowed
        r.requestAddMagnet(magnetB, sourceHost = "page.example")
        assertNotNull("after the quiet time the torrent may be offered again", r.pendingAdd.value)
        r.cancelPendingAdd()
    }

    @Test
    fun theUsersOwnTapIsNeverHeldBack() {
        val r = newRepo().also { it.start() }
        r.requestAddMagnet(magnetB, sourceHost = "page.example")
        r.cancelPendingAdd()
        r.requestAddMagnet(magnetB, sourceHost = "page.example", userInitiated = true)
        assertNotNull("the links sheet and shares are the user's own hand", r.pendingAdd.value)
    }

    @Test
    fun aPageCannotReStageTheFileTheUserJustDeclined() {
        val r = newRepo().also { it.start() }
        val file = staged("web-a.torrent", "Request A")
        r.requestAddTorrentFile(file, sourceHost = "page.example")
        await("the first request") { r.pendingAdd.value != null }
        r.cancelPendingAdd()
        assertFalse(file.exists())

        val again = staged("web-a2.torrent", "Request A") // same torrent, served again
        r.requestAddTorrentFile(again, sourceHost = "page.example")
        await("the re-staged file discarded") { !again.exists() }
        Thread.sleep(150)
        assertNull(r.pendingAdd.value)
    }

    // ---------------------------------------------------------------- staged files + shares

    @Test
    fun stagedTorrentsFromEarlierRunsAreSweptAtStartup() {
        val old = System.currentTimeMillis() - 60 * 60 * 1000L
        fun oldFile(name: String) = File(context.cacheDir, name).apply { writeText("d"); setLastModified(old) }
        val web = oldFile("web-1.torrent")
        val rss = oldFile("rss-2.torrent")
        val imported = oldFile("import-3.torrent")
        val otherTorrent = oldFile("my-backup.torrent")
        val otherFile = oldFile("web-notes.txt")
        val fresh = File(context.cacheDir, "import-4.torrent").apply { writeText("d") }
        val r = newRepo().also { it.start() }

        await("the sweep") { !web.exists() }
        assertFalse(rss.exists())
        assertFalse(imported.exists())
        assertTrue("a file staged a moment ago may belong to a request in flight", fresh.isFile)
        assertTrue("only the staging names are ours to delete", otherTorrent.isFile && otherFile.isFile)
    }

    @Test
    fun aTorrentSharedByAnotherAppIsStagedAndConfirmed() {
        val r = newRepo().also { it.start() }
        val shared = staged("shared.torrent", "Shared torrent")
        r.addTorrentFromUri(Uri.fromFile(shared))
        await("the confirmation") { r.pendingAdd.value != null }
        assertEquals("Shared torrent", r.pendingAdd.value?.name)
        assertTrue("a copy is staged; the shared file itself is untouched", shared.isFile)
    }

    @Test
    fun anOversizedTorrentFromAnotherAppIsNotCopiedWhole() {
        val r = newRepo().also { it.start() }
        val notices = noticesOf(r)
        val cap = TorrentRepository.MAX_PENDING_TORRENT_BYTES
        // Three times the cap, counted as it leaves the "other app".
        val source = CountingSource(cap * 3)
        val uri = Uri.parse("content://other.app/shared/big.torrent")
        org.robolectric.Shadows.shadowOf(context.contentResolver).registerInputStream(uri, source)

        r.addTorrentFromUri(uri)
        await("the refusal") { notices.isNotEmpty() }
        assertNull(r.pendingAdd.value)
        await("no partial copy left behind") { context.cacheDir.listFiles().orEmpty().none { it.name.startsWith("import-") } }
        assertTrue(
            "reading stops at the cap: took ${source.taken} of ${source.size} bytes",
            source.taken in 1..(cap + 256 * 1024).toLong(),
        )
    }

    /** An endless-looking stream that counts what was taken from it. */
    private class CountingSource(val size: Int) : java.io.InputStream() {
        var taken = 0L
            private set

        override fun read(): Int {
            if (taken >= size) return -1
            taken++
            return 'd'.code
        }

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (taken >= size) return -1
            val n = minOf(len.toLong(), size - taken).toInt()
            taken += n
            return n
        }
    }

    @Test
    fun confirmingARequestWhoseFileIsGoneSaysSo() {
        val r = newRepo().also { it.start() }
        val notices = noticesOf(r)
        val file = staged("web-a.torrent", "Request A")
        r.requestAddTorrentFile(file, sourceHost = "page.example")
        await("the request") { r.pendingAdd.value != null }
        file.delete() // the system trimmed the cache while the dialog was up
        r.confirmPendingAdd()
        await("a notice, not silence") { notices.isNotEmpty() }
        assertTrue(engine.files.isEmpty())
        assertNull(r.pendingAdd.value)
    }

    // ---------------------------------------------------------------- what the confirmation shows

    @Test
    fun aNameWithLineBreaksOrBidiOverridesIsShownClean() {
        fun shown(name: String) = space.deepflux.android.engine.TorrentRepository.confirmationName(name)
        assertEquals("a plain name is untouched", "Ubuntu 24.04", shown("Ubuntu 24.04"))
        // Line breaks used to push the dialog's buttons around.
        assertEquals("Ubuntu 24.04", shown("Ubuntu\n\r\n24.04"))
        assertEquals("one two two", shown("one\ttwo   two"))
        // A right-to-left override can make a name read as something else.
        assertEquals("reportexe.txt", shown("report\u202Eexe.txt"))
        assertEquals("no zero-width or isolates left", "clean name", shown("cle\u200Ban\u2066 name\u2069\ufeff"))
        assertEquals("", shown(" \u202E "))
        assertEquals("cut to 200 characters", 200, shown("n".repeat(600)).length)
    }

    @Test
    fun aRequestFromAFrameIsToldApartFromOneByThePageItself() {
        val r = newRepo().also { it.start() }
        r.requestAddMagnet(magnetB, sourceHost = "top.example", fromSubframe = true)
        assertEquals("top.example", r.pendingAdd.value?.sourceHost)
        assertTrue("the dialog must not lend the top page's name to a frame's request", r.pendingAdd.value!!.fromSubframe)
        r.cancelPendingAdd()
        r.requestAddMagnet(magnetB, sourceHost = "top.example", userInitiated = true)
        assertEquals(false, r.pendingAdd.value?.fromSubframe)
    }

    // ---------------------------------------------------------------- start-up

    @Test
    fun aFailedEngineStartIsRetriedAndTheSavedTorrentsFollow() {
        java.io.File(context.filesDir, "torrents_state.json").writeText(savedState(hB))
        engine.startFailures = 2
        val r = TorrentRepository(context, engine, engineStartRetryMs = 10).also { repos += it }.also { it.start() }
        val notices = noticesOf(r)
        await("the engine up on the third try") { engine.starts >= 3 }
        await("saved torrents are added once it runs") { engine.magnets.size == 1 }
        assertTrue("no failure notice for a start that succeeded in the end", notices.isEmpty())
    }

    @Test
    fun afterTheLastFailedStartTheUserIsToldAndNothingIsAdded() {
        engine.startFailures = 50
        val r = TorrentRepository(context, engine, engineStartRetryMs = 10).also { repos += it }.also { it.start() }
        val notices = noticesOf(r)
        await("the failure notice") { notices.isNotEmpty() }
        assertEquals(listOf(Strings.t.noticeEngineStartFailed), notices.toList())
        assertEquals("four tries, no more", 4, engine.starts)
        assertTrue("nothing is added to an engine that is not there", engine.magnets.isEmpty())
    }
}
