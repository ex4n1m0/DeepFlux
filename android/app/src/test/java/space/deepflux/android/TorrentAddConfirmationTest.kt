package space.deepflux.android

import android.app.Application
import android.content.ComponentName
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.net.Uri
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
import org.libtorrent4j.AlertListener
import org.libtorrent4j.TorrentStatus
import space.deepflux.android.engine.MagnetParser
import space.deepflux.android.engine.TorrentEngineApi
import space.deepflux.android.engine.TorrentFileAdd
import space.deepflux.android.engine.TorrentMeta
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.service.TorrentService
import java.io.File

/** The engine seam's test double: records what the repository asks for, owns no native session. */
class FakeTorrentEngine : TorrentEngineApi {
    override var listener: AlertListener? = null
    override val isRunning = false
    val magnets = mutableListOf<String>()
    val files = mutableListOf<String>()
    val removed = mutableListOf<String>()
    val paused = mutableListOf<String>()
    val resumed = mutableListOf<String>()

    /** How many times `start()` should still fail before it succeeds. */
    @Volatile var startFailures = 0

    /** Every `start()` call, successful or not. */
    @Volatile var starts = 0
    /** Engine calls come from the repository's worker threads; tests poll these from their own. */
    @Volatile var sessionPauses = 0
    @Volatile var sessionResumes = 0

    /** One per poll — "the first refresh has completed" is `snapshots >= 1`. */
    @Volatile var snapshots = 0

    /** `start()` takes this long — an add that arrives meanwhile must wait, not fail. */
    @Volatile var startDelayMs = 0L

    /** Names the fake reports as a torrent's own name once its metadata is known. */
    val names = java.util.concurrent.ConcurrentHashMap<String, String>()

    /** Every .torrent the fake wrote out as a torrent's metadata: hash → path. */
    val savedTorrents = java.util.concurrent.ConcurrentHashMap<String, String>()

    /** How many times the app asked the fake to write a torrent's metadata — successful or not. */
    @Volatile var saveCalls = 0

    /** True while `saveTorrentFile` should fail, as a full disk would make it. */
    @Volatile var failSaving = false

    /** While set, every attempt to add a .torrent file fails with this reason — as a disk error would make it. */
    @Volatile var failFileAdds: String? = null

    /** While set, the engine reports every .torrent file as no torrent — as a moment's read error would. */
    @Volatile var refuseKeptFiles = false

    /** For each kept torrent's v1 hash (what `addTorrentFile` reports): the v2 prefix it also answers to. */
    val v2Prefixes = java.util.concurrent.ConcurrentHashMap<String, String>()

    /**
     * When [holdName] is set, `torrentName` — the last engine step of a keep —
     * reports that it was asked and then waits to be let go: a test taps
     * Remove in exactly that moment.
     */
    class Hold {
        val asked = java.util.concurrent.CountDownLatch(1)
        private val go = java.util.concurrent.CountDownLatch(1)
        fun release() = go.countDown()
        fun await() {
            go.await(10, java.util.concurrent.TimeUnit.SECONDS)
        }
    }

    @Volatile var holdName: Hold? = null

    /** Hashes the fake reports as "this torrent carries a symbolic link". */
    val symlinkHashes: MutableSet<String> = java.util.concurrent.ConcurrentHashMap.newKeySet()

    /** True while `start()` is running — like the real engine, the fake then refuses to add anything. */
    @Volatile var starting = false

    /** True once `start()` has succeeded — until then the real engine has no session, and every add fails. */
    @Volatile var startedOk = false

