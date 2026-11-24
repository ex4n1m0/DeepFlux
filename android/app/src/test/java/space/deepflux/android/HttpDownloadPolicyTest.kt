package space.deepflux.android

import android.app.Application
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.dlmgr.HttpDownloadRepository
import space.deepflux.android.dlmgr.HttpState
import java.io.File

/**
 * Review round 1 (2026-11-24) on the HTTP download engine:
 *  - [HIGH] a file name from the agent, a server header or a page could carry
 *    "../" and plant a file anywhere in the app's private storage;
 *  - a public URL could 302 an agent-added download into the user's own network;
 *  - a download the network gate paused stayed paused forever after the
 *    process died.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class HttpDownloadPolicyTest {

    private lateinit var context: Context
    private lateinit var server: TinyHttpServer
    private lateinit var saveDir: File
    @Volatile private var gateOpen = true

    private val body = ByteArray(256 * 1024) { (it % 97).toByte() }

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        server = TinyHttpServer()
        saveDir = File(context.filesDir, "dl").apply { mkdirs() }
    }

    @After
    fun tearDown() {
        server.close()
    }

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 15_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(20)
        }
    }

    private fun repo(policy: (String) -> String? = { null }) =
        HttpDownloadRepository(context, saveDir, gateOk = { gateOpen }, notify = {}, urlPolicy = policy)

    // ---------------------------------------------------------------- file names

    @Test
    fun proposedFileNamesAreReducedToAPlainNameInsideTheSaveDir() {
        val r = repo()
        val url = server.url("/x")
        fun name(proposed: String) = r.add(url + "/" + System.nanoTime(), fileName = proposed)!!.fileName
        assertEquals("rss_feeds.json", name("../rss_feeds.json"))
        assertEquals("x.bin", name("..\\..\\x.bin"))
        assertEquals("planted.bin", name("/data/data/space.deepflux.android/files/planted.bin"))
        assertEquals("file.bin", name("sub/dir/file.bin"))
        assertEquals("win.bin", name("C:\\Users\\someone\\win.bin"))
        assertEquals("a_b_c.bin", name("a\nb\u0000c.bin"))
        // Bidi overrides (they can make a name read backwards on screen) become a visible "_".
        assertEquals("_ok.txt", name("\u202Eok.txt"))
        assertEquals("plain.txt", name("plain.txt"))
        assertEquals(200, name("n".repeat(600) + ".bin").length)
        // "." / ".." / blanks fall back to a name from the URL.
        for (junk in listOf(".", "..", "...", "  ", "/")) assertTrue("'$junk'", name(junk).isNotBlank())
        assertTrue("nothing was written yet", saveDir.listFiles().orEmpty().isEmpty())
    }

    @Test
    fun aServerChosenNameCannotEscapeEither() {
        server.route("/file") {
            TinyHttpServer.Response(
                body = body,
                headers = mapOf(
                    "Content-Type" to "application/octet-stream",
                    // RFC 5987 with an encoded traversal — decoded, then reduced to a plain name.
                    "Content-Disposition" to "attachment; filename*=UTF-8''..%2F..%2Fplanted.bin",
                ),
            )
        }
        val r = repo()
        // No name offered and the URL has no extension: the server's name is used.
        val item = r.add(server.url("/file"))!!
        await("the download") { r.items.value.first { it.id == item.id }.state == HttpState.DONE }
        val done = r.items.value.first { it.id == item.id }
        assertEquals("planted.bin", done.fileName)
        assertArrayEquals(body, File(saveDir, "planted.bin").readBytes())
        assertFalse("nothing above the save dir", File(saveDir.parentFile, "planted.bin").exists())
        assertFalse(File(context.filesDir, "planted.bin").exists())
    }

    @Test
    fun namesSavedBeforeTheFixAreRepairedOnLoad() {
        // A pre-fix state file carrying a traversal name.
        File(context.filesDir, "http_downloads.json").writeText(
            """[{"id":"abc","url":"http://x/old","fileName":"../escaped.bin","state":"DONE","totalBytes":1,"doneBytes":1}]""",
        )
        val r = repo()
        await("the saved list") { r.items.value.isNotEmpty() }
        assertEquals("escaped.bin", r.items.value.single().fileName)
    }

    // ---------------------------------------------------------------- redirects

    @Test
    fun anAgentAddedDownloadRefusesARedirectIntoTheUsersNetwork() {
        server.route("/start") {
            TinyHttpServer.Response(status = 302, headers = mapOf("Location" to server.url("/lan-secret")))
        }
        var lanHits = 0
        server.route("/lan-secret") { lanHits++; TinyHttpServer.Response(body = body) }
        // "Public" = exactly the start URL; everything else counts as the user's network.
        val publicStart = server.url("/start")
        val r = repo { u -> if (u == publicStart) null else "Local and private network URLs are not allowed" }

        val item = r.add(publicStart, publicOnly = true)!!
        await("the refusal") { r.items.value.first { it.id == item.id }.state == HttpState.ERROR }
        val done = r.items.value.first { it.id == item.id }
        assertTrue(done.error, done.error.startsWith("Local and private network URLs are not allowed"))
        assertEquals("the LAN address was never requested", 0, lanHits)
        assertFalse("nothing saved", saveDir.listFiles().orEmpty().any { it.name.startsWith("innocent") })
    }

    @Test
    fun aDownloadTheUserAskedForMayFollowTheSameRedirect() {
        server.route("/start") {
            TinyHttpServer.Response(status = 302, headers = mapOf("Location" to server.url("/lan-secret")))
        }
        var lanHits = 0
        server.route("/lan-secret") { lanHits++; TinyHttpServer.Response(body = body) }
        val publicStart = server.url("/start")
        val r = repo { u -> if (u == publicStart) null else "Local and private network URLs are not allowed" }

        val item = r.add(publicStart)!! // not agent-added: the user's own choice
        await("the download") { r.items.value.first { it.id == item.id }.state == HttpState.DONE }
        assertEquals(1, lanHits)
    }

    // ---------------------------------------------------------------- gate + restart

    @Test
    fun aGatePausedDownloadResumesAfterTheProcessRestarts() {
        server.route("/slow") { TinyHttpServer.Response(body = ByteArray(1024 * 1024), chunkSize = 16 * 1024, chunkDelayMs = 50) }
        val first = repo()
        gateOpen = false
        val item = first.add(server.url("/slow"))!!
        Thread.sleep(300)
        assertEquals("nothing starts behind a closed gate", HttpState.QUEUED, first.items.value.first { it.id == item.id }.state)

        // The process dies and comes back with the gate still closed.
        val second = repo()
        await("the saved list") { second.items.value.isNotEmpty() }
        Thread.sleep(300)
        assertEquals(HttpState.QUEUED, second.items.value.first { it.id == item.id }.state)

        // Wi-Fi is back: the queue drains without anyone pressing Resume.
        gateOpen = true
        second.onGateReopened()
        await("the download") { second.items.value.first { it.id == item.id }.state == HttpState.DONE }
    }

    @Test
    fun aGatePauseMidTransferSurvivesARestart() {
        server.route("/slow2") { TinyHttpServer.Response(body = ByteArray(1024 * 1024), chunkSize = 16 * 1024, chunkDelayMs = 50) }
        val first = repo()
        val item = first.add(server.url("/slow2"))!!
        await("bytes flowing") { first.items.value.first { it.id == item.id }.doneBytes > 0 }
        gateOpen = false
        await("the gate to pause it") { first.items.value.first { it.id == item.id }.state == HttpState.PAUSED }
        assertTrue(first.items.value.first { it.id == item.id }.gatePaused)
        assertTrue(first.hasGatePaused())
        Thread.sleep(300) // let the pause reach the state file

        val second = repo() // restart, gate still closed
        await("the saved list") { second.items.value.isNotEmpty() }
        Thread.sleep(300)
        val restored = second.items.value.first { it.id == item.id }
        assertEquals("queued, not stranded in Paused", HttpState.QUEUED, restored.state)
        // Still behind the closed gate, so it is flagged as waiting on it.
        assertTrue(second.hasGatePaused())

        gateOpen = true
        second.onGateReopened()
        await("the resumed download") { second.items.value.first { it.id == item.id }.state == HttpState.DONE }
    }

    /**
     * Second review (2026-11-24): "active" — what starts the foreground
     * service — was only re-evaluated when the list was SAVED, and every
     * caller saves BEFORE it schedules. The next look was a worker's 5 s
     * tick: a user who left the app within seconds of starting a download
     * got no service (Android 12+ refuses the late start from the background).
     */
    @Test
    fun theFirstRunningDownloadAsksForTheForegroundServiceAtOnce() {
        // The server holds its answer: no bytes, so no worker tick for seconds.
        server.route("/hold") { TinyHttpServer.Response(body = body, delayMs = 4000) }
        val askedAt = java.util.concurrent.atomic.AtomicLong(0)
        val r = repo()
        r.onActiveChanged = { active -> if (active) askedAt.set(System.nanoTime()) }

        val t0 = System.nanoTime()
        r.add(server.url("/hold"))
        await("the service request") { askedAt.get() != 0L }
        val elapsedMs = (askedAt.get() - t0) / 1_000_000
        assertTrue("asked ${elapsedMs}ms after the add — not after the first bytes", elapsedMs < 1_500)
        assertEquals("nothing has arrived yet", 0L, r.items.value.single().doneBytes)
    }

    @Test
    fun theGateReopenNudgeIsFreeWhenNothingWaits() {
        val r = repo()
        val t0 = System.nanoTime()
        repeat(1000) { r.onGateReopened() }
        assertTrue("1000 idle nudges took ${(System.nanoTime() - t0) / 1_000_000}ms", (System.nanoTime() - t0) / 1_000_000 < 1_500)
        assertFalse(r.hasGatePaused())
    }

    @Test
    fun pauseAllStateSurvivesARestartAndResumeAllRestoresOnlyIt() {
        server.route("/s") { TinyHttpServer.Response(body = body, chunkSize = 16 * 1024, chunkDelayMs = 20) }
        val a = server.url("/s?a")
        val b = server.url("/s?b")
        server.route("/s") { TinyHttpServer.Response(body = body, chunkSize = 16 * 1024, chunkDelayMs = 20) }
        val first = repo()
        val ia = first.add(a)!!
        val ib = first.add(b)!!
        await("both running") {
            first.items.value.first { it.id == ia.id }.state == HttpState.RUNNING &&
                first.items.value.first { it.id == ib.id }.state == HttpState.RUNNING
        }
        first.pauseAll()
        await("both paused") { first.items.value.all { it.state == HttpState.PAUSED } }
        Thread.sleep(400) // the pause reaches the state file

        val second = repo()
        await("the saved list") { second.items.value.size == 2 }
        assertTrue("Resume all is still offered after a restart", second.anyPausedByPauseAll())
        second.resumeAll()
        assertFalse(second.anyPausedByPauseAll())
        await("both resumed") { second.items.value.all { it.state == HttpState.DONE } }
    }

    // ---------------------------------------------------------------- second review round

    @Test
    fun pauseAllAlsoHoldsWhatTheGateHadPaused() {
        server.route("/slow3") { TinyHttpServer.Response(body = ByteArray(1024 * 1024), chunkSize = 16 * 1024, chunkDelayMs = 50) }
        val r = repo()
        val item = r.add(server.url("/slow3"))!!
        await("bytes flowing") { r.items.value.first { it.id == item.id }.doneBytes > 0 }
        gateOpen = false
        await("the gate to pause it") { r.items.value.first { it.id == item.id }.state == HttpState.PAUSED }
        assertTrue(r.items.value.first { it.id == item.id }.gatePaused)

        r.pauseAll()
        assertTrue("held by Pause all now", r.items.value.first { it.id == item.id }.pauseAll)

        gateOpen = true
        r.onGateReopened() // Wi-Fi is back: the user's Pause all must still hold
        Thread.sleep(300)
        assertEquals("still paused", HttpState.PAUSED, r.items.value.first { it.id == item.id }.state)

        r.resumeAll()
        await("resumed on request") { r.items.value.first { it.id == item.id }.state == HttpState.DONE }
    }

    @Test
    fun unsafeNamesAreRepairedForEverySavedItemWhateverItsState() {
        // A pre-fix state file: one item that was QUEUED, one that the gate paused, when the process died.
        File(context.filesDir, "http_downloads.json").writeText(
            """[{"id":"q","url":"http://x/old","fileName":"../escaped.bin","state":"QUEUED","totalBytes":1,"doneBytes":0},""" +
                """{"id":"g","url":"http://x/old2","fileName":"..\\gone.bin","state":"PAUSED","gp":true,"totalBytes":1,"doneBytes":0}]""",
        )
        val r = repo()
        await("the saved list") { r.items.value.size == 2 }
        assertEquals(mapOf("q" to "escaped.bin", "g" to "gone.bin"), r.items.value.associate { it.id to it.fileName })
    }
}
