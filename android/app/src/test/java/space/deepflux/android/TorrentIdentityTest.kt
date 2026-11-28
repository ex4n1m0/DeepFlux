package space.deepflux.android

import android.app.Application
import android.app.NotificationManager
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import space.deepflux.android.engine.TorrentItem
import space.deepflux.android.engine.TorrentMeta
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.engine.TorrentState
import space.deepflux.android.ui.Strings
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit

/**
 * 1.4.14 — what the emulator showed about torrent identity (2026-11-24/25):
 *  - a magnet's metadata was never kept, so after a restart the torrent had
 *    to be handed its info again by some peer — a FINISHED torrent came back
 *    as "Fetching info" with nothing to play whenever nobody was;
 *  - the engine's status never carried a name, so torrents added from a
 *    .torrent file were listed under their info-hash and magnets without a
 *    title stayed "Fetching…" as their name for good;
 *  - a .torrent refused for a symbolic link was reported as "not a valid
 *    .torrent file";
 *  - the engine reports "finished" again for every complete torrent at each
 *    start: a "Download finished" on every launch, and a copy run every time.
 *
 * What only the real engine can do (reading the metadata out, writing the
 * file) is verified on the emulator; the file's bytes by [KeptTorrentTest].
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class TorrentIdentityTest {

    private lateinit var context: RecordingContext
    private val repos = mutableListOf<TorrentRepository>()
    private val collectors = mutableListOf<Job>()

    /**
     * The hash a test's magnet carries: that of the very torrent the fake
     * engine writes as kept metadata — as it is for a real one.
     */
    private val hB = FakeTorrentEngine.keptHash()
    private val magnetB get() = "magnet:?xt=urn:btih:$hB"

    @Before
    fun setUp() {
        context = RecordingContext(ApplicationProvider.getApplicationContext())
    }

    @After
    fun tearDown() {
        collectors.forEach { it.cancel() }
        repos.forEach { it.shutdownForTests() }
    }

    private fun repo(engine: FakeTorrentEngine, start: Boolean = true): TorrentRepository =
        TorrentRepository(context, engine).also { repos += it }.also { if (start) it.start() }

    /** Starts collecting the repository's snackbar notices BEFORE anything can emit. */
    private fun noticesOf(r: TorrentRepository): MutableList<String> {
        val seen = CopyOnWriteArrayList<String>()
        collectors += CoroutineScope(Dispatchers.Unconfined).launch { r.notices.collect { seen.add(it) } }
        return seen
    }

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 10_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(25)
        }
    }

    private var lastSavedState = ""

    /**
     * The list as it is saved on disk — the truth a restart wakes up to. A
     * read that collides with the save's move (Windows hands out a sharing
     * error for that instant) answers with the last good read; the next poll
     * reads again.
     */
    private fun savedState(): String {
        val f = File(context.filesDir, "torrents_state.json")
        if (!f.isFile) return lastSavedState
        runCatching { f.readText() }.onSuccess { lastSavedState = it }
        return lastSavedState
    }

    private fun savedName(): String? = savedState().takeIf { it.isNotBlank() }?.let { JSONArray(it).getJSONObject(0).optString("n") }

    private fun keptTorrent(hash: String): File = File(File(context.filesDir, "torrents"), "$hash.torrent")

    /** What a start finds: an entry for [hash], and a kept file holding [kept] (as a bad build or a damaged disk may leave). */
    private fun startWith(hash: String, kept: ByteArray) {
        keptTorrent(hash).apply { parentFile?.mkdirs() }.writeBytes(kept)
        File(context.filesDir, "torrents_state.json").writeText(
            """[{"h":"$hash","m":"magnet:?xt=urn:btih:$hash","n":"Old","s":false,"a":0,"p":false,"pa":false}]""",
        )
    }

    private fun grantPostNotifications() {
        org.robolectric.shadows.ShadowInstrumentation::class.java
            .getDeclaredMethod("grantPermissions", Array<String>::class.java)
            .apply { isAccessible = true }
            .invoke(
                shadowOf(androidx.test.platform.app.InstrumentationRegistry.getInstrumentation()),
                arrayOf(android.Manifest.permission.POST_NOTIFICATIONS),
            )
    }

    /** "Download finished" announcements so far (they pile up over the test run). */
    private fun finishedAnnouncements(): List<String> =
        shadowOf(context.getSystemService(NotificationManager::class.java)).allNotifications
            .filter { it.extras.getCharSequence(android.app.Notification.EXTRA_TITLE)?.toString() == context.getString(R.string.notif_finished_title) }
            .mapNotNull { it.extras.getCharSequence(android.app.Notification.EXTRA_TEXT)?.toString() }

    /**
     * A source file of the app, found from the test's working folder — with its line ends made
     * one. A pin that spans lines must not depend on how the checkout wrote them: a fresh
     * Windows checkout is CRLF and a working copy edited in place may be LF, so this pin passed
     * for the one and failed for the other (2026-11-28, a worktree build of a commit).
     */
    private fun appSource(relative: String): String? {
        val anchor = "src/main/java/space/deepflux/android/engine/TorrentRepository.kt"
        var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
        while (dir != null && !File(dir, anchor).isFile) dir = dir.parentFile
        return dir?.let { File(it, relative).takeIf { f -> f.isFile }?.readText()?.replace("\r\n", "\n") }
    }

    private fun repositorySource(): String? =
        appSource("src/main/java/space/deepflux/android/engine/TorrentRepository.kt")

    // ------------------------------------------------------------------ the metadata is kept

    @Test
    fun aMagnetsMetadataIsKeptSoARestartNeedsNoPeer() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet(magnetB)
        await("the add") { engine.magnets.any { it == magnetB } }
        await("the add is saved") { hB in savedState() }
        runBlocking { r.metadataArrived(hB) }

        assertTrue("the metadata is written as a .torrent", keptTorrent(hB).isFile)

        // A restart — with nobody left to hand the metadata back.
        r.shutdownForTests()
        val secondEngine = FakeTorrentEngine()
        repo(secondEngine).start()
        await("the torrent is back from the kept file") { secondEngine.files.any { it.endsWith("$hB.torrent") } }
        assertTrue("not as a magnet that would have to fetch its info again", secondEngine.magnets.none { magnetB in it })
    }

    @Test
    fun theMetadataIsWrittenOnceNotAtEveryAlert() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet(magnetB)
        await("the add") { engine.magnets.any { it == magnetB } }
        runBlocking { r.metadataArrived(hB) }
        runBlocking { r.metadataArrived(hB) }
        assertEquals(1, engine.saveCalls)
    }

    @Test
    fun aFailedKeepIsTriedAgainOnALaterRefresh() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.keepRetryEveryMs = 0
        r.addMagnet(magnetB)
        await("the add") { engine.magnets.any { it == magnetB } }
        engine.names[hB] = "Ubuntu 26.04 Desktop" // the engine knows the torrent: there is something to keep
        engine.failSaving = true
        runBlocking { r.metadataArrived(hB) }
        assertFalse("nothing half-written", keptTorrent(hB).exists() || File(context.filesDir, "torrents/$hB.torrent.tmp").exists())

        // The disk has room again — on a later refresh, which is how a failed keep is retried.
        engine.failSaving = false
        await("no keep in flight") { !r.isKeeping(hB) }
        runBlocking { r.refreshForTests() }

        await("kept on the retry") { keptTorrent(hB).isFile }
    }

    @Test
    fun andNeverMoreThanThreeTimes() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.keepRetryEveryMs = 0
        r.addMagnet(magnetB)
        await("the add") { engine.magnets.any { it == magnetB } }
        engine.failSaving = true // a disk that stays full
        runBlocking { r.metadataArrived(hB) } // the alert's keep — while the engine does not "know" the torrent yet, so nothing else can have started one
        assertEquals("the alert's keep", 1, engine.saveCalls)
        engine.names[hB] = "Ubuntu 26.04 Desktop" // now there is something to retry keeping

        // Refresh after refresh (the repository's own poller adds some): three retries, then no more.
        repeat(10) {
            await("no keep in flight") { !r.isKeeping(hB) }
            runBlocking { r.refreshForTests() }
        }
        await("settled") { !r.isKeeping(hB) }

        assertEquals("the alert's keep and three retries", 1 + 3, engine.saveCalls)
        assertFalse(keptTorrent(hB).exists())
    }

    @Test
    fun aTorrentWhoseMetadataIsNotHereIsNotKeptAgainAndAgain() {
        // "Fetching info": nothing to keep, however many refreshes go by.
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.keepRetryEveryMs = 0
        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }

        repeat(5) { runBlocking { r.refreshForTests() } }
        await("no keep in flight") { !r.isKeeping(hB) }

        assertEquals("nothing was asked of the engine", 0, engine.saveCalls)
    }

    @Test
    fun aKeptFileTheEngineCannotReadIsDroppedAndKeptAgain() {
        // What a bad build or a damaged disk leaves behind: a kept file that is no torrent.
        keptTorrent(hB).apply { parentFile?.mkdirs() }.writeText("this is no torrent")
        File(context.filesDir, "torrents_state.json").writeText(
            """[{"h":"$hB","m":"magnet:?xt=urn:btih:$hB","n":"Old","s":false,"a":0,"p":false,"pa":false}]""",
        )
        val engine = FakeTorrentEngine()
        val r = repo(engine)

        await("the magnet is what is left") { engine.magnets.any { hB in it } }
        await("the unreadable file is gone") { !keptTorrent(hB).exists() }

        engine.names[hB] = "Ubuntu 26.04 Desktop"
        runBlocking { r.metadataArrived(hB) }

        assertTrue("kept afresh once the info arrives", keptTorrent(hB).isFile)
    }

    @Test
    fun aKeptFileOfSomeOtherTorrentIsNotAdded() {
        // Valid — and not the entry's torrent. Added, it would be a torrent nobody asked for.
        val otherHash = TorrentMeta.readTorrent(TestTorrents.bytes("Other Name", "README"))!!.infoHash
        startWith(hB, TestTorrents.bytes("Other Name", "README"))
        val engine = FakeTorrentEngine()

        repo(engine).start()

        await("the magnet is what is left") { engine.magnets.any { hB in it } }
        assertTrue("the other torrent was not left in the session", otherHash in engine.removed)
        await("the wrong file is gone") { !keptTorrent(hB).exists() }
    }

    @Test
    fun aKeptFileIsThisEntrySTorrentByTheHashTheEngineNamesItBy() {
        // A magnet may carry a torrent's v2 hash under `btih:`, so the entry is keyed by it, while
        // the file's info dictionary hashes to its v1 hash: the engine names the torrent by the
        // v2 one — which is what `infoHash` reports.
        val otherHash = TorrentMeta.readTorrent(TestTorrents.bytes("Other Name", "README"))!!.infoHash
        startWith(hB, TestTorrents.bytes("Other Name", "README"))
        val engine = FakeTorrentEngine()
        engine.v2Prefixes[otherHash] = hB

        repo(engine).start()

        await("back from the kept file, under its own name") { savedName() == "Other Name" }
        assertTrue("no magnet was needed", engine.magnets.isEmpty())
        assertTrue("and the file stays", keptTorrent(hB).isFile)
        assertTrue(engine.removed.isEmpty())
    }

    @Test
    fun aMagnetWithALinkInItsMetadataIsRemovedAndNothingIsKept() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val notices = noticesOf(r)
        r.addMagnet(magnetB)
        await("the add") { engine.magnets.any { it == magnetB } }
        engine.symlinkHashes.add(hB)

        runBlocking { r.metadataArrived(hB) }

        await("gone from the saved list") { hB !in savedState() }
        assertTrue("never written out", !keptTorrent(hB).exists())
        assertEquals(0, engine.saveCalls)
        assertTrue("told why", notices.contains(Strings.t.noticeTorrentHasSymlinks))
    }

    // ------------------------------------------------------------------ names

    @Test
    fun aMagnetGetsItsRealNameOnceTheEngineKnowsIt() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet(magnetB)
        await("the add") { engine.magnets.any { it == magnetB } }
        engine.names[hB] = "Ubuntu 26.04 Desktop"

        runBlocking { r.metadataArrived(hB) }
        await("the name is saved") { savedName() == "Ubuntu 26.04 Desktop" }
    }

    @Test
    fun aNameIsListedOnOneLineAndNotEndlessly() {
        // It comes from the torrent — from strangers: into the list, the player title and the saved list.
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }
        val expected = "Line one Line two " + "x".repeat(300)
        engine.names[hB] = "Line one\nLine two\r\n\u0007\u202E" + "x".repeat(300)

        runBlocking { r.metadataArrived(hB) }

        await("the cleaned name is saved") { savedName() == expected.take(200) }
        assertEquals("as the confirmation would show it, and no longer than 200 characters", expected.take(200), savedName())
    }

    @Test
    fun aMagnetsTitleIsWhatItIsListedUnderUntilThen() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet("magnet:?xt=urn:btih:$hB&dn=Show")
        await("the add") { engine.magnets.any { hB in it } }
        await("the title is saved") { "\"n\":\"Show\"" in savedState() }
    }

    @Test
    fun aTorrentAddedFromAFileIsListedUnderItsName() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val file = TestTorrents.write(File(context.cacheDir, "clean.torrent"), "Clean Name", "README")

        var ok: Boolean? = null
        r.addTorrentFile(file) { ok = it }

        await("the add") { ok != null }
        assertEquals(true, ok)
        await("the torrent's own name is saved, not its hash") { "\"n\":\"Clean Name\"" in savedState() }
        assertTrue(keptTorrent(TorrentMeta.readTorrent(TestTorrents.bytes("Clean Name", "README"))!!.infoHash).isFile)
    }

    @Test
    fun anOldEntryStoredUnderItsHashGetsItsNameBackAtStart() {
        // What 1.4.13 saved for a torrent added from a .torrent file.
        val bytes = TestTorrents.bytes("Clean Name", "README")
        val hash = TorrentMeta.readTorrent(bytes)!!.infoHash
        File(context.cacheDir, "clean.torrent").writeBytes(bytes)
        keptTorrent(hash).apply { parentFile?.mkdirs() }.writeBytes(bytes)
        File(context.filesDir, "torrents_state.json").writeText(
            """[{"h":"$hash","m":"magnet:?xt=urn:btih:$hash","n":"$hash","s":false,"a":0,"p":false,"pa":false}]""",
        )

        val engine = FakeTorrentEngine()
        repo(engine).start()

        await("the engine re-adds it from the kept file") { engine.files.any { it.endsWith("$hash.torrent") } }
        await("its real name replaces the hash") { "\"n\":\"Clean Name\"" in savedState() }
    }

    /** What a poll reports for one torrent. */
    private fun polledItem(state: TorrentState, hash: String) = TorrentItem(
        infoHash = hash, name = hash.take(6), magnetUri = "magnet:?xt=urn:btih:$hash", sequential = false,
        addedAt = 0L, state = state, progress = 0f, totalBytes = 10L, doneBytes = 0L,
        downloadRate = 0L, uploadRate = 0L, peers = 0, seeds = 0,
    )

    @Test
    fun aNameThatReadsAsSomethingElseIsListedAsItIsWritten() {
        // A right-to-left override makes "report<override>fdp.exe" display as if it began with "exe".
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }
        engine.names[hB] = "report\u202Efdp.exe"

        runBlocking { r.metadataArrived(hB) }

        await("the name is saved") { savedName() == "reportfdp.exe" }
    }

    @Test
    fun aNameSavedRawByAnEarlierVersionIsListedCleaned() {
        // 1.4.13 saved a magnet's title as it came — an invisible override and a line break included.
        File(context.filesDir, "torrents_state.json").writeText(
            JSONArray().put(
                JSONObject().put("h", hB).put("m", "magnet:?xt=urn:btih:$hB&dn=x").put("n", "re\u202Eport.exe\nx").put("s", false).put("a", 0),
            ).toString(),
        )
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("the saved list is read") { engine.starts >= 1 }

        assertEquals("as the confirmation would show it", "report.exe x", r.listedNameOf(hB))
    }

    // ------------------------------------------------------------------ refused with the reason

    @Test
    fun aTorrentFileWithASymbolicLinkIsRefusedAndSaysWhy() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val notices = noticesOf(r)
        val hash = TorrentMeta.readTorrent(TestTorrents.bytes("Evil", "README", "link.txt"))!!.infoHash
        engine.symlinkHashes.add(hash)
        val file = TestTorrents.write(File(context.cacheDir, "evil.torrent"), "Evil", "README", "link.txt")

        var ok: Boolean? = null
        r.addTorrentFile(file) { ok = it }

        await("the refusal") { ok != null }
        assertEquals(false, ok)
        assertTrue(engine.files.isEmpty())
        assertTrue("told the real reason", notices.contains(Strings.t.noticeTorrentHasSymlinksFile))
        assertFalse("…and not that it were no torrent", notices.contains(Strings.t.noticeNotTorrent))
        await("nothing saved") { hash !in savedState() }
    }

    @Test
    fun aTorrentTheEngineCouldNotTakeSaysSo() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val notices = noticesOf(r)
        // A .torrent the engine is up for and still will not take — a disk error, a duplicate.
        engine.failFileAdds = "duplicate torrent"
        val file = TestTorrents.write(File(context.cacheDir, "clean.torrent"), "Clean Name", "README")

        var ok: Boolean? = null
        r.addTorrentFile(file) { ok = it }

        await("the refusal") { ok != null }
        assertEquals(false, ok)
        assertTrue("told it could not be taken", notices.contains(Strings.t.noticeCouldNotAddTorrent))
        assertFalse("…and not that it were no torrent", notices.contains(Strings.t.noticeNotTorrent))
    }

    @Test
    fun aFileThatIsNoTorrentSaysSo() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val notices = noticesOf(r)
        val file = File(context.cacheDir, "garbage.torrent").apply { writeText("this is not bencode") }

        var ok: Boolean? = null
        r.addTorrentFile(file) { ok = it }

        await("the refusal") { ok != null }
        assertEquals(false, ok)
        assertTrue(notices.contains(Strings.t.noticeNotTorrent))
        assertFalse(notices.contains(Strings.t.noticeTorrentHasSymlinksFile))
    }

    // ------------------------------------------------------------------ copied once, also across an update

    @Test
    fun aTorrentFinishedBeforeTheCopyWasRememberedIsLeftAsItIs() {
        // What a start finds after an update from 1.4.13: a finished torrent whose entry
        // was saved before anything was remembered about copies.
        startWith(hB, TestTorrents.bytes(FakeTorrentEngine.KEPT_NAME, "file.bin"))
        val engine = FakeTorrentEngine()
        val r = repo(engine)

        await("the saved list is read") { engine.starts >= 1 }

        assertFalse("not copied again at the first start — a deleted copy came back", r.needsCopy(hB))
    }

    @Test
    fun aTorrentStillDownloadingIsCopiedWhenItFinishesEvenIfItsEntryPredatesTheCopyMark() {
        startWith(hB, TestTorrents.bytes(FakeTorrentEngine.KEPT_NAME, "file.bin"))
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("the saved list is read") { engine.starts >= 1 }

        r.watchDownloading(listOf(polledItem(TorrentState.DOWNLOADING, hB)))

        assertTrue("its files are being written anew: the finish is yet to be copied", r.needsCopy(hB))
    }

    @Test
    fun aTorrentWhoseFilesCouldNotBeListedIsNotMarkedCopied() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("the saved list is read") { engine.starts >= 1 }
        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }
        r.onFinished(hB) // the finish of an entry that is there — a mark would stick, if one were made

        assertFalse(r.copyFinishedOnce(hB, null))
        assertTrue("asked again at the next start", r.needsCopy(hB))
        assertFalse("and nothing is saved as copied", "\"c\":true" in savedState())
    }

    @Test
    fun onlyACopyRunWithNoFailureMarksTheTorrentCopied() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("the saved list is read") { engine.starts >= 1 }
        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }
        val noRoom = space.deepflux.android.dlmgr.FinishedExport.Result.Failed("no room")

        assertFalse(r.copyFinishedOnce(hB, listOf("a.mkv", "b.mkv")) { _, _, _ -> noRoom })
        assertTrue("a run with a failure is not a copy", r.needsCopy(hB))

        r.onFinished(hB) // the engine's "finished" comes first, and takes the torrent off the watch list
        assertTrue(r.copyFinishedOnce(hB, listOf("a.mkv", "b.mkv")) { _, _, _ -> null })
        assertFalse(r.needsCopy(hB))
        await("remembered") { "\"c\":true" in savedState() }
    }

    // ------------------------------------------------------------------ kept files and half-written ones at a start

    @Test
    fun aKeptFileThatIsATorrentStaysWhenTheEngineCannotReadItJustNow() {
        // It may be all a private torrent will ever get: a moment's read error is not a verdict.
        startWith(hB, TestTorrents.bytes(FakeTorrentEngine.KEPT_NAME, "file.bin"))
        val engine = FakeTorrentEngine()
        engine.refuseKeptFiles = true

        repo(engine).start()

        await("the magnet is what the engine has for now") { engine.magnets.any { hB in it } }
        Thread.sleep(300)
        assertTrue("the file is kept", keptTorrent(hB).isFile)
    }

    @Test
    fun aHalfWrittenKeepFromAnEarlierStartIsSweptButNotOneBeingWrittenNow() {
        val dir = File(context.filesDir, "torrents").apply { mkdirs() }
        val old = File(dir, "aaaa.torrent.tmp").apply {
            writeText("cut short")
            setLastModified(System.currentTimeMillis() - 120_000)
        }
        val fresh = File(dir, "bbbb.torrent.tmp").apply { writeText("being written right now") }
        val engine = FakeTorrentEngine()

        repo(engine).start()

        await("the old one is gone") { !old.exists() }
        Thread.sleep(500)
        assertTrue("one being written right now stays", fresh.exists())
    }

    @Test
    fun aFailedKeepIsNotRetriedAtTheVeryNextPoll() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet(magnetB)
        await("the add") { engine.magnets.any { it == magnetB } }
        engine.failSaving = true
        runBlocking { r.metadataArrived(hB) }
        assertEquals("the alert's keep", 1, engine.saveCalls)
        engine.names[hB] = "Ubuntu 26.04 Desktop" // there is something to retry keeping

        repeat(5) {
            await("no keep in flight") { !r.isKeeping(hB) }
            runBlocking { r.refreshForTests() }
        }
        await("settled") { !r.isKeeping(hB) }

        assertEquals("a minute passes before the first retry, not one poll", 1, engine.saveCalls)
    }

    // ------------------------------------------------------------------ across an update from 1.4.13

    @Test
    fun aTorrentSeenDownloadingStaysToBeCopiedAcrossARestart() {
        startWith(hB, TestTorrents.bytes(FakeTorrentEngine.KEPT_NAME, "file.bin"))
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("the saved list is read") { engine.starts >= 1 }

        r.watchDownloading(listOf(polledItem(TorrentState.DOWNLOADING, hB)))
        await("that it is still downloading is saved") { "\"c\":false" in savedState() }

        // Its copy fails later, nothing else is saved, the app goes: the next start must ask again.
        r.shutdownForTests()
        val second = FakeTorrentEngine()
        val again = repo(second)
        await("the saved list is read again") { second.starts >= 1 }

        assertTrue("still to be copied: the mark did not come back with the restart", again.needsCopy(hB))
    }

    @Test
    fun aKeptFileTheEngineWouldNotTakeIsWrittenOverWhenTheInfoArrives() {
        startWith(hB, TestTorrents.bytes(FakeTorrentEngine.KEPT_NAME, "file.bin"))
        val engine = FakeTorrentEngine()
        engine.refuseKeptFiles = true
        val r = repo(engine)
        await("the magnet is what the engine has for now") { engine.magnets.any { hB in it } }
        assertTrue("kept: it may be all a private torrent will ever get", keptTorrent(hB).isFile)

        runBlocking { r.metadataArrived(hB) }

        assertEquals("the metadata is written over it, not skipped because a file is there", 1, engine.saveCalls)
    }

    // ------------------------------------------------------------------ a magnet's own peer addresses

    @Test
    fun aMagnetsOwnPeerAddressesAreHandedToTheTorrentKeptFromItsFile() {
        // A .torrent holds trackers, not the addresses a magnet names as peers: told again
        // when the torrent comes back from the kept file — or a trackerless magnet has no peer.
        val magnet = "magnet:?xt=urn:btih:$hB&dn=Kept torrent&x.pe=10.0.2.2:47632,192.168.1.4:6881&x.pe=10.0.2.2:47632"
        keptTorrent(hB).apply { parentFile?.mkdirs() }.writeBytes(TestTorrents.bytes(FakeTorrentEngine.KEPT_NAME, "file.bin"))
        File(context.filesDir, "torrents_state.json").writeText(
            JSONArray().put(JSONObject().put("h", hB).put("m", magnet).put("n", "Kept torrent").put("s", false).put("a", 0)).toString(),
        )
        val engine = FakeTorrentEngine()

        repo(engine).start()

        await("back from the kept file") { engine.files.any { it.endsWith("$hB.torrent") } }
        await("told its peers") { engine.peers.containsKey(hB) }
        assertEquals("each address once", listOf("10.0.2.2" to 47632, "192.168.1.4" to 6881), engine.peers[hB])
        assertTrue("no magnet was needed", engine.magnets.isEmpty())
    }

    @Test
    fun aTorrentAddedAsAMagnetIsNotToldPeersOnTop() {
        // The engine takes the addresses from a magnet itself.
        File(context.filesDir, "torrents_state.json").writeText(
            JSONArray().put(
                JSONObject().put("h", hB).put("m", "magnet:?xt=urn:btih:$hB&x.pe=10.0.2.2:47632").put("n", "Show").put("s", false).put("a", 0),
            ).toString(),
        )
        val engine = FakeTorrentEngine()

        repo(engine).start()

        await("added as a magnet") { engine.magnets.any { hB in it } }
        Thread.sleep(300)
        assertTrue(engine.peers.isEmpty())
    }

    // ------------------------------------------------------------------ Remove while the name changes

    @Test
    fun aRemoveDuringTheRenameStillRemovesIt() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }
        engine.names[hB] = "Ubuntu 26.04 Desktop"
        val hold = FakeTorrentEngine.Hold().also { engine.holdName = it }
        collectors += CoroutineScope(Dispatchers.Unconfined).launch { r.metadataArrived(hB) }

        // The name is one step from being listed — and Remove is tapped right then.
        assertTrue("the keep reached its last step", hold.asked.await(10, TimeUnit.SECONDS))
        r.remove(hB)
        hold.release()
        await("the keep is done") { !r.isKeeping(hB) }

        await("gone for good — not back at the next start") { hB !in savedState() }
        assertTrue("and out of the engine", hB in engine.removed)
    }

    // ------------------------------------------------------------------ finished: announced once, copied once

    @Test
    fun aTorrentAddedHereIsAnnouncedWhenItFinishes() {
        grantPostNotifications()
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }
        val before = finishedAnnouncements().size

        r.onFinished(hB)
        r.onFinished(hB) // the engine may say it more than once

        await("the announcement") { finishedAnnouncements().size == before + 1 }
        Thread.sleep(200)
        assertEquals("said once", before + 1, finishedAnnouncements().size)
        assertEquals(context.getString(R.string.notif_generic_torrent), finishedAnnouncements().last())
    }

    @Test
    fun aTorrentThatWasCompleteAtStartIsNotAnnouncedAgain() {
        grantPostNotifications()
        // A finished torrent the way a start finds it: in the list, its metadata kept.
        startWith(hB, TestTorrents.bytes(FakeTorrentEngine.KEPT_NAME, "file.bin"))
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("it came back from the kept file") { engine.files.any { it.endsWith("$hB.torrent") } }
        val before = finishedAnnouncements().size

        r.onFinished(hB) // what the engine says for every complete torrent at start

        Thread.sleep(200)
        assertEquals("no \"Download finished\" on opening the app", before, finishedAnnouncements().size)
    }

    @Test
    fun onlyWhatWasSeenDownloadingIsNewsWhenItFinishes() {
        grantPostNotifications()
        val rechecked = "1".repeat(40) // complete, its files gone through again at a start
        val downloading = "2".repeat(40)
        val fetching = "3".repeat(40) // "Fetching info"
        val seeding = "4".repeat(40)
        val hashes = listOf(rechecked, downloading, fetching, seeding)
        // Torrents a start finds in the saved list — none of them added in this process.
        File(context.filesDir, "torrents_state.json").writeText(
            JSONArray().also { arr ->
                hashes.forEach { arr.put(JSONObject().put("h", it).put("m", "magnet:?xt=urn:btih:$it").put("n", it.take(6)).put("s", false).put("a", 0)) }
            }.toString(),
        )
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("the saved list is read") { engine.starts >= 1 }
        fun polled(state: TorrentState, hash: String) = TorrentItem(
            infoHash = hash, name = hash.take(6), magnetUri = "magnet:?xt=urn:btih:$hash", sequential = false,
            addedAt = 0L, state = state, progress = 0f, totalBytes = 10L, doneBytes = 0L,
            downloadRate = 0L, uploadRate = 0L, peers = 0, seeds = 0,
        )
        val before = finishedAnnouncements().size

        r.watchDownloading(
            listOf(
                polled(TorrentState.CHECKING, rechecked),
                polled(TorrentState.DOWNLOADING, downloading),
                polled(TorrentState.METADATA, fetching),
                polled(TorrentState.SEEDING, seeding),
            ),
        )
        hashes.forEach { r.onFinished(it) }

        await("the one announcement that is news") { finishedAnnouncements().size == before + 1 }
        Thread.sleep(200)
        assertEquals("only the one seen downloading, by its name", listOf("222222"), finishedAnnouncements().drop(before))
    }

    @Test
    fun aFinishedTorrentWhoseFilesWereCopiedIsNotCopiedAgain() {
        startWith(hB, TestTorrents.bytes(FakeTorrentEngine.KEPT_NAME, "file.bin"))
        File(context.filesDir, "torrents_state.json").writeText(
            """[{"h":"$hB","m":"magnet:?xt=urn:btih:$hB","n":"Kept torrent","s":false,"a":0,"p":false,"pa":false,"c":true}]""",
        )
        val engine = FakeTorrentEngine()
        val r = repo(engine)

        await("the saved list is read") { engine.starts >= 1 }

        assertFalse("copied before — not again at every start", r.needsCopy(hB))
    }

    @Test
    fun whatWasCopiedIsRememberedAndGoesWithTheTorrent() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("the saved list is read") { engine.starts >= 1 }
        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }
        assertTrue("nothing copied yet", r.needsCopy(hB))

        r.onFinished(hB) // the engine's "finished" comes first, and takes the torrent off the watch list
        r.markCopied(hB)

        await("remembered") { "\"c\":true" in savedState() }
        assertFalse(r.needsCopy(hB))

        r.remove(hB)
        await("the entry is gone") { hB !in savedState() }
        r.addMagnet(magnetB) // the same torrent again is a new entry
        await("the add is saved") { hB in savedState() }
        assertTrue("a torrent added afresh has not been copied", r.needsCopy(hB))
    }

    // ------------------------------------------------------------------ a hybrid torrent from a v1 magnet

    /**
     * The emulator, 2026-11-25. A hybrid torrent has two hashes. The engine
     * names it by its v2 one; the magnet that added it carried its v1 one, so
     * the entry is keyed by that. Nothing that compared the two ever matched:
     * the entry's own kept file read as another torrent's and was deleted at
     * every start, the finish was never announced, the copy never marked.
     */

    /** The v1 hash of "Other Name" — the key its entry gets from a magnet that carries the v1 hash. */
    private val hOther get() = TorrentMeta.readTorrent(TestTorrents.bytes("Other Name", "README"))!!.infoHash

    /** The engine knows "Other Name" as a hybrid torrent: v1 hash [hOther], named by its v2 prefix [hB]. */
    private fun hybridIn(engine: FakeTorrentEngine) {
        engine.v2Prefixes[hOther] = hB
    }

    @Test
    fun aHybridTorrentsKeptFileIsThisEntrySTorrentByItsV1Hash() {
        startWith(hOther, TestTorrents.bytes("Other Name", "README"))
        val engine = FakeTorrentEngine()
        hybridIn(engine)

        repo(engine).start()

        await("back from the kept file, under its own name") { savedName() == "Other Name" }
        assertTrue("no magnet was needed — it would have had to be handed its info again", engine.magnets.isEmpty())
        assertTrue("the file stays", keptTorrent(hOther).isFile)
        assertTrue(engine.removed.isEmpty())
    }

    @Test
    fun anotherEntrySTorrentIsNotTakenAwayHoweverThatEntryIsKeyed() {
        // B owns "Other Name" by its v1 hash; A's kept file holds that very torrent — which is not A's.
        val aKey = "7".repeat(40)
        keptTorrent(aKey).apply { parentFile?.mkdirs() }.writeBytes(TestTorrents.bytes("Other Name", "README"))
        keptTorrent(hOther).apply { parentFile?.mkdirs() }.writeBytes(TestTorrents.bytes("Other Name", "README"))
        File(context.filesDir, "torrents_state.json").writeText(
            JSONArray().also { arr ->
                arr.put(JSONObject().put("h", hOther).put("m", "magnet:?xt=urn:btih:$hOther").put("n", "B").put("s", false).put("a", 0))
                arr.put(JSONObject().put("h", aKey).put("m", "magnet:?xt=urn:btih:$aKey").put("n", "A").put("s", false).put("a", 0))
            }.toString(),
        )
        val engine = FakeTorrentEngine()
        hybridIn(engine)

        repo(engine).start()

        await("A's wrong file is gone") { !keptTorrent(aKey).exists() }
        await("A is asked as a magnet") { engine.magnets.any { aKey in it } }
        assertTrue("B's torrent was not taken out of the engine", hB !in engine.removed && hOther !in engine.removed)
        assertTrue("B came back from its own kept file", engine.files.any { it.endsWith("$hOther.torrent") })
    }

    @Test
    fun whatTheEngineReportsAboutAHybridTorrentIsKeptUnderItsEntrySKey() {
        val engine = FakeTorrentEngine()
        hybridIn(engine)
        engine.names[hOther] = "Other Name"
        val r = repo(engine)
        r.addMagnet("magnet:?xt=urn:btih:$hOther")
        await("the add is saved") { hOther in savedState() }

        runBlocking { r.metadataArrived(hB) } // said by the torrent's v2 hash, as the engine does

        await("kept under the entry's key") { keptTorrent(hOther).isFile }
        await("and the entry has the torrent's own name") { savedName() == "Other Name" }
        assertTrue("nothing under the engine's name", !keptTorrent(hB).exists())
    }

    @Test
    fun aHybridTorrentWithALinkIsGoneFromTheListHoweverTheEngineNamesIt() {
        val engine = FakeTorrentEngine()
        hybridIn(engine)
        val r = repo(engine)
        val notices = noticesOf(r)
        r.addMagnet("magnet:?xt=urn:btih:$hOther")
        await("the add is saved") { hOther in savedState() }
        engine.symlinkHashes.add(hOther)

        runBlocking { r.metadataArrived(hB) } // said by the torrent's v2 hash

        await("told why") { notices.contains(Strings.t.noticeTorrentHasSymlinks) }
        assertTrue("gone from the saved list", hOther !in savedState())
        assertTrue("and out of the engine", hB in engine.removed || hOther in engine.removed)
        assertEquals(0, engine.saveCalls)
    }

    @Test
    fun theEngineSHashOfAHybridTorrentIsItsEntryS() {
        val engine = FakeTorrentEngine()
        hybridIn(engine)
        val r = repo(engine)
        r.addMagnet("magnet:?xt=urn:btih:$hOther")
        await("the add is saved") { hOther in savedState() }

        runBlocking {
            assertEquals("said by the v2 hash, it is the entry keyed by the v1 one", hOther, r.entryOfEngineHash(hB))
            assertEquals("an entry's own key stays", hOther, r.entryOfEngineHash(hOther))
            assertNull("a torrent no entry owns", r.entryOfEngineHash("9".repeat(40)))
            assertNull(r.entryOfEngineHash(null))
        }
    }

    // ------------------------------------------------------------------ after the reviews (2026-11-25)

    @Test
    fun aKeptFileUnderAnotherHashIsGivenToItsEntryAtStart() {
        // A device that ran the first 1.4.14 test build: the entry is keyed by the magnet's v1 hash, the
        // metadata was kept under the engine's name (the v2 one). Without this the entry would still have
        // to be handed its info by some peer after the update — the very trouble this version fixes.
        startWith(hOther, TestTorrents.bytes("Other Name", "README"))
        assertTrue("as that build left it", keptTorrent(hOther).renameTo(keptTorrent(hB)))
        val engine = FakeTorrentEngine()
        hybridIn(engine)

        repo(engine).start()

        await("the file is the entry's now") { keptTorrent(hOther).isFile && !keptTorrent(hB).exists() }
        await("back from it, under its own name") { savedName() == "Other Name" }
        assertTrue("no magnet was needed", engine.magnets.isEmpty())
    }

    @Test
    fun aKeptFileNoEntryOwnsIsNotLeftInTheSession() {
        val stray = "7".repeat(40)
        startWith(stray, TestTorrents.bytes("Other Name", "README"))
        assertTrue("a file no entry is named by", keptTorrent(stray).renameTo(keptTorrent(hOther)))
        val engine = FakeTorrentEngine()

        repo(engine).start()

        await("the entry is asked as a magnet") { engine.magnets.any { stray in it } }
        assertTrue("the other torrent is not left in the session", hOther in engine.removed)
        assertTrue("the file itself is left as it is", keptTorrent(hOther).isFile)
    }

    @Test
    fun nothingIsMarkedForATorrentRemovedWhileItsCopyRan() {
        // Remove during the copy, then the same torrent added afresh: it is a new entry, to be copied like any.
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("the saved list is read") { engine.starts >= 1 }
        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }

        r.remove(hB)
        await("the entry is gone") { hB !in savedState() }
        r.markCopied(hB) // the copy that was running ends now

        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }
        assertTrue("no mark survived the Remove: the new entry is to be copied", r.needsCopy(hB))
        assertFalse("nor is one saved for it", "\"c\":true" in savedState())
    }

    @Test
    fun aTorrentSeenDownloadingAgainWhileItsCopyRanIsNotMarkedCopied() {
        // Its files are being written anew: what the copy took is not their final state — its next finish copies again.
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("the saved list is read") { engine.starts >= 1 }
        r.addMagnet(magnetB)
        await("the add is saved") { hB in savedState() }
        r.onFinished(hB)
        r.watchDownloading(listOf(polledItem(TorrentState.DOWNLOADING, hB))) // a poll during the copy

        r.markCopied(hB)

        assertTrue("not marked", r.needsCopy(hB))
    }

    @Test
    fun theSameTorrentAddedAsAFileBesideItsMagnetEntryIsNotListedTwice() {
        // A hybrid torrent: its entry is keyed by the magnet's v1 hash. The .torrent file is that very torrent.
        val engine = FakeTorrentEngine()
        hybridIn(engine)
        val r = repo(engine)
        val notices = noticesOf(r)
        r.addMagnet("magnet:?xt=urn:btih:$hOther")
        await("the add is saved") { hOther in savedState() }
        val file = TestTorrents.write(File(context.cacheDir, "same.torrent"), "Other Name", "README")

        var ok: Boolean? = null
        r.addTorrentFile(file) { ok = it }

        await("the answer") { ok != null }
        assertEquals(false, ok)
        await("told it is in the list already") { notices.contains(Strings.t.noticeAlreadyInList) }
        await("one entry still") { savedState().isNotBlank() && JSONArray(savedState()).length() == 1 }
        assertTrue("and the file is kept for that entry, under ITS key", keptTorrent(hOther).isFile)
    }

    /** What the test looked for and did not find — `fail` returns, this does not. */
    private fun missing(what: String): Nothing = throw AssertionError(what)

    // ------------------------------------------------------------------ the alerts are wired to all of this

    @Test
    fun theAlertsAreWiredToWhatTheTestsCover() {
        // No alert can be built on the JVM; this reads the two branches that hand over to what is tested.
        val source = repositorySource() ?: missing("could not find the repository's source from the test's working folder")
        val metadataAt = source.indexOf("is MetadataReceivedAlert -> {")
        val metadata = source.substringAfter("is MetadataReceivedAlert -> {").substringBefore("else ->")
        assertTrue("the metadata alert branch was found", metadataAt >= 0 && metadataAt < source.lastIndexOf("else ->"))
        assertTrue("the metadata alert keeps the metadata: $metadata", "scope.launch { metadataArrived(hex) }" in metadata)
        val finished = source.substringAfter("is TorrentFinishedAlert -> {").substringBefore("is TorrentErrorAlert -> {")
        assertTrue("the finished alert is handled by the entry's hash, not the engine's: $finished", "finishedFromEngine(hex)" in finished)
        assertFalse("nobody announces straight from the alert", "postEventNotification(EventKind.FINISHED" in finished)
        // A hybrid torrent: the engine names it by its v2 hash, the entry is keyed by the magnet's one.
        val handling = source.substringAfter("private suspend fun finishedFromEngine(").substringBefore("internal suspend fun entryOfEngineHash(")
        val lookup = handling.indexOf("entryOfEngineHash(engineHash)")
        assertTrue("looked up whose torrent it is first", lookup >= 0 && lookup < handling.indexOf("onFinished(entry)"))
        assertTrue("the entry's hash announces", "onFinished(entry)" in handling)
        assertTrue("…and copies", "exportFinishedFiles(entry)" in handling)
        assertFalse("never the engine's hash straight", "onFinished(engineHash)" in source || "exportFinishedFiles(engineHash)" in source)
        assertTrue("the metadata alert is kept under the entry's key", "val infoHash = entryOfEngineHash(engineHash) ?: return" in source)
        assertTrue("a kept file is the entry's by either of its hashes", "kept.infoHash == e.infoHash || kept.v1Hash == e.infoHash" in source)
        // What the reviews found (2026-11-25): kept files under a hash no entry has are given to their entry
        // first; the pool says whether it took the copy; a mark is a finish's only while the entry is that torrent's.
        assertTrue("kept files under another hash are given to their entries before the entries are restored", source.indexOf("adoptOrphanKeptFiles()") in 0 until source.indexOf("entries.forEach { e ->"))
        assertTrue("only a job the pool took is the pool's", "queued = space.deepflux.android.dlmgr.FinishedExport.submit {" in source.substringAfter("TorrentFinishedAlert, infoHash: String?) {").substringBefore("internal fun copyFinishedOnce("))
        assertTrue("and the pool says whether it did", "fun submit(job: () -> Unit): Boolean {" in (appSource("src/main/java/space/deepflux/android/dlmgr/FinishedExport.kt") ?: missing("no FinishedExport source")))
        assertTrue("a mark is for a torrent that is still this entry's, and not one seen downloading again", "if (infoHash in unfinishedSeen || entries.none { it.infoHash == infoHash }) return" in source)
        assertTrue("a magnet's addresses cannot stop the entries after it", "could not read the magnet's peer addresses" in source)
        // NO ALERT OUTLIVES ITS CALLBACK (a native crash at a torrent's finish, emulator, 2026-11-25): the binding
        // wraps an alert's handle without owning it. The copy-out looks the torrent up in the session instead.
        val copyStart = source.substringAfter("private fun exportFinishedFiles(infoHash: String) {").substringBefore("internal fun copyFinishedOnce(")
        assertTrue("the copy-out asks the session for the torrent's files", "engine.filePathsOf(infoHash)" in copyStart)
        assertFalse("…and touches no alert after its callback", "alert.handle()" in copyStart || "TorrentFinishedAlert" in copyStart)
        assertFalse("the finished alert is not handed on", "finishedFromEngine(alert" in source)
        assertTrue("…and the engine reads the files from a handle of its own", "session.find(Sha1Hash.parseHex(infoHashHex))?.torrentFile() ?: return null" in (appSource("src/main/java/space/deepflux/android/engine/TorrentEngine.kt") ?: missing("no engine source")))

        assertTrue("each poll reports what is downloading: it decides what is news when it finishes", "watchDownloading(items)" in source)

        // The copy the finished alert starts asks first whether it was done, hands over to the one
        // copy run, and holds the torrent as "being copied" until that run is over.
        val starter = source.substringAfter("private fun exportFinishedFiles(infoHash: String) {").substringBefore("internal fun copyFinishedOnce(")
        assertTrue("asked first whether it was copied: ${starter.take(200)}", "!needsCopy(infoHash)" in starter)
        assertTrue("and hands over to the one copy run", "copyFinishedOnce(infoHash, paths)" in starter)
        assertTrue("the mark is held until the copy is over", "copyFinishedOnce(infoHash, paths)\n                        } finally {\n                            copyingHashes.remove(infoHash)" in starter)
        val run = source.substringAfter("internal fun copyFinishedOnce(").substringBefore("\n    /** True while a finished torrent")
        assertTrue("a run whose files could not be listed is no copy", "if (paths == null)" in run)
        assertTrue("nor is one with a failure", "!= 0) return false" in run)
        assertTrue("one with none marks the torrent", "markCopied(infoHash)" in run)

        // The engine writes the kept file through the builder a test decodes — never bytes of its own.
        val engineSource = appSource("src/main/java/space/deepflux/android/engine/TorrentEngine.kt") ?: missing("no engine source")
        assertTrue("the kept file is written through the builder", "tmp.writeBytes(KeptTorrent.bytes(" in engineSource)
        assertTrue("and read back before it counts", "TorrentInfo(tmp)" in engineSource)
        assertTrue("the engine tells every hash of a torrent it adds (1.4.16: its whole v2 one too)", "orEmpty(), v1Hash(info), v2Hash(info))" in engineSource)
        assertTrue("and the whole v2 hash is read where there is one", "if (hashes.has_v2()) hashes.getV2().to_hex().lowercase() else null" in engineSource)
        assertTrue(
            "the engine hands libtorrent the magnet in the one spelling it parses (emulator, 2026-11-28: as written, it refused a v2-only one)",
            "session.download(MagnetParser.forEngine(magnet), saveDir, org.libtorrent4j.swig.torrent_flags_t())" in engineSource,
        )
        assertTrue("the v1 hash is read as the v2 prefix is", "if (hashes.has_v1()) hashes.getV1().to_hex().lowercase() else null" in engineSource)
    }
}