    override fun start() {
        starts++
        starting = true
        try {
            if (startDelayMs > 0) Thread.sleep(startDelayMs)
            if (startFailures > 0) {
                startFailures--
                throw java.io.IOException("engine start failed (fake)")
            }
            startedOk = true
        } finally {
            starting = false
        }
    }
    override fun stop() {}
    override fun addMagnet(magnet: String, saveDir: File): String? {
        if (!startedOk) return null
        return MagnetParser.infoHashHex(magnet)?.also { magnets.add(magnet) }
    }
    override fun addTorrentFile(path: String, saveDir: File): TorrentFileAdd {
        if (!startedOk) return TorrentFileAdd.Failed("engine not running")
        failFileAdds?.let { return TorrentFileAdd.Failed(it) }
        // The real engine derives the hash from the file; mirror TorrentMeta here.
        val info = File(path).takeIf { it.isFile }?.let { TorrentMeta.readTorrent(it.readBytes()) }
            ?: return TorrentFileAdd.NotATorrent
        if (refuseKeptFiles) return TorrentFileAdd.NotATorrent
        if (info.infoHash in symlinkHashes) return TorrentFileAdd.HasSymbolicLinks
        files.add(path)
        return TorrentFileAdd.Added(info.infoHash, info.name, v2Prefixes[info.infoHash])
    }
    override fun hasSymlinkEntries(infoHashHex: String): Boolean = infoHashHex in symlinkHashes
    override fun torrentName(infoHashHex: String): String? {
        holdName?.let {
            it.asked.countDown()
            it.await()
        }
        return names[infoHashHex]
    }
    override fun saveTorrentFile(infoHashHex: String, to: File): Boolean {
        saveCalls++
        if (failSaving) return false
        to.parentFile?.mkdirs()
        val tmp = File(to.path + ".tmp")
        TestTorrents.write(tmp, names[infoHashHex] ?: KEPT_NAME, "file.bin")
        if (!tmp.renameTo(to)) {
            tmp.delete()
            return false
        }
        savedTorrents[infoHashHex] = to.absolutePath
        return true
    }
    override fun firstVideoFile(infoHashHex: String, saveDir: File): String? = null
    override fun applySequential(infoHashHex: String) = false
    override fun pauseTorrent(infoHashHex: String) { paused.add(infoHashHex) }
    override fun resumeTorrent(infoHashHex: String) { resumed.add(infoHashHex) }
    override fun removeTorrent(infoHashHex: String) { removed.add(infoHashHex) }
    override fun pauseSession() { sessionPauses++ }
    override fun resumeSession() { sessionResumes++ }
    override fun snapshot(infoHashes: List<String>): List<Pair<String, TorrentStatus?>> {
        snapshots++
        return emptyList()
    }
    override fun trackerUrls(infoHashHex: String): List<String> = emptyList()
    override fun libtorrentVersion() = "fake"

    companion object {
        /** The name of the torrent the fake writes as kept metadata unless `names` has one for the hash. */
        const val KEPT_NAME = "Kept torrent"

        /**
         * The info-hash of the torrent the fake keeps under [KEPT_NAME] — so the
         * hash a test's magnet carries is that of the very torrent a restart
         * finds in the kept file, as it is for a real one.
         */
        fun keptHash(name: String = KEPT_NAME): String =
            TorrentMeta.readTorrent(TestTorrents.bytes(name, "file.bin"))!!.infoHash
    }
}

/** A context that records service starts instead of launching the real foreground service. */
class RecordingContext(base: Context) : ContextWrapper(base) {
    // Service starts are asked for from worker threads.
    val startedServices: MutableList<Intent> = java.util.Collections.synchronizedList(mutableListOf())
    override fun startForegroundService(service: Intent?): ComponentName? {
        service?.let { startedServices.add(it) }
        return ComponentName(packageName, "fake.Service")
    }
    override fun startService(service: Intent?): ComponentName? {
        service?.let { startedServices.add(it) }
        return ComponentName(packageName, "fake.Service")
    }
}

