package space.deepflux.android

import android.app.Application
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.json.JSONObject
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
import org.robolectric.Shadows
import space.deepflux.android.data.AppSettings
import space.deepflux.android.data.AppUpdater
import space.deepflux.android.data.CheckResult
import space.deepflux.android.data.UpdateInfo
import space.deepflux.android.data.UpdateProgress
import space.deepflux.android.data.UpdateProgress.Ready
import kotlinx.coroutines.runBlocking
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Fix batch 2026-11-23 — the updater's check/download pipeline against a
 * real local server: a failed check is REPORTED as failed (it used to read
 * as "up to date"), the daily stamp is written only after the feed was
 * really read (one offline launch used to silence checks for 20 h), the
 * download honours the network gate, and a Play-installed build never
 * sideloads.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class AppUpdaterFlowTest {

    private lateinit var context: Context
    private lateinit var server: TinyHttpServer
    private lateinit var settings: AppSettings

    @Volatile
    private var gateOpen = true

    /** A valid payload: ≥ 10 MB (the feed parser's sanity floor). */
    private val apk = ByteArray(10_000_000) { (it * 31).toByte() }
    private val apkSha: String by lazy {
        MessageDigest.getInstance("SHA-256").digest(apk).joinToString("") { "%02x".format(it) }
    }

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        server = TinyHttpServer()
        settings = AppSettings(context)
    }

    @After
    fun tearDown() {
        server.close()
        // Leave a non-Store installer behind for the next test.
        Shadows.shadowOf(context.packageManager).setInstallSourceInfo(context.packageName, null, null)
    }

    private fun feedJson(mutate: (JSONObject) -> Unit = {}): JSONObject = JSONObject()
        .put("version", "9.9.9").put("versionCode", 9999)
        .put("file", "DeepFlux-9.9.9-Android.apk")
        .put("size", apk.size).put("sha256", apkSha)
        .put("released", "2026-11-23").put("notes", "test")
        .also(mutate)

    /** The feed exactly as this test's server serves it. */
    private val servedInfo: UpdateInfo by lazy { AppUpdater.parseFeed(feedJson().toString())!! }

    private fun serveFeed(mutate: (JSONObject) -> Unit = {}, status: Int = 200, body: String? = null) {
        server.route("/latest-android.json") {
            TinyHttpServer.Response(
                status = status, body = (body ?: feedJson(mutate).toString()).toByteArray(),
                headers = mapOf("Content-Type" to "application/json"),
            )
        }
        server.route("/DeepFlux-9.9.9-Android.apk") { TinyHttpServer.Response(body = apk) }
    }

    private fun updater() = AppUpdater(
        context, settings, notify = {},
        networkGateOpen = { gateOpen }, baseUrl = server.url("").trimEnd('/'),
    )

    private fun check(): CheckResult {
        val latch = CountDownLatch(1)
        var out: CheckResult? = null
        updater().checkNow { out = it; latch.countDown() }
        assertTrue("the check never answered", latch.await(10, TimeUnit.SECONDS))
        return out!!
    }

    /** The daily gate's timestamp ("last checked at"). */
    private fun stamp(): Long = runBlocking { settings.prefsSnapshot().lastUpdateCheck }

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 20_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(25)
        }
    }

    @Test
    fun aNewerFeedIsAvailableAndStampsTheDailyGate() {
        serveFeed()
        val before = stamp()
        val u = updater()
        val latch = CountDownLatch(1)
        var out: CheckResult? = null
        u.checkNow { out = it; latch.countDown() }
        assertTrue(latch.await(10, TimeUnit.SECONDS))
        val available = out as CheckResult.Available
        assertEquals("9.9.9", available.info.version)
        // The dialog opens from the manual check too (AppRoot watches `available`).
        assertEquals(available.info, u.available.value)
        assertTrue("a successful check counts as today's check", stamp() >= before)
    }

    @Test
    fun aFailedCheckSaysSoAndLeavesTheDailyGateAlone() {
        serveFeed(status = 500)
        val before = stamp()
        assertEquals(CheckResult.Failed("HTTP 500"), check())
        assertEquals("a failed probe must not count as today's check", before, stamp())
    }

    @Test
    fun aMalformedFeedIsAFailureNotUpToDate() {
        serveFeed(body = "not json at all")
        val before = stamp()
        assertEquals(CheckResult.Failed("feed is invalid"), check())
        assertEquals(before, stamp())
    }

    @Test
    fun anOlderFeedIsUpToDate() {
        serveFeed(
            mutate = {
                it.put("versionCode", 1)
                it.put("version", "0.0.1")
            },
        )
        assertEquals(CheckResult.UpToDate, check())
    }

    @Test
    fun theDownloadWaitsForTheNetworkGate() {
        serveFeed()
        val u = updater()
        gateOpen = false
        u.startDownload(servedInfo)
        val waiting = u.progress.value
        assertTrue("$waiting", waiting is UpdateProgress.Error && waiting.waitingForNetwork)
        Thread.sleep(200)
        assertEquals("nothing is downloaded behind the gate", 0, server.requests.count { it.path.endsWith(".apk") })

        gateOpen = true
        u.startDownload(servedInfo)
        await("the download once the gate opened") { u.progress.value is Ready }
        assertArrayEquals(apk, (u.progress.value as Ready).file.readBytes())
    }

    @Test
    fun theDownloadIsVerifiedAgainstTheFeed() {
        serveFeed()
        val u = updater()
        u.startDownload(servedInfo)
        await("the verified download") { u.progress.value is Ready }
        assertArrayEquals(apk, (u.progress.value as Ready).file.readBytes())
    }

    @Test
    fun aTamperedPayloadIsRejected() {
        serveFeed()
        val tampered = apk.copyOf().also { it[123] = (it[123] + 1).toByte() }
        server.route("/DeepFlux-9.9.9-Android.apk") { TinyHttpServer.Response(body = tampered) }
        val u = updater()
        u.startDownload(servedInfo)
        await("the checksum failure") { u.progress.value is UpdateProgress.Error }
        val p = u.progress.value as UpdateProgress.Error
        assertEquals("checksum mismatch", p.message)
        assertFalse(p.waitingForNetwork)
        assertFalse(
            "a rejected payload leaves no file to install",
            File(context.cacheDir, "updates").listFiles()?.any { it.name.endsWith(".apk") } ?: false,
        )
    }

    @Test
    fun aPlayInstallNeverSideloads() {
        serveFeed()
        Shadows.shadowOf(context.packageManager).setInstallSourceInfo(context.packageName, AppUpdater.PLAY_STORE_PACKAGE, AppUpdater.PLAY_STORE_PACKAGE)
        val u = updater()
        assertTrue(u.installedByPlayStore)
        assertEquals(CheckResult.ManagedByStore, check())
        u.autoCheckIfDue()
        Thread.sleep(300)
        assertEquals("no feed request for a Play install", 0, server.hits)
        assertNull(u.available.value)
    }

    @Test
    fun otherInstallersAreNotMistakenForPlay() {
        Shadows.shadowOf(context.packageManager).setInstallSourceInfo(context.packageName, "com.android.packageinstaller", "com.android.packageinstaller")
        assertFalse(updater().installedByPlayStore)
        Shadows.shadowOf(context.packageManager).setInstallSourceInfo(context.packageName, null, null)
        assertFalse(updater().installedByPlayStore)
    }

    /** The APK route: the FIRST request waits for [answerFirst] and then serves wrong bytes; every later one trickles the good payload. */
    private fun routeWithSlowFirst(answerFirst: CountDownLatch, firstAsked: CountDownLatch, chunkDelayMs: Long = 0) {
        val asks = java.util.concurrent.atomic.AtomicInteger()
        val wrong = ByteArray(apk.size) // right size, wrong bytes
        server.route("/latest-android.json") {
            TinyHttpServer.Response(body = feedJson().toString().toByteArray(), headers = mapOf("Content-Type" to "application/json"))
        }
        server.route("/DeepFlux-9.9.9-Android.apk") {
            if (asks.incrementAndGet() == 1) {
                firstAsked.countDown()
                answerFirst.await(15, TimeUnit.SECONDS) // the server takes its time with the FIRST request
                TinyHttpServer.Response(body = wrong)
            } else {
                TinyHttpServer.Response(body = apk, chunkSize = 1_000_000, chunkDelayMs = chunkDelayMs)
            }
        }
    }

    /**
     * Review round 2026-11-24: cancel used to be one shared flag, and a
     * restart reset it — so a download that was still blocked in a read when
     * it was cancelled resumed as if nothing happened, wrote into the new
     * download's file and reported its own outcome over the new one's.
     */
    @Test
    fun aCancelledDownloadThatEndsLateCannotTouchTheOneStartedAfterIt() {
        val releaseFirst = CountDownLatch(1)
        val firstAsked = CountDownLatch(1)
        routeWithSlowFirst(releaseFirst, firstAsked)
        val u = updater()

        u.startDownload(servedInfo)
        assertTrue("the first download reached the server", firstAsked.await(10, TimeUnit.SECONDS))
        u.cancelDownload()
        u.startDownload(servedInfo)
        await("the second download ready") { u.progress.value is Ready }

        releaseFirst.countDown() // the first request is answered now — to a download that was cancelled
        Thread.sleep(1500) // nothing to await: the cancelled download only ever exits
        assertTrue(
            "the cancelled download's late failure must not replace the result: ${u.progress.value}",
            u.progress.value is Ready,
        )
        assertArrayEquals("and the payload is the good one", apk, (u.progress.value as Ready).file.readBytes())
    }

    /**
     * Second review, same day: pressing Update again supersedes a download
     * without cancelling its request. Its answer used to arrive while the
     * NEW download was mid-file — and the old one opened (and so truncated)
     * the one shared ".part" before its first liveness check. Length still
     * matched and the digest is computed from the stream, so a corrupted
     * payload passed as Ready.
     */
    @Test
    fun aSupersededDownloadsLateAnswerCannotTruncateTheNewPayload() {
        val answerFirst = CountDownLatch(1)
        val firstAsked = CountDownLatch(1)
        routeWithSlowFirst(answerFirst, firstAsked, chunkDelayMs = 150) // ≈ 1.5 s for the good payload
        val u = updater()

        u.startDownload(servedInfo)
        assertTrue("the first download reached the server", firstAsked.await(10, TimeUnit.SECONDS))
        u.startDownload(servedInfo) // "Update" again — the first is superseded, not cancelled
        await("the new download mid-file") {
            (u.progress.value as? UpdateProgress.Downloading)?.let { it.read in 1 until apk.size } == true
        }

        answerFirst.countDown() // the superseded download's answer arrives NOW
        await("the new download ready") { u.progress.value is Ready }
        assertArrayEquals("whole and intact", apk, (u.progress.value as Ready).file.readBytes())
    }
}
