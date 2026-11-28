package space.deepflux.android

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import org.json.JSONArray
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
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.ui.Strings
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList

/**
 * 1.4.16 — v2 torrents in the LIST, driven through the real repository with its test engine.
 * The files are the libtorrent reference torrents ([RefTorrents]); the fake engine reads
 * them the way the real one names them (TorrentMeta, checked against libtorrent in
 * TorrentIdentityV2Test).
 *
 * The rules, each learned from something that went wrong:
 *  * a v2-only MAGNET (64-hex btih) is a magnet: the entry is keyed by the engine's 20-byte
 *    name — the same key the torrent has when it is added from a .torrent file — and the
 *    engine gets the magnet whole;
 *  * the magnet SAVED with a torrent names the torrent (v1, else the whole v2 hash) — the
 *    one built from the 20-byte name alone named nothing that was v2;
 *  * one torrent, one row — whichever of its hashes it was added by;
 *  * a list saved by an earlier version, with such a nameless magnet, is put right at the
 *    start from the kept file's own hashes.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class TorrentV2IdentityTest {

    private lateinit var context: RecordingContext
    private val repos = mutableListOf<TorrentRepository>()
    private val collectors = mutableListOf<Job>()
    private val v2 = RefTorrents.v2
    private val hybrid = RefTorrents.hybrid

    @Before
    fun setUp() {
        context = RecordingContext(ApplicationProvider.getApplicationContext())
    }

    @After
    fun tearDown() {
        collectors.forEach { it.cancel() }
        repos.forEach { it.shutdownForTests() }
    }

    private fun repo(engine: FakeTorrentEngine = FakeTorrentEngine()): TorrentRepository =
        TorrentRepository(context, engine).also { repos += it }.also { it.start() }

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

    private fun savedState(): String {
        val f = File(context.filesDir, "torrents_state.json")
        if (!f.isFile) return lastSavedState
        runCatching { f.readText() }.onSuccess { lastSavedState = it }
        return lastSavedState
    }

    /** The saved entry of [key], as a restart will read it. */
    private fun savedEntry(key: String): Pair<String, String>? = savedState().takeIf { it.isNotBlank() }
        ?.let { runCatching { JSONArray(it) }.getOrNull() }
        ?.let { arr -> (0 until arr.length()).map { arr.getJSONObject(it) }.firstOrNull { it.optString("h") == key } }
        ?.let { it.optString("m") to it.optString("n") }

    private fun keptTorrent(key: String) = File(File(context.filesDir, "torrents"), "$key.torrent")

    private fun staged(ref: RefTorrents.Ref): File = File(context.cacheDir, "ref-${System.nanoTime()}.torrent").apply { writeBytes(ref.bytes) }

    /** The keys of the entries on disk — the truth a restart wakes up to. (The on-screen list is
     * built from the engine's statuses, and the test engine reports none.) */
    private fun savedKeys(): List<String> = savedState().takeIf { it.isNotBlank() }
        ?.let { runCatching { JSONArray(it) }.getOrNull() }
        ?.let { arr -> (0 until arr.length()).map { arr.getJSONObject(it).optString("h") } }
        .orEmpty()

    /** The user confirms a .torrent file handed over — as a tap on Add does. */
    private fun addFile(r: TorrentRepository, ref: RefTorrents.Ref): Unit = r.requestAddTorrentFile(staged(ref), userInitiated = true).let {
        await("the confirmation") { r.pendingAdd.value != null }
        r.confirmPendingAdd()
        await("the torrent is listed") { ref.engineHash in savedKeys() }
    }

    /** What a start finds: one entry with this key and magnet, and a kept file holding [kept]. */
    private fun startWith(key: String, magnet: String, kept: ByteArray, name: String = "Old") {
        keptTorrent(key).apply { parentFile?.mkdirs() }.writeBytes(kept)
        File(context.filesDir, "torrents_state.json").writeText(
            """[{"h":"$key","m":"$magnet","n":"$name","s":false,"a":0,"p":false,"pa":false}]""",
        )
    }

    // ------------------------------------------------------------------ by magnet

    @Test
    fun aV2OnlyMagnetIsAddedAndListedByTheEnginesNameForIt() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val notices = noticesOf(r)
        val magnet = v2.siteMagnet + "&dn=OnlyV2"
        r.addMagnet(magnet)

        await("listed") { v2.engineHash in savedKeys() }
        assertEquals("the engine got the magnet WHOLE — its 64-hex hash included", listOf(magnet), engine.magnets)
        assertEquals("40 hex: the first 20 bytes of the v2 hash", 40, v2.engineHash.length)
        assertEquals(v2.v2!!.take(40), savedKeys().single())
        await("saved with the magnet that names it") { savedEntry(v2.engineHash)?.first == magnet }
        assertFalse("it was 'not a valid magnet link' until 1.4.16", notices.contains(Strings.t.noticeInvalidMagnet))
    }

    @Test
    fun theSameV2OnlyTorrentIsNotListedTwiceHoweverItsMagnetIsWritten() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val notices = noticesOf(r)
        r.addMagnet(v2.siteMagnet + "&dn=a")
        await("listed") { savedKeys().size == 1 }
        r.addMagnet("magnet:?xt=urn:btih:${v2.v2!!.uppercase()}&dn=b")

        await("said to be in the list already") { notices.contains(Strings.t.noticeAlreadyInList) }
        assertEquals(1, savedKeys().size)
        assertEquals("the engine was asked once", 1, engine.magnets.size)
    }

    // ------------------------------------------------------------------ by file

    @Test
    fun aV2OnlyTorrentFromAFileIsSavedWithTheMagnetThatNamesIt() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        addFile(r, v2)

        assertEquals("the same key a magnet add gives it", v2.engineHash, savedKeys().single())
        await("saved") { savedEntry(v2.engineHash) != null }
        val (magnet, name) = savedEntry(v2.engineHash)!!
        assertEquals("magnet:?xt=urn:btih:${v2.v2}", magnet)
        assertEquals("the torrent's own name", "Ref Pack", name)
        assertTrue("its metadata is kept under the engine's name", keptTorrent(v2.engineHash).isFile)
    }

    @Test
    fun aHybridTorrentFromAFileIsSavedWithItsV1Magnet() {
        val r = repo()
        addFile(r, hybrid)
        await("saved") { savedEntry(hybrid.engineHash) != null }
        assertEquals("keyed by the engine's name: the first 20 bytes of its v2 hash", hybrid.v2!!.take(40), savedKeys().single())
        assertEquals("magnet:?xt=urn:btih:${hybrid.v1}", savedEntry(hybrid.engineHash)!!.first)
    }

    @Test
    fun oneTorrentOneRowWhicheverOfItsHashesItWasAddedBy() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val notices = noticesOf(r)
        addFile(r, v2)
        r.addMagnet(v2.siteMagnet)
        await("the magnet is refused as a repeat") { notices.contains(Strings.t.noticeAlreadyInList) }
        assertEquals(1, savedKeys().size)
        assertTrue("nothing was asked of the engine for it", engine.magnets.isEmpty())

        // The other way round, and for a hybrid torrent: the file first (keyed by the engine's
        // name — its v2 one), then a magnet with the v1 hash every site links it by.
        addFile(r, hybrid)
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=hybrid")
        await("the v1 magnet is the listed torrent, not a second row") { savedKeys().size == 2 && notices.count { it == Strings.t.noticeAlreadyInList } >= 2 }
        assertEquals("still two rows: one per torrent", 2, savedKeys().size)
        assertEquals("the v1 magnet reached the engine (the session already holds the torrent), and no row was made for it", 1, engine.magnets.size)
    }

    @Test
    fun theConfirmationOfAV2OnlyFileCarriesTheEnginesNameAndTheMagnetThatNamesIt() {
        val r = repo()
        r.requestAddTorrentFile(staged(v2), userInitiated = true)
        await("the confirmation") { r.pendingAdd.value != null }
        val pending = r.pendingAdd.value!!
        assertEquals(v2.engineHash, pending.infoHash)
        assertEquals("magnet:?xt=urn:btih:${v2.v2}", pending.magnet)
        assertEquals("Ref Pack", pending.name)
    }

    @Test
    fun aV2OnlyFileIsNotAskedForWhenItsTorrentIsListedFromAMagnet() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val notices = noticesOf(r)
        r.addMagnet(v2.siteMagnet)
        await("listed") { savedKeys().size == 1 }
        r.requestAddTorrentFile(staged(v2), userInitiated = true)

        await("said to be in the list already") { notices.contains(Strings.t.noticeAlreadyInList) }
        assertNull("no second confirmation for a torrent that is there", r.pendingAdd.value)
        assertEquals(1, savedKeys().size)
    }

    @Test
    fun decliningAFilesOfferAlsoQuietsTheSameTorrentOfferedAsAMagnet() {
        // A hybrid torrent: its file is keyed by the engine's name (its v2 one), the magnet a
        // page would re-offer carries the v1 hash. Declined once is declined — in both forms.
        val r = repo()
        r.requestAddTorrentFile(staged(hybrid), sourceHost = "page.example")
        await("the confirmation") { r.pendingAdd.value != null }
        r.cancelPendingAdd()

        r.requestAddMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=again", sourceHost = "page.example")
        await("the notice is looked at") { r.pendingAdd.value == null }
        Thread.sleep(300)
        assertNull("the same torrent, declined a moment ago, is not offered again in its other form", r.pendingAdd.value)
        assertTrue(savedKeys().isEmpty())
    }

    @Test
    fun aHybridTorrentIsNotListedAgainByItsOtherMagnetOnceTheEngineKnowsIt() {
        // Added by its v1 magnet (keyed by the v1 hash); the metadata arrives; asked for again
        // by its v2 magnet — the engine names both alike now: the listed torrent, not a row.
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val notices = noticesOf(r)
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=byV1")
        await("listed by its v1 magnet") { savedKeys().size == 1 }
        engine.learned[hybrid.v1!!] = space.deepflux.android.engine.TorrentFileAdd.Added(
            hybrid.engineHash, "Ref Pack", hybrid.v1, hybrid.v2,
        )
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v2}&dn=byV2")

        await("said to be in the list already") { notices.contains(Strings.t.noticeAlreadyInList) }
        assertEquals("one row", listOf(hybrid.v1), savedKeys())
    }

    // ------------------------------------------------------------------ twins in the session
    //
    // What the LIBRARY does with a hybrid torrent added by both of its magnets before either
    // had its metadata (measured on libtorrent 2.1.1, 2026-11-28, and seen on the emulator):
    // a peer offers the metadata to one — the library REFUSES it, stops BOTH with error 19
    // "torrent already exists in session", and leaves the choice to the client. Without the
    // metadata nothing can say which two are one. So nothing is merged until one of them
    // HAS it: the entry added first goes on alone, the other leaves the session and waits.

    /** What the engine knows of a torrent once its metadata arrived: its two hashes, named by its v2 one. */
    private fun meta(engineHash: String, v1: String?, v2: String?) =
        space.deepflux.android.engine.TorrentFileAdd.Added(engineHash, "Ref Pack", v1, v2)

    /** The library's verdict on a wedged pair, as the engine reports it: both carry its error. */
    private fun wedged(engine: FakeTorrentEngine, r: TorrentRepository, vararg keys: String) {
        engine.duplicateErrored.addAll(keys)
        kotlinx.coroutines.runBlocking { r.resolveTwins() }
    }

    @Test
    fun aHybridTorrentAddedByBothOfItsMagnetsBeforeTheirMetadataIsOneRowAfterwards() {
        // Neither had its metadata when the second was added: nothing could know they are one —
        // two entries, and two session torrents.
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=byV1")
        await("the first") { savedKeys().size == 1 }
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v2}&dn=byV2")
        await("two entries while neither has its metadata") { savedKeys().size == 2 }
        val twin = hybrid.engineHash

        // A peer offers the metadata to one of them: the library refuses it and stops both.
        wedged(engine, r, hybrid.v1!!, twin)

        assertEquals("the other's session torrent is let go", listOf(twin), engine.removed)
        assertEquals("the entry added first keeps its torrent, and goes on", listOf(hybrid.v1!!), engine.resumed)
        assertEquals("and nothing is merged without the metadata", 2, savedKeys().size)

        // Alone, it gets its metadata — said by the engine's name for it, which is the other's key.
        engine.learned[hybrid.v1!!] = meta(hybrid.engineHash, hybrid.v1, hybrid.v2)
        kotlinx.coroutines.runBlocking { r.metadataArrived(hybrid.engineHash) }

        await("one row") { savedKeys().size == 1 }
        assertEquals("the entry added first stays", listOf(hybrid.v1), savedKeys())
        assertEquals("the other was asked of the engine once more — and answered with the torrent held", 2, engine.magnets.count { it.contains(hybrid.v2!!) })
        assertTrue("its kept metadata is where the surviving entry looks", keptTorrent(hybrid.v1!!).isFile)
    }

    @Test
    fun aStoppedTorrentTheUserHadPausedStaysPausedAndItsTwinWaitsForIt() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val twin = hybrid.engineHash
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=byV1")
        await("the first") { savedKeys().size == 1 }
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v2}&dn=byV2")
        await("two entries") { savedKeys().size == 2 }
        r.pause(hybrid.v1!!)
        await("paused by hand") { hybrid.v1!! in engine.paused }

        wedged(engine, r, hybrid.v1!!, twin)

        assertEquals("stopped by the library, not resumed over the user's pause", emptyList<String>(), engine.resumed)
        assertEquals(listOf(twin), engine.removed)
        assertEquals("still two entries", 2, savedKeys().size)

        r.resume(hybrid.v1!!)
        await("resumed by hand") { hybrid.v1!! in engine.resumed }
        engine.learned[hybrid.v1!!] = meta(hybrid.engineHash, hybrid.v1, hybrid.v2)
        kotlinx.coroutines.runBlocking { r.metadataArrived(hybrid.engineHash) }

        await("one row") { savedKeys().size == 1 }
        assertEquals(listOf(hybrid.v1), savedKeys())
    }

    @Test
    fun anUnrelatedTorrentStillFetchingItsMetadataIsLeftAlone() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val twin = hybrid.engineHash
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=byV1")
        await("the first") { savedKeys().size == 1 }
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v2}&dn=byV2")
        await("the second") { savedKeys().size == 2 }
        r.addMagnet(v2.siteMagnet + "&dn=stranger")
        await("three entries") { savedKeys().size == 3 }

        wedged(engine, r, hybrid.v1!!, twin)

        assertEquals("only the twin leaves the session", listOf(twin), engine.removed)
        assertEquals(listOf(hybrid.v1!!), engine.resumed)
        assertEquals(3, savedKeys().size)
    }

    @Test
    fun twoWedgedPairsAreResolvedOnePairAtATimeNeverIntoOne() {
        // Two hybrid torrents, each added by both of its magnets, none with metadata: the
        // library stops all four — and the group does not say which two are which.
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val twin = hybrid.engineHash
        val otherV1 = "ab".repeat(20)
        val otherV2 = "cd".repeat(32)
        val otherTwin = otherV2.take(40)
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=one")
        await("the first") { savedKeys().size == 1 }
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v2}&dn=two")
        await("the second") { savedKeys().size == 2 }
        r.addMagnet("magnet:?xt=urn:btih:$otherV1&dn=three")
        await("the third") { savedKeys().size == 3 }
        r.addMagnet("magnet:?xt=urn:btih:$otherV2&dn=four")
        await("four entries") { savedKeys().size == 4 }

        wedged(engine, r, hybrid.v1!!, twin, otherV1, otherTwin)

        assertEquals("the earliest of all goes on alone", listOf(hybrid.v1!!), engine.resumed)
        assertEquals("the other three leave the session", setOf(twin, otherV1, otherTwin), engine.removed.toSet())
        assertEquals("nothing is merged on a guess", 4, savedKeys().size)

        engine.learned[hybrid.v1!!] = meta(hybrid.engineHash, hybrid.v1, hybrid.v2)
        kotlinx.coroutines.runBlocking { r.metadataArrived(hybrid.engineHash) }

        await("the first torrent is one row") { savedKeys().size == 3 }
        assertEquals("the other pair is asked of the engine again — strangers to the first torrent: once at its add, once more now", 4, engine.magnets.count { it.contains(otherV1) || it.contains(otherV2) })

        // They are twins of each other: they wedge, and are resolved the same way.
        wedged(engine, r, otherV1, otherTwin)

        assertEquals(listOf(otherV1), engine.resumed.drop(1))
        assertEquals(otherTwin, engine.removed.last())
        engine.learned[otherV1] = meta(otherTwin, otherV1, otherV2)
        kotlinx.coroutines.runBlocking { r.metadataArrived(otherTwin) }

        await("one row per torrent") { savedKeys().size == 2 }
        assertEquals(listOf(hybrid.v1, otherV1), savedKeys())
    }

    @Test
    fun aStoppedTorrentWhoseTwinIsGoneAlreadyJustGoesOn() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=byV1")
        await("the first") { savedKeys().size == 1 }
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v2}&dn=byV2")
        await("two entries") { savedKeys().size == 2 }
        // The user removed the other one before the library's verdict was acted on.
        r.remove(hybrid.engineHash)
        await("one entry") { savedKeys().size == 1 }

        wedged(engine, r, hybrid.v1!!)

        assertEquals(listOf(hybrid.v1!!), engine.resumed)
        assertEquals("nothing more leaves the session", 1, engine.removed.size)
    }

    @Test
    fun aReportThatFindsNothingStoppedByTheLibraryChangesNothing() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=byV1")
        await("listed") { savedKeys().size == 1 }

        wedged(engine, r)

        assertTrue(engine.removed.isEmpty())
        assertTrue(engine.resumed.isEmpty())
        assertEquals(1, savedKeys().size)
    }

    @Test
    fun aTwinRemovedWhileItWaitsIsNotAskedOfTheEngineAgain() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val twin = hybrid.engineHash
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=byV1")
        await("the first") { savedKeys().size == 1 }
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v2}&dn=byV2")
        await("two entries") { savedKeys().size == 2 }
        wedged(engine, r, hybrid.v1!!, twin)

        r.remove(twin)
        await("one entry") { savedKeys().size == 1 }
        engine.learned[hybrid.v1!!] = meta(hybrid.engineHash, hybrid.v1, hybrid.v2)
        kotlinx.coroutines.runBlocking { r.metadataArrived(hybrid.engineHash) }

        assertEquals("asked of the engine the once, at its add", 1, engine.magnets.count { it.contains(hybrid.v2!!) })
        assertEquals(listOf(hybrid.v1), savedKeys())
    }

    @Test
    fun onEqualAddTimesTheListSFirstEntryStaysWhateverTorrentTheEngineNamed() {
        // A list saved by an earlier version can give several entries the same add time. The
        // entry added first is then the LIST's first — not the one the engine's alert happened
        // to name (the engine names a hybrid torrent by its v2 hash: the other entry's key).
        // The rule is asked only when the merge's input IS that other entry: so both start
        // as magnets the engine cannot tell apart — with a kept file for the first, the merge
        // at the start has folded the pair already, list's first first, and asks nothing.
        // (Found by a mutation that survived this test's first version.)
        fun line(key: String, magnet: String, name: String) =
            """{"h":"$key","m":"$magnet","n":"$name","s":false,"a":7,"p":false,"pa":false}"""
        File(context.filesDir, "torrents_state.json").writeText(
            "[" + line(hybrid.v1!!, "magnet:?xt=urn:btih:${hybrid.v1}&dn=one", "one") + "," +
                line(hybrid.engineHash, "magnet:?xt=urn:btih:${hybrid.v2}&dn=two", "two") + "]",
        )
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("the list is read, restored, and the start's own merge round is over") { engine.snapshots >= 1 }
        assertEquals("two rows while the engine cannot tell", listOf(hybrid.v1, hybrid.engineHash), savedKeys())

        engine.learned[hybrid.v1!!] = meta(hybrid.engineHash, hybrid.v1, hybrid.v2)
        kotlinx.coroutines.runBlocking { r.metadataArrived(hybrid.engineHash) }

        await("one row") { savedKeys().size == 1 }
        assertEquals("the list's first entry stays", listOf(hybrid.v1), savedKeys())
        assertTrue("with the metadata under its own key", keptTorrent(hybrid.v1!!).isFile)
        assertFalse("and nothing left under the other's", keptTorrent(hybrid.engineHash).isFile)
    }

    @Test
    fun theSurvivorIsToldThePeerAddressesTheOthersKnew() {
        // The first magnet carries no peer address, the second does. Kept alone with what it
        // knew, the survivor could never have fetched its metadata — and its twin would have
        // waited for it for good.
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val twin = hybrid.engineHash
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=byV1&tr=udp://192.0.2.1:80/announce")
        await("the first") { savedKeys().size == 1 }
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v2}&dn=byV2&x.pe=192.0.2.11:6882")
        await("two entries") { savedKeys().size == 2 }

        wedged(engine, r, hybrid.v1!!, twin)

        assertEquals("told the other magnet's address", listOf("192.0.2.11" to 6882), engine.peers[hybrid.v1])
    }

    /** Both magnet forms of the reference hybrid torrent, each with a peer address of its own, added in that order. */
    private fun twinsWithPeerAddresses(r: TorrentRepository): String {
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=byV1&x.pe=192.0.2.10:6881")
        await("the first") { savedKeys().size == 1 }
        r.addMagnet("magnet:?xt=urn:btih:${hybrid.v2}&dn=byV2&x.pe=192.0.2.11:6882")
        await("two entries") { savedKeys().size == 2 }
        return hybrid.engineHash
    }

    @Test
    fun aTorrentBroughtBackByAMergeIsToldItsMagnetsPeerAddressesAgain() {
        // A .torrent does not carry a magnet's `x.pe=` addresses. After a merge the survivor
        // came back from its kept file with NO address at all — until the next start — and a
        // trackerless magnet (the emulator's seeder) had nothing to connect to: 0 B for good.
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val twin = twinsWithPeerAddresses(r)
        wedged(engine, r, hybrid.v1!!, twin)
        // The survivor already had its metadata kept under its own key.
        keptTorrent(hybrid.v1!!).apply { parentFile?.mkdirs() }.writeBytes(hybrid.bytes)
        engine.learned[hybrid.v1!!] = meta(hybrid.engineHash, hybrid.v1, hybrid.v2)
        kotlinx.coroutines.runBlocking { r.metadataArrived(hybrid.engineHash) }

        await("one row") { savedKeys().size == 1 }
        assertEquals("told the addresses of BOTH magnets again, each once", setOf("192.0.2.10" to 6881, "192.0.2.11" to 6882), engine.peers[hybrid.v1]?.toSet())
        assertTrue("the survivor's kept file is where its entry looks", keptTorrent(hybrid.v1!!).isFile)
        assertFalse("the other's kept file does not stay behind with no entry", keptTorrent(twin).isFile)
    }

    @Test
    fun aTwinPausedByHandWhileItWaitedComesBackPaused() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val twin = twinsWithPeerAddresses(r)
        wedged(engine, r, hybrid.v1!!, twin)
        r.pause(twin) // no row showed it; the agent can still pause by hash
        await("paused by hand") { twin in engine.paused }

        engine.learned[hybrid.v1!!] = meta(hybrid.engineHash, hybrid.v1, hybrid.v2)
        kotlinx.coroutines.runBlocking { r.metadataArrived(hybrid.engineHash) }

        await("one row") { savedKeys().size == 1 }
        assertEquals("asked of the engine again, and paused right after that", 2, engine.paused.count { it == twin })
    }

    @Test
    fun twinsAreAskedOfTheEngineAgainOnceTheirSurvivorIsRemoved() {
        // They waited for THAT torrent's metadata. With it gone, nothing would ever have
        // brought them back short of a restart.
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val twin = twinsWithPeerAddresses(r)
        wedged(engine, r, hybrid.v1!!, twin)
        assertEquals(1, engine.magnets.count { it.contains(hybrid.v2!!) })

        r.remove(hybrid.v1!!)

        await("one entry left") { savedKeys().size == 1 }
        await("the waiting twin is asked of the engine again") { engine.magnets.count { it.contains(hybrid.v2!!) } == 2 }
        assertEquals(listOf(twin), savedKeys())
    }

    @Test
    fun removingSomeOtherTorrentLeavesWaitingTwinsWhereTheyAre() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        val twin = twinsWithPeerAddresses(r)
        r.addMagnet(v2.siteMagnet + "&dn=stranger")
        await("three entries") { savedKeys().size == 3 }
        wedged(engine, r, hybrid.v1!!, twin)

        r.remove(v2.engineHash)

        await("the stranger is gone") { savedKeys().size == 2 }
        Thread.sleep(400)
        assertEquals("still waiting for THEIR survivor — not put back to wedge again", 1, engine.magnets.count { it.contains(hybrid.v2!!) })
    }

    /** A source file of the app, found from the test's working folder — line ends made one (see TorrentIdentityTest.appSource). */
    private fun appSource(relative: String): String? {
        val anchor = "src/main/java/space/deepflux/android/engine/TorrentRepository.kt"
        var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
        while (dir != null && !File(dir, anchor).isFile) dir = dir.parentFile
        return dir?.let { File(it, relative).takeIf { f -> f.isFile }?.readText()?.replace("\r\n", "\n") }
    }

    @Test
    fun theLibrarySDuplicateErrorIsResolvedAndNotReportedAsAFailure() {
        // No alert can be built on the JVM — the routing is read where it is written.
        val source = appSource("src/main/java/space/deepflux/android/engine/TorrentRepository.kt")!!
        val branch = source.substringAfter("is TorrentErrorAlert -> {").substringBefore("is MetadataReceivedAlert -> {")
        assertTrue("the error's code is what routes it: ${branch.take(200)}", "if (code == TorrentEngineApi.DUPLICATE_TORRENT_ERROR) {" in branch)
        assertTrue("resolved after a settle", "delay(TWINS_SETTLE_MS); resolveTwins()" in branch)
        assertEquals("no failure notice in the duplicate branch", -1, branch.substringAfter("DUPLICATE_TORRENT_ERROR").substringBefore("} else {").indexOf("postEventNotification"))
    }

    @Test
    fun theEngineReadsTheLibrarySOwnCodeForADuplicateTorrent() {
        assertEquals("libtorrent errors::duplicate_torrent — '(19 torrent already exists in session)' on a wedged pair", 19, space.deepflux.android.engine.TorrentEngineApi.DUPLICATE_TORRENT_ERROR)
        val source = appSource("src/main/java/space/deepflux/android/engine/TorrentEngine.kt")!!
        val fun_ = source.substringAfter("override fun hasDuplicateError(").substringBefore("\n    }")
        assertTrue(fun_, "status.errorCode().value == TorrentEngineApi.DUPLICATE_TORRENT_ERROR" in fun_)
        assertTrue("read from the session by the torrent's own key, as a status is", "session.find(Sha1Hash.parseHex(infoHashHex))?.status()" in fun_)
    }

    @Test
    fun decliningAMagnetsOfferAlsoQuietsTheSameTorrentOfferedAsAFile() {
        // The mirror of the other direction: a hybrid torrent's magnet is keyed by its v1
        // hash, its file by the engine's name. Declined once is declined — in both forms.
        val r = repo()
        r.requestAddMagnet("magnet:?xt=urn:btih:${hybrid.v1}&dn=once", sourceHost = "page.example")
        await("the confirmation") { r.pendingAdd.value != null }
        r.cancelPendingAdd()

        r.requestAddTorrentFile(staged(hybrid), sourceHost = "page.example")
        await("the notice is looked at") { r.pendingAdd.value == null }
        Thread.sleep(300)
        assertNull("the same torrent, declined a moment ago, is not offered again as a file", r.pendingAdd.value)
        assertTrue(savedKeys().isEmpty())
    }

    // ------------------------------------------------------------------ restart

    @Test
    fun withoutItsKeptFileAV2OnlyTorrentIsAskedForAgainByTheMagnetThatNamesIt() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        addFile(r, v2)
        await("saved") { savedEntry(v2.engineHash) != null }
        assertTrue(keptTorrent(v2.engineHash).delete())
        r.shutdownForTests()

        val second = FakeTorrentEngine()
        repo(second)
        await("asked for by its whole v2 hash") { second.magnets.contains("magnet:?xt=urn:btih:${v2.v2}") }
    }

    @Test
    fun withItsKeptFileAV2OnlyTorrentComesBackFromItAndIsNotAskedFor() {
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        addFile(r, v2)
        await("kept") { keptTorrent(v2.engineHash).isFile }
        r.shutdownForTests()

        val second = FakeTorrentEngine()
        val r2 = repo(second)
        await("back from the kept file") { second.files.any { it.endsWith("${v2.engineHash}.torrent") } }
        assertTrue("not as a magnet", second.magnets.isEmpty())
        assertEquals(listOf(v2.engineHash), savedKeys())
    }

    @Test
    fun aMagnetThatNamedNoTorrentIsPutRightAtTheStartFromTheKeptFile() {
        // What 1.4.15 saved for a v2-only torrent added from a .torrent file: the key is the
        // engine's 20-byte name, and the "magnet" is that name written as a v1 hash.
        startWith(v2.engineHash, "magnet:?xt=urn:btih:${v2.engineHash}", v2.bytes)
        val r = repo()
        await("the saved magnet names the torrent now") { savedEntry(v2.engineHash)?.first == "magnet:?xt=urn:btih:${v2.v2}" }
        assertEquals(listOf(v2.engineHash), savedKeys())
    }

    @Test
    fun aHybridTorrentSSavedMagnetIsPutRightTheSameWay() {
        startWith(hybrid.engineHash, "magnet:?xt=urn:btih:${hybrid.engineHash}", hybrid.bytes)
        repo()
        await("the saved magnet names the torrent now") { savedEntry(hybrid.engineHash)?.first == "magnet:?xt=urn:btih:${hybrid.v1}" }
    }

    @Test
    fun aSitesMagnetWithItsTrackersIsLeftAsItIs() {
        // A hybrid torrent added from a magnet: keyed by the v1 hash, the magnet as the site
        // wrote it. It names the torrent; the kept file changes nothing about it.
        val siteMagnet = "magnet:?xt=urn:btih:${hybrid.v1}&dn=Show&tr=udp%3A%2F%2Ftracker.example%3A80"
        startWith(hybrid.v1!!, siteMagnet, hybrid.bytes)
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("restored from the kept file") { engine.files.any { it.endsWith(".torrent") } }
        // The step that would "heal" a nameless magnet follows that call in the same coroutine;
        // what any such change would write is on disk well inside this pause.
        Thread.sleep(400)
        assertEquals(siteMagnet, savedEntry(hybrid.v1!!)!!.first)
        assertEquals(hybrid.v1, savedKeys().single())
    }

    @Test
    fun anEntryWhoseKeptFileIsMissingKeepsItsMagnetEvenWhenThatMagnetNamesNothing() {
        // Nothing is in hand to tell the torrent's hashes from — the magnet is left alone
        // rather than replaced by a guess, and the entry is asked for as before.
        startWith(v2.engineHash, "magnet:?xt=urn:btih:${v2.engineHash}", v2.bytes)
        assertTrue(keptTorrent(v2.engineHash).delete())
        val engine = FakeTorrentEngine()
        val r = repo(engine)
        await("asked for by what was saved") { engine.magnets.contains("magnet:?xt=urn:btih:${v2.engineHash}") }
        await("the list is written at least once") { savedState().isNotBlank() }
        assertNotNull(savedEntry(v2.engineHash))
        assertEquals("magnet:?xt=urn:btih:${v2.engineHash}", savedEntry(v2.engineHash)!!.first)
    }
}
