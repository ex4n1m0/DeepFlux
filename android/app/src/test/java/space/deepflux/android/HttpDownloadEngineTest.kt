package space.deepflux.android

import android.app.Application
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.dlmgr.HttpDownloadRepository
import space.deepflux.android.dlmgr.HttpState
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList

/**
 * Fix batch 2026-11-23 — the HTTP/HLS download engine against a real local
 * server: a download the network gate paused resumes when the gate reopens
 * (it stayed Paused until the user resumed it by hand), one added behind a
 * closed gate starts once the gate opens, "Pause all"/"Resume all" restore
 * exactly what they paused, and finished/failed downloads are reported.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class HttpDownloadEngineTest {

    @Volatile
    private var gateOpen = true
    private val finished = CopyOnWriteArrayList<String>()
    private val failed = CopyOnWriteArrayList<Pair<String, String>>()
    private lateinit var context: Context
    private lateinit var server: TinyHttpServer
    private lateinit var repo: HttpDownloadRepository

    /** ~1 MiB in 64 chunks of ~50 ms (≈3 s) — the engine publishes byte counts every 500 ms, so a mid-transfer state must last long enough to observe. */
    private val body = ByteArray(1024 * 1024) { (it % 251).toByte() }

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        server = TinyHttpServer()
        repo = HttpDownloadRepository(context, File(context.filesDir, "dl"), gateOk = { gateOpen })
        repo.onFinished = { finished.add(it) }
        repo.onFailed = { name, reason -> failed.add(name to reason) }
    }

    @After
    fun tearDown() {
        server.close()
    }

    private fun serveSlow(path: String): String {
        server.route(path) { TinyHttpServer.Response(body = body, chunkSize = 16 * 1024, chunkDelayMs = 50) }
        return server.url(path)
    }

    private fun state(id: String): HttpState = repo.items.value.first { it.id == id }.state

    private fun doneBytes(id: String): Long = repo.items.value.first { it.id == id }.doneBytes

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 15_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) {
                val items = repo.items.value.joinToString { "${it.fileName}:${it.state}:${it.doneBytes}B:${it.error}" }
                throw AssertionError("timed out waiting for $what (items: $items)")
            }
            Thread.sleep(20)
        }
    }

    @Test
    fun gatePausedDownloadResumesWhenTheGateReopens() {
        val added = repo.add(serveSlow("/a.bin"))!!
        await("the download to start") { state(added.id) == HttpState.RUNNING && doneBytes(added.id) > 0 }

        gateOpen = false
        await("the gate to pause it") { state(added.id) == HttpState.PAUSED }
        Thread.sleep(300)
        assertEquals("still paused on its own", HttpState.PAUSED, state(added.id))

        gateOpen = true
        repo.onGateReopened()
        await("the resumed download to finish") { state(added.id) == HttpState.DONE }
        assertEquals(listOf("a.bin"), finished.toList())
        assertArrayEquals(body, File(context.filesDir, "dl/a.bin").readBytes())
    }

    @Test
    fun downloadAddedBehindAClosedGateStartsOnceItOpens() {
        gateOpen = false
        val added = repo.add(serveSlow("/b.bin"))!!
        Thread.sleep(400)
        assertEquals("nothing starts behind a closed gate", HttpState.QUEUED, state(added.id))

        gateOpen = true
        repo.onGateReopened()
        await("it to start and finish") { state(added.id) == HttpState.DONE }
    }

    @Test
    fun theUsersOwnPauseIsNotUndoneByTheGate() {
        val added = repo.add(serveSlow("/c.bin"))!!
        await("the download to start") { state(added.id) == HttpState.RUNNING && doneBytes(added.id) > 0 }
        repo.pause(added.id)
        await("the user pause") { state(added.id) == HttpState.PAUSED }

        gateOpen = false
        Thread.sleep(100)
        gateOpen = true
        repo.onGateReopened()
        Thread.sleep(300)
        assertEquals("a hand pause stays paused", HttpState.PAUSED, state(added.id))
        assertTrue(finished.isEmpty())
    }

    @Test
    fun pauseAllAndResumeAllRestoreExactlyWhatTheyPaused() {
        val a = repo.add(serveSlow("/d.bin"))!!
        val b = repo.add(serveSlow("/e.bin"))!!
        await("both downloads running") {
            state(a.id) == HttpState.RUNNING && state(b.id) == HttpState.RUNNING &&
                doneBytes(a.id) > 0 && doneBytes(b.id) > 0
        }
        assertFalse(repo.anyPausedByPauseAll())

        repo.pauseAll()
        await("both paused") { state(a.id) == HttpState.PAUSED && state(b.id) == HttpState.PAUSED }
        assertTrue(repo.anyPausedByPauseAll())

        // One resumed by hand finishes; the other is still "Resume all"'s to restore.
        repo.resume(a.id)
        await("a to finish") { state(a.id) == HttpState.DONE }
        assertEquals(HttpState.PAUSED, state(b.id))
        assertTrue(repo.anyPausedByPauseAll())

        repo.resumeAll()
        await("b to finish") { state(b.id) == HttpState.DONE }
        assertFalse(repo.anyPausedByPauseAll())
    }

    @Test
    fun failuresAreReportedOnceWithTheirReason() {
        server.route("/missing.bin") { TinyHttpServer.Response(status = 404) }
        val added = repo.add(server.url("/missing.bin"))!!
        await("the error") { state(added.id) == HttpState.ERROR }
        assertEquals(listOf("missing.bin" to "HTTP 404"), failed.toList())
        assertTrue(finished.isEmpty())
        assertNull("a failed download leaves no final file", File(context.filesDir, "dl/missing.bin").takeIf { it.exists() })
    }
}