/**
 * Fix batch 2026-11-23 — torrent adds that did not come from the user's own
 * hand (a page navigation, a redirect, another app) wait for a confirmation.
 * Before the fix a page could put torrents in the list unasked.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class TorrentAddConfirmationTest {

    private val hash = "0000000000000000000000000000000000000001"
    private val magnet = "magnet:?xt=urn:btih:$hash&dn=Ubuntu+24.04"
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

    @org.junit.After
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

    /** A valid .torrent file staged in cacheDir, as the browser / intent lanes hand them over. */
    private fun stagedTorrent(name: String = "Ubuntu Test ISO"): File {
        fun s(x: String): String {
            val b = x.toByteArray(Charsets.UTF_8)
            return "${b.size}:$x"
        }
        val text = "d" + s("announce") + s("udp://tracker.example:80") + s("info") + "d" +
            s("length") + "i1024e" + s("name") + s(name) + s("piece length") + "i16384e" +
            s("pieces") + "20:" + "A".repeat(20) + "e" + s("creation date") + "i1700000000e" + "e"
        return File(context.cacheDir, "web-test.torrent").apply { writeText(text) }
    }

    @Test
    fun pageRequestWaitsForTheUser() {
        repo.requestAddMagnet(magnet, sourceHost = "tracker.example")
        val pending = repo.pendingAdd.value
        assertNotNull(pending)
        assertEquals(hash, pending!!.infoHash)
        assertEquals("Ubuntu 24.04", pending.name)
        assertEquals("tracker.example", pending.sourceHost)
        Thread.sleep(200)
        assertTrue("nothing reaches the engine before the user confirms", engine.magnets.isEmpty())

        repo.confirmPendingAdd()
        await("the confirmed add") { engine.magnets.isNotEmpty() }
        assertEquals(listOf(magnet), engine.magnets)
        assertNull(repo.pendingAdd.value)
        // Adding starts the foreground service (recorded, not launched) —
        // a few statements after the engine call, on the same worker.
        await("the foreground-service start") { context.startedServices.isNotEmpty() }
        assertTrue(context.startedServices.any { it.component?.className == TorrentService::class.java.name })
    }

    @Test
    fun cancelDiscardsTheRequest() {
        repo.requestAddMagnet(magnet)
        repo.cancelPendingAdd()
        assertNull(repo.pendingAdd.value)
        Thread.sleep(300)
        assertTrue(engine.magnets.isEmpty())
    }

    @Test
    fun invalidMagnetNeverPrompts() {
        repo.requestAddMagnet("hello there")
        assertNull(repo.pendingAdd.value)
    }

    @Test
    fun aNewerRequestReplacesTheOlderOne() {
        val other = "magnet:?xt=urn:btih:0000000000000000000000000000000000000002"
        repo.requestAddMagnet(magnet)
        repo.requestAddMagnet(other, sourceHost = "other.example")
        assertEquals("0000000000000000000000000000000000000002", repo.pendingAdd.value?.infoHash)
    }

    @Test
    fun torrentFileIsValidatedStagedAndConsumedOnConfirm() {
        val file = stagedTorrent()
        repo.requestAddTorrentFile(file, sourceHost = "site.example")
        await("the staged request") { repo.pendingAdd.value != null }
        val pending = repo.pendingAdd.value!!
        assertEquals("Ubuntu Test ISO", pending.name)
        assertEquals(40, pending.infoHash.length)
        assertEquals("site.example", pending.sourceHost)
        assertTrue("the staged file waits on disk", file.isFile)
        assertTrue(engine.files.isEmpty())

        repo.confirmPendingAdd()
        await("the confirmed file add") { engine.files.isNotEmpty() }
        assertEquals(listOf(file.absolutePath), engine.files)
        assertFalse("the staged file is consumed by the import", file.exists())
        // Restart-proof copy of the .torrent, as for any other add.
        assertTrue(File(context.filesDir, "torrents/${pending.infoHash}.torrent").isFile)
    }

    @Test
    fun cancelDeletesTheStagedTorrentFile() {
        val file = stagedTorrent()
        repo.requestAddTorrentFile(file)
        await("the staged request") { repo.pendingAdd.value != null }
        repo.cancelPendingAdd()
        assertFalse(file.exists())
        assertTrue(engine.files.isEmpty())
    }

    @Test
    fun invalidTorrentFileIsDiscardedWithoutPrompting() {
        val file = File(context.cacheDir, "web-bad.torrent").apply { writeText("<html>a login page</html>") }
        repo.requestAddTorrentFile(file)
        // The discard deletes the file; only after that could anything be shown.
        await("the invalid file discarded") { !file.exists() }
        Thread.sleep(100) // a dialog would be set on the same worker, right after the delete
        assertNull(repo.pendingAdd.value)
    }

    @Test
    fun anotherAppsMagnetIntentIsConfirmed() {
        repo.addFromIntent(Intent(Intent.ACTION_VIEW, Uri.parse(magnet)))
        assertNotNull("an external magnet waits for the user", repo.pendingAdd.value)
        Thread.sleep(200)
        assertTrue(engine.magnets.isEmpty())
    }

    @Test
    fun aTorrentAlreadyInTheListIsNotOfferedAgain() {
        repo.requestAddMagnet(magnet)
        repo.confirmPendingAdd()
        // What the repository RECORDS, not what the engine was asked: the entry is
        // noted a moment after the engine has it, and the next line asks the repository.
        val savedList = File(context.filesDir, "torrents_state.json")
        await("the first add is saved") { runCatching { hash in savedList.readText() }.getOrDefault(false) }
        repo.requestAddMagnet(magnet, sourceHost = "page.example")
        assertNull("already listed torrents never prompt", repo.pendingAdd.value)
    }

    @Test
    fun theAgentsOwnAddStaysDirect() {
        // add_torrent (always-YOLO) and the URL bar / links sheet call addMagnet — no prompt.
        repo.addMagnet(magnet)
        await("the direct add") { engine.magnets.isNotEmpty() }
        assertNull(repo.pendingAdd.value)
    }

    @Test
    fun torrentActionsReturnImmediatelyOnceTheListIsLoaded() {
        // The load latch used to open only AFTER the endless poll loop, so
        // every add / pause / resume / remove tap blocked the main thread for
        // the full 3 s timeout.
        repo.addMagnet(magnet)
        await("the add") { engine.magnets.isNotEmpty() }
        val t0 = System.nanoTime()
        repo.pause(hash)
        repo.resume(hash)
        repo.remove(hash)
        val elapsedMs = (System.nanoTime() - t0) / 1_000_000
        assertTrue("pause/resume/remove took ${elapsedMs}ms on the calling thread", elapsedMs < 1_000)
    }
}
