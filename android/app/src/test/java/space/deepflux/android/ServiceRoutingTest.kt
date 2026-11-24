package space.deepflux.android

import android.app.Application
import android.content.Context
import android.content.Intent
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.service.TorrentService

/**
 * Fix batch 2026-11-23 — notification routing and the "Pause all" /
 * "Resume all" pair shared by the notification and the system time limit.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class ServiceRoutingTest {

    private val h1 = "0000000000000000000000000000000000000011"
    private val h2 = "0000000000000000000000000000000000000012"
    private val h3 = "0000000000000000000000000000000000000013"
    private lateinit var context: RecordingContext
    private lateinit var engine: FakeTorrentEngine
    private lateinit var repo: TorrentRepository

    @Before
    fun setUp() {
        context = RecordingContext(ApplicationProvider.getApplicationContext())
        engine = FakeTorrentEngine()
        repo = TorrentRepository(context, engine)
        repo.start()
    }

    @After
    fun tearDown() {
        repo.shutdownForTests()
    }

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 5_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(25)
        }
    }

    /** Stops the setUp repository and starts a fresh one over the same saved state — a restart. */
    /** Stops the setUp repository and starts a fresh one over the same saved state — a restart. [active] stands in for "the engine reports an active torrent". */
    private fun restart(
        newEngine: FakeTorrentEngine = FakeTorrentEngine(),
        gate: (() -> Boolean?)? = null,
        active: (() -> Boolean?)? = null,
    ): Pair<TorrentRepository, FakeTorrentEngine> {
        repo.shutdownForTests()
        val r = TorrentRepository(context, newEngine, gateOverride = gate, activeOverride = active).also { it.start() }
        repo = r
        engine = newEngine
        return r to newEngine
    }

    /** (paused by hand, paused by "Pause all") per saved entry, or null while there is no saved state yet. */
    private fun savedPauseFlags(): List<Pair<Boolean, Boolean>>? = runCatching {
        val arr = org.json.JSONArray(java.io.File(context.filesDir, "torrents_state.json").readText())
        (0 until arr.length()).map { i -> val o = arr.getJSONObject(i); o.optBoolean("p") to o.optBoolean("pa") }
    }.getOrNull()

    /**
     * The flags regardless of entry order — two adds race on the engine
     * dispatcher, so the saved list's order is not fixed.
     *
     * "Both torrents are recorded" is `savedPauseFlags()?.size == 2`, never
     * the fake engine's own list: the engine is asked BEFORE the repository
     * notes the entry down, and a "Pause all" issued in that gap leaves the
     * second torrent out (a test race that failed 3 runs in 8).
     */
    private fun savedFlagSet(): Set<Pair<Boolean, Boolean>>? = savedPauseFlags()?.toSet()

    @Test
    fun downloadNotificationsOpenTheDownloadsTab() {
        val open = TorrentService.openDownloadsIntent(context)
        assertEquals(MainActivity::class.java.name, open.component?.className)
        assertEquals(TorrentRepository.TAB_DOWNLOADS, open.getStringExtra(TorrentRepository.EXTRA_OPEN_TAB))

        val resume = TorrentService.resumeAllIntent(context)
        assertEquals(MainActivity::class.java.name, resume.component?.className)
        assertEquals(TorrentRepository.TAB_DOWNLOADS, resume.getStringExtra(TorrentRepository.EXTRA_OPEN_TAB))
        assertEquals(TorrentRepository.ACTION_RESUME_ALL, resume.getStringExtra(TorrentRepository.EXTRA_ACTION))
        // Only OUR notification may trigger "Resume all": MainActivity is
        // exported, so the action is bound to this install's secret.
        assertEquals(TorrentService.notificationToken(context), resume.getStringExtra(TorrentRepository.EXTRA_NOTIFICATION_TOKEN))
        assertTrue(TorrentService.notificationToken(context).length >= 32)

        val pause = TorrentService.pauseAllIntent(context)
        assertEquals(TorrentService::class.java.name, pause.component?.className)
        assertEquals(TorrentService.ACTION_PAUSE_ALL, pause.action)
        assertFalse("the pause action carries no token: it targets the non-exported service directly", pause.hasExtra(TorrentRepository.EXTRA_NOTIFICATION_TOKEN))
    }

    @Test
    fun onlyOurOwnNotificationCanAskForResumeAll() {
        // What another app can send: the exported activity, every extra —
        // but never the secret. MainActivity's decision is
        // isResumeAllRequest, so that is what is put to the test.
        val forged = Intent(space.deepflux.android.engine.TorrentRepository.ACTION_RESUME_ALL)
            .setClassName(context.packageName, MainActivity::class.java.name)
            .putExtra(TorrentRepository.EXTRA_OPEN_TAB, TorrentRepository.TAB_DOWNLOADS)
            .putExtra(TorrentRepository.EXTRA_ACTION, TorrentRepository.ACTION_RESUME_ALL)
        assertFalse(TorrentService.isResumeAllRequest(context, forged))
        assertFalse(
            "a guessed token",
            TorrentService.isResumeAllRequest(context, Intent(forged).putExtra(TorrentRepository.EXTRA_NOTIFICATION_TOKEN, "0".repeat(32))),
        )
        assertFalse(
            "the token without the action asks for nothing",
            TorrentService.isResumeAllRequest(
                context,
                Intent(forged).putExtra(TorrentRepository.EXTRA_NOTIFICATION_TOKEN, TorrentService.notificationToken(context)).putExtra(TorrentRepository.EXTRA_ACTION, ""),
            ),
        )
        assertFalse("the pause action's target cannot resume", TorrentService.isResumeAllRequest(context, TorrentService.pauseAllIntent(context)))
        assertTrue("the real thing", TorrentService.isResumeAllRequest(context, TorrentService.resumeAllIntent(context)))
    }

    // ---------------------------------------------------------------- startup + network gate

    @Test
    fun startingUpNeverStartsTheForegroundService() {
        java.io.File(context.filesDir, "torrents_state.json")
            .writeText("""[{"h":"$h3","m":"magnet:?xt=urn:btih:$h3","n":"Saved","s":false,"a":1}]""")
        // The gate is fixed open: Wi-Fi only is the default, and what
        // Robolectric reports as "metered" is not this test's subject. The
        // engine reports nothing active (a paused or finished torrent).
        val (started, e) = restart(gate = { true }, active = { false })

        await("the first refresh") { e.snapshots >= 1 }
        assertEquals("the saved torrent is back", 1, e.magnets.size)
        assertEquals("the gate is applied on the first look", 1, e.sessionResumes)
        assertTrue("no foreground service at startup: ${context.startedServices}", context.startedServices.isEmpty())

        await("a few more polls") { e.snapshots >= 4 }
        assertEquals("an open gate is not 'reopened' on every poll", 1, e.sessionResumes)
        assertTrue("still no service", context.startedServices.isEmpty())
        assertTrue(started.gateOpen.value)

        // A user gesture still brings the service up.
        started.resume(h3)
        await("the gesture's service start") { context.startedServices.isNotEmpty() }
    }

    /**
     * Second review (2026-11-24): once the gate's first look no longer
     * started the service, torrents that a RESTART resumes ran with none.
     * MainActivity.onStart reads the list before the first snapshot exists.
     * The service now follows activity — started when torrents become
     * active, once per edge.
     */
    @Test
    fun torrentsARestartResumesBringTheServiceUpOnce() {
        val active = java.util.concurrent.atomic.AtomicBoolean(false)
        val (r, e) = restart(gate = { true }, active = { active.get() })

        await("the first refresh") { e.snapshots >= 1 }
        assertTrue("nothing active: no service", context.startedServices.isEmpty())

        active.set(true)
        await("the service follows activity") { context.startedServices.isNotEmpty() }
        await("several more polls") { e.snapshots >= 5 }
        assertEquals("on the edge, not on every poll", 1, context.startedServices.size)

        active.set(false)
        await("polls while idle") { e.snapshots >= 7 }
        active.set(true)
        await("and again on the next edge") { context.startedServices.size == 2 }
    }

    @Test
    fun aGateThatOpensLaterTellsTheOtherLaneAndReassertsTheService() {
        val gate = java.util.concurrent.atomic.AtomicBoolean(false)
        val opened = java.util.concurrent.atomic.AtomicInteger()
        val (r, e) = restart(gate = { gate.get() }, active = { false })
        r.onGateOpened = { opened.incrementAndGet() }

        await("the first look") { e.snapshots >= 1 }
        assertEquals("closed at startup: the session starts paused", 1, e.sessionPauses)
        assertFalse(r.gateOpen.value)
        assertEquals("no nudge while the gate is closed", 0, opened.get())
        assertTrue(context.startedServices.isEmpty())

        gate.set(true)
        await("the reopening") { e.sessionResumes == 1 }
        await("the HTTP lane learns of it") { opened.get() >= 1 }
        await("the service is re-asserted") { context.startedServices.isNotEmpty() }
        assertTrue(r.gateOpen.value)

        // The lane is nudged on EVERY poll while the gate is open — its
        // workers can pause an item during a blip the poller never sees —
        // the session resumes and the service is asserted only once.
        val nudgesAtReopen = opened.get()
        await("a few more polls") { e.snapshots >= 5 }
        assertTrue("nudged per poll while open ($nudgesAtReopen → ${opened.get()})", opened.get() > nudgesAtReopen)
        assertEquals(1, e.sessionResumes)
        assertEquals(1, context.startedServices.size)
    }

    // ---------------------------------------------------------------- the pause survives a restart

    @Test
    fun torrentsAddedOneAfterTheOtherAreAllSaved() {
        // Each round adds a burst. Two adds saving at once used to share one
        // tmp file, and the slower writer could put its older, shorter list
        // on disk — a race, so it takes many rounds to show (without the
        // fix: failed in 6 of 6 runs).
        repeat(12) { round ->
            val base = (0x10 + round) * 256
            val hashes = (1..6).map { i -> "%040x".format(0L).take(36) + "%04x".format(base + i) }
            hashes.forEach { h -> repo.addMagnet("magnet:?xt=urn:btih:$h") }
            await("round $round: all saved so far") { savedPauseFlags()?.size == 6 * (round + 1) }
        }
    }

    @Test
    fun aHandPauseAndAPauseAllPauseSurviveARestart() {
        repo.addMagnet("magnet:?xt=urn:btih:$h1")
        repo.addMagnet("magnet:?xt=urn:btih:$h2")
        await("both torrents recorded") { savedPauseFlags()?.size == 2 }
        repo.pause(h1)
        repo.pauseAll()
        assertTrue("recorded at once, before any engine call", repo.anyPausedByPauseAll())
        assertTrue(repo.pausedByPauseAll.value)
        await("everything paused") { engine.paused.containsAll(listOf(h1, h2)) }
        await("the pause saved") { savedFlagSet() == setOf(true to false, true to true) }

        val (second, e) = restart()
        await("both torrents restored and still paused") { e.magnets.size == 2 && e.paused.containsAll(listOf(h1, h2)) }
        assertTrue("'Resume all' is still offered after a restart", second.anyPausedByPauseAll())
        assertTrue(second.pausedByPauseAll.value)

        second.resumeAll()
        assertFalse("answered at once — nothing waits any more", second.anyPausedByPauseAll())
        assertFalse(second.pausedByPauseAll.value)
        await("exactly what 'Pause all' paused is resumed") { h2 in e.resumed }
        assertFalse("the hand-paused torrent stays paused", h1 in e.resumed)
        await("the resumed state saved") { savedFlagSet() == setOf(true to false, false to false) }
    }

    @Test
    fun aResumedTorrentLeavesResumeAllsList() {
        repo.addMagnet("magnet:?xt=urn:btih:$h1")
        repo.addMagnet("magnet:?xt=urn:btih:$h2")
        await("both torrents recorded") { savedPauseFlags()?.size == 2 }
        repo.pauseAll()
        assertTrue(repo.pausedByPauseAll.value)
        repo.resume(h1) // by hand
        await("only one still waits") { savedFlagSet() == setOf(false to false, true to true) }
        assertTrue(repo.pausedByPauseAll.value)
        repo.resume(h2)
        assertFalse("resumed by hand one by one: 'Resume all' has nothing left", repo.pausedByPauseAll.value)
        assertFalse(repo.anyPausedByPauseAll())
    }

    @Test
    fun requestedTabIsHeldUntilTheUiConsumesIt() {
        assertNull(repo.requestedTab.value)
        repo.requestTab(TorrentRepository.TAB_DOWNLOADS)
        assertEquals(TorrentRepository.TAB_DOWNLOADS, repo.requestedTab.value)
        repo.consumeRequestedTab()
        assertNull(repo.requestedTab.value)
    }

    @Test
    fun resumeAllRestoresExactlyWhatPauseAllPaused() {
        repo.addMagnet("magnet:?xt=urn:btih:$h1")
        repo.addMagnet("magnet:?xt=urn:btih:$h2")
        await("both torrents recorded") { savedPauseFlags()?.size == 2 }

        repo.pauseAll()
        await("both paused") { engine.paused.containsAll(listOf(h1, h2)) }
        assertTrue(repo.anyPausedByPauseAll())

        // One is resumed by hand: it is no longer "Resume all"'s to restore.
        repo.resume(h1)
        await("the hand resume") { h1 in engine.resumed }
        assertTrue(repo.anyPausedByPauseAll())

        repo.resumeAll()
        await("the other resumed") { h2 in engine.resumed }
        assertFalse(repo.anyPausedByPauseAll())
        assertEquals("resumeAll must not touch the hand-resumed torrent again", 1, engine.resumed.count { it == h1 })
    }

    @Test
    fun pauseAllLeavesAHandPausedTorrentAlone() {
        repo.addMagnet("magnet:?xt=urn:btih:$h1")
        repo.addMagnet("magnet:?xt=urn:btih:$h2")
        await("both torrents recorded") { savedPauseFlags()?.size == 2 }

        repo.pause(h1)
        await("the hand pause") { h1 in engine.paused }

        repo.pauseAll()
        await("the other paused") { h2 in engine.paused }
        assertTrue(repo.anyPausedByPauseAll())

        repo.resumeAll()
        await("the other resumed") { h2 in engine.resumed }
        assertFalse("the hand-paused torrent stays paused", h1 in engine.resumed)
    }
}
