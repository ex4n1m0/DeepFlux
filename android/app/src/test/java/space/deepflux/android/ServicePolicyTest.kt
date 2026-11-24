package space.deepflux.android

import android.Manifest
import android.app.Application
import android.app.NotificationManager
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowInstrumentation
import space.deepflux.android.service.TorrentService
import space.deepflux.android.service.TransferLockPolicy

/**
 * Review round 2026-11-24 — the download service's battery and "Resume all"
 * behaviour:
 *  - the wake + Wi-Fi locks follow actual progress with the network gate
 *    open. They used to live exactly as long as the service, which kept
 *    running — lock held — through a closed gate and through every idle
 *    grace;
 *  - "Pause all" idles every transfer, so the service stops 20 s later and
 *    its "Resume all" button vanished with it. A standing notice takes over.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class ServicePolicyTest {

    // ---------------------------------------------------------------- lock policy

    /** A controllable clock for the lock policy. */
    private class Clock(var t: Long = 1_000_000L) {
        fun advance(ms: Long) {
            t += ms
        }
    }

    private fun policy(clock: Clock, stallMs: Long = TransferLockPolicy.STALL_MS) =
        TransferLockPolicy(stallMs = stallMs, now = { clock.t })

    /** One look at the policy with transfers active and the gate open. */
    private fun TransferLockPolicy.look(movedBytes: Long, gateOpen: Boolean = true, active: Boolean = true, receiving: Boolean = false) =
        tick(gateOpen = gateOpen, transfersActive = active, checking = false, receiving = receiving, movedBytes = movedBytes)

    @Test
    fun heldWhileTransfersMoveWithTheGateOpen() {
        val clock = Clock()
        val p = policy(clock)
        assertTrue("a transfer that just started gets time to connect or fetch metadata", p.look(movedBytes = 0))
        repeat(200) { i ->
            clock.advance(TransferLockPolicy.STALL_MS / 2) // slow: a byte every half a stall window
            assertTrue("byte ${i + 1}", p.look(movedBytes = (i + 1) * 4096L))
        }
    }

    @Test
    fun releasedTheMomentTheGateCloses() {
        val clock = Clock()
        val p = policy(clock)
        assertTrue(p.look(movedBytes = 1_000_000))
        assertFalse("nothing can move while the engine is paused — no lock for a phone waiting for Wi-Fi", p.look(movedBytes = 1_000_000, gateOpen = false))
        repeat(10) {
            clock.advance(5_000)
            assertFalse("stays released while closed", p.look(movedBytes = 2_000_000, gateOpen = false, receiving = true))
        }
        assertTrue("and comes back when the gate opens again", p.look(movedBytes = 2_000_000))
    }

    @Test
    fun releasedAfterAStallAndTakenBackByTheNextByte() {
        val clock = Clock()
        val p = policy(clock, stallMs = 60_000)
        var bytes = 0L
        repeat(10) {
            bytes += 100_000
            clock.advance(5_000)
            assertTrue(p.look(movedBytes = bytes))
        }
        // The swarm goes quiet.
        var waited = 0L
        while (waited < 60_000 - 5_000) {
            clock.advance(5_000)
            waited += 5_000
            assertTrue("a stall under a minute is still working ($waited ms)", p.look(movedBytes = bytes))
        }
        clock.advance(5_000)
        assertFalse("after a full minute without a byte the CPU is given back", p.look(movedBytes = bytes))
        repeat(20) {
            clock.advance(60_000)
            assertFalse("and stays free", p.look(movedBytes = bytes))
        }
        // A peer shows up (an inbound connection still wakes the radio).
        bytes += 100_000
        assertTrue("the first byte takes the lock again", p.look(movedBytes = bytes))
    }

    @Test
    fun aPieceInProgressCountsAsProgress() {
        val clock = Clock()
        val p = policy(clock, stallMs = 60_000)
        assertTrue(p.look(movedBytes = 0))
        // Bytes inside an unfinished torrent piece do not show as "moved" yet;
        // the live rate does — here for far longer than the stall window.
        repeat(24) {
            clock.advance(5_000) // two minutes without one completed piece
            assertTrue(p.look(movedBytes = 0, receiving = true))
        }
        // The rate drops and nothing else arrives: the stall window counts from there.
        clock.advance(55_000)
        assertTrue(p.look(movedBytes = 0, receiving = false))
        clock.advance(10_000)
        assertFalse(p.look(movedBytes = 0, receiving = false))
    }

    @Test
    fun nothingActiveMeansNoLockEvenWithTheGateOpen() {
        val p = policy(Clock())
        assertFalse("no transfer, nothing to keep awake", p.look(movedBytes = 0, active = false))
        assertFalse(p.look(movedBytes = 5_000_000, active = false))
        assertTrue(p.look(movedBytes = 5_000_000, active = true))
        // The last transfer finishes.
        assertFalse(p.look(movedBytes = 6_000_000, active = false))
    }

    @Test
    fun aHashCheckHoldsTheCpuWithoutTheNetwork() {
        val p = policy(Clock())
        assertTrue("checking files is local work — it needs the CPU, not the gate",
            p.tick(gateOpen = false, transfersActive = true, checking = true, receiving = false, movedBytes = 0))
        assertFalse("…and only while it runs", p.tick(gateOpen = false, transfersActive = true, checking = false, receiving = false, movedBytes = 0))
    }

    /** One look at the policy with nothing transferring and a copy of a finished download running. */
    private fun TransferLockPolicy.copying(bytes: Long, gateOpen: Boolean = false) =
        tick(gateOpen = gateOpen, transfersActive = false, checking = false, receiving = false, movedBytes = 0, exporting = true, exportBytes = bytes)

    @Test
    fun aCopyIntoTheUsersFolderHoldsTheCpuWithOrWithoutTheNetwork() {
        val clock = Clock()
        val p = policy(clock)
        assertTrue("a copy of a finished download is local work — the gate is the network's business", p.copying(bytes = 0))
        repeat(50) { i ->
            clock.advance(TransferLockPolicy.STALL_MS / 2)
            assertTrue("chunk ${i + 1}", p.copying(bytes = (i + 1) * 256 * 1024L, gateOpen = i % 2 == 0))
        }
    }

    @Test
    fun aCopyThatStopsMovingBytesGivesTheCpuBack() {
        val clock = Clock()
        val p = policy(clock)
        assertTrue(p.copying(bytes = 1_000_000))
        clock.advance(TransferLockPolicy.STALL_MS - 1)
        assertTrue("still inside the stall window", p.copying(bytes = 1_000_000))
        clock.advance(1)
        assertFalse("a copy whose folder provider hangs must not keep the phone awake", p.copying(bytes = 1_000_000))
        assertTrue("the next byte takes the CPU again", p.copying(bytes = 1_000_256))
    }

    @Test
    fun aCopyThatStartsLongAfterTheLastOneIsNotJudgedOnOldBytes() {
        val clock = Clock()
        val p = policy(clock)
        assertTrue("the first copy, before its first byte", p.copying(bytes = 0))
        clock.advance(5_000)
        assertTrue(p.copying(bytes = 1_000))
        // As the service feeds it while nothing is copied: the counter keeps its last value.
        assertFalse("nothing is being copied", p.tick(gateOpen = false, transfersActive = false, checking = false, receiving = false, movedBytes = 0, exporting = false, exportBytes = 1_000))
        clock.advance(10 * TransferLockPolicy.STALL_MS)
        assertTrue("a new copy, also before its first byte", p.copying(bytes = 1_000))
    }

    // ---------------------------------------------------------------- the standing "paused" notice

    private val context: Context get() = ApplicationProvider.getApplicationContext()

    @Before
    fun grantPostNotifications() {
        // Robolectric starts with runtime permissions ungranted, and the
        // shadow's grantor is package-private.
        val shadow = shadowOf(androidx.test.platform.app.InstrumentationRegistry.getInstrumentation())
        ShadowInstrumentation::class.java
            .getDeclaredMethod("grantPermissions", Array<String>::class.java)
            .apply { isAccessible = true }
            .invoke(shadow, arrayOf(Manifest.permission.POST_NOTIFICATIONS))
    }

    private fun notices(): List<android.app.Notification> =
        shadowOf(context.getSystemService(NotificationManager::class.java)).allNotifications

    @Test
    fun theNoticeIsWantedExactlyWhileTransfersWaitOutsideTheService() {
        assertFalse(TorrentService.pausedNoticeWanted(anyPausedByPauseAll = false, serviceRunning = false))
        assertFalse(TorrentService.pausedNoticeWanted(anyPausedByPauseAll = false, serviceRunning = true))
        // The service's own notification already carries "Resume all".
        assertFalse(TorrentService.pausedNoticeWanted(anyPausedByPauseAll = true, serviceRunning = true))
        assertTrue(TorrentService.pausedNoticeWanted(anyPausedByPauseAll = true, serviceRunning = false))
    }

    @Test
    fun theNoticeCarriesResumeAllAndOpensTheDownloadsTab() {
        TorrentService.syncPausedNotice(context, wanted = true, bySystemLimit = false)
        val n = notices().single()
        assertEquals(context.getString(R.string.notif_paused_title), shadowOf(n).contentTitle.toString())
        assertEquals(context.getString(R.string.notif_paused_text), shadowOf(n).contentText.toString())

        val action = n.actions.single()
        assertEquals(context.getString(R.string.notif_resume_all), action.title.toString())
        val actionIntent = shadowOf(action.actionIntent).savedIntent
        assertTrue("the button is OUR resume request", TorrentService.isResumeAllRequest(context, actionIntent))
        assertEquals(MainActivity::class.java.name, actionIntent.component?.className)

        val tap = shadowOf(n.contentIntent).savedIntent
        assertEquals(
            "tapping the text opens Downloads",
            space.deepflux.android.engine.TorrentRepository.TAB_DOWNLOADS,
            tap.getStringExtra(space.deepflux.android.engine.TorrentRepository.EXTRA_OPEN_TAB),
        )
        assertFalse("…and resumes nothing by itself", TorrentService.isResumeAllRequest(context, tap))
    }

    @Test
    fun androidsTimeLimitGetsItsOwnWordingAndNoSecondNotice() {
        TorrentService.syncPausedNotice(context, wanted = true, bySystemLimit = true)
        val all = notices()
        assertEquals("one notice says it: paused, why, and how to continue", 1, all.size)
        val n = all.single()
        assertEquals(context.getString(R.string.notif_limit_title), shadowOf(n).contentTitle.toString())
        assertEquals(context.getString(R.string.notif_limit_text), shadowOf(n).contentText.toString())
        assertEquals("…with the way to continue", context.getString(R.string.notif_resume_all), n.actions.single().title.toString())
    }

    @Test
    fun theNoticeLeavesWhenNothingWaitsOrTheServiceTakesOver() {
        TorrentService.syncPausedNotice(context, wanted = true, bySystemLimit = false)
        assertNotNull(notices().singleOrNull())
        TorrentService.syncPausedNotice(context, wanted = false, bySystemLimit = false)
        assertNull("resumed, or the service's own notification has the button now", notices().singleOrNull())
    }

    /**
     * Second review (2026-11-24): the notice is a NORMAL notification and
     * outlives the process that posted it — while no service runs, a kill is
     * likely. With a per-process secret its "Resume all" was dead after the
     * first such kill.
     */
    @Test
    fun theResumeAllButtonSurvivesTheProcessThatPostedTheNotice() {
        val tokenExtra = space.deepflux.android.engine.TorrentRepository.EXTRA_NOTIFICATION_TOKEN
        TorrentService.forgetTokenForTests()
        // Process 1: "Pause all", the service stops, the notice is posted.
        val armedByProcess1 = TorrentService.resumeAllIntent(context)
        assertTrue(TorrentService.isResumeAllRequest(context, armedByProcess1))

        // The process dies; the notice stays in the shade.
        TorrentService.forgetTokenForTests()
        assertTrue("process 2 still honours its own earlier notice", TorrentService.isResumeAllRequest(context, armedByProcess1))
        assertEquals(TorrentService.notificationToken(context), armedByProcess1.getStringExtra(tokenExtra))
        assertFalse(
            "another install's token is still refused",
            TorrentService.isResumeAllRequest(
                context,
                android.content.Intent(armedByProcess1).putExtra(tokenExtra, "f".repeat(32)),
            ),
        )
        assertTrue("kept in private storage, not in memory only", java.io.File(context.filesDir, "notification_token").isFile)
    }
}
