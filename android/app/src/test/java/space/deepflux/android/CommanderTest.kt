package space.deepflux.android

import android.app.Application
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.content.FileProvider
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.filecmd.CommanderRepository
import space.deepflux.android.filecmd.FolderSizer
import java.io.File
import java.nio.file.Files

/**
 * Fix batch 2026-11-23 — the Commander's model: folder sizes are walked off
 * the UI thread (listing used to walk every folder synchronously, and the
 * header re-walked the whole tree during composition), Share / Open with hand
 * out real content URIs (a raw internal path is unreadable by other apps),
 * and nothing outside the app's tree can be deleted.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class CommanderTest {

    private lateinit var context: Context
    private lateinit var appRoot: File
    private lateinit var repo: CommanderRepository

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        appRoot = File(context.filesDir, "DeepFlux").apply { mkdirs() }
        repo = CommanderRepository(context, appRoot)
        // The repository reports refused operations only through android.util.Log.
        previousLogStream = org.robolectric.shadows.ShadowLog.stream
        org.robolectric.shadows.ShadowLog.stream = System.out
    }

    @org.junit.After
    fun tearDown() {
        org.robolectric.shadows.ShadowLog.stream = previousLogStream
    }

    private var previousLogStream: java.io.PrintStream? = null

    private fun file(rel: String, bytes: Int): File =
        File(appRoot, rel).apply { parentFile?.mkdirs(); writeBytes(ByteArray(bytes) { 7 }) }

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 10_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(25)
        }
    }

    // ---------------------------------------------------------------- FolderSizer

    @Test
    fun folderSizerSumsNestedFiles() {
        file("a.bin", 1000)
        file("sub/b.bin", 500)
        file("sub/deep/c.bin", 250)
        assertEquals(750L, FolderSizer.sizeOf(File(appRoot, "sub")))
        assertEquals(1750L, FolderSizer.sizeOf(appRoot))
        assertEquals("a missing folder is empty, not an error", 0L, FolderSizer.sizeOf(File(appRoot, "nope")))
    }

    @Test
    fun folderSizerStopsWhenCancelled() {
        file("sub/b.bin", 500)
        file("sub/deep/c.bin", 250)
        assertEquals("cancelled before the first poll step", 0L, FolderSizer.sizeOf(File(appRoot, "sub")) { true })
        var visited = 0
        FolderSizer.forEachFile(File(appRoot, "sub"), cancelled = { visited > 0 }) { visited++ }
        assertEquals("the callback runs until the caller says stop", 1, visited)
    }

    @Test
    fun folderSizerDoesNotFollowSymbolicLinks() {
        // Under the root: real/x.bin (300), elsewhere/y.bin (9000), link -> real,
        // outside -> a folder that is NOT under the root (7000), loop -> the root itself.
        // No link followed: 300 + 9000. Following `link` would count x.bin twice (9600),
        // following `outside` would add 7000, following `loop` would never end.
        // Until 2026-11-26 this expected 300 — a value that layout cannot produce — and
        // nobody knew: the test skips itself on a Windows host, and no CI ran it. The
        // first Linux CI run failed it (9300, which is the right answer).
        file("real/x.bin", 300)
        file("elsewhere/y.bin", 9000)
        val outside = File(context.cacheDir, "not-under-the-root").apply { mkdirs() }
        File(outside, "z.bin").writeBytes(ByteArray(7000) { 7 })
        val created = runCatching {
            Files.createSymbolicLink(File(appRoot, "link").toPath(), File(appRoot, "real").toPath())
            Files.createSymbolicLink(File(appRoot, "outside").toPath(), outside.toPath())
            Files.createSymbolicLink(File(appRoot, "loop").toPath(), appRoot.toPath())
        }
        // A Windows dev shell has no privilege to create symlinks: that is a
        // SKIP, reported as one — it used to return silently and count as a pass.
        org.junit.Assume.assumeTrue("this machine cannot create symbolic links: ${created.exceptionOrNull()}", created.isSuccess)
        assertEquals("the file behind the link is counted once; no link is followed", 9300L, FolderSizer.sizeOf(appRoot))
    }

    /** The same walk with the link test substituted: runs where the test above is skipped. */
    @Test
    fun folderSizerLeavesWhateverTheLinkTestFlagsAlone() {
        file("real/x.bin", 300)
        file("linked/y.bin", 9000)
        assertEquals("the flagged folder is never entered", 300L, FolderSizer.sizeOf(appRoot, isLink = { it.name == "linked" }))
        assertEquals("and a real folder is", 9300L, FolderSizer.sizeOf(appRoot))
    }

    @Test
    fun aPickedFolderIsNamedAfterTheFolderNotItsEncodedId() {
        fun name(uri: String) = CommanderRepository.pickedFolderName(Uri.parse(uri))
        // The tree uri's last segment is an encoded document id; that string used to be the root's name.
        assertEquals("Movies", name("content://com.android.externalstorage.documents/tree/primary%3ADownload%2FMovies"))
        assertEquals("Download", name("content://com.android.externalstorage.documents/tree/primary%3ADownload"))
        assertEquals("primary", name("content://com.android.externalstorage.documents/tree/primary%3A"))
        assertEquals("an opaque id stays as it is", "1234", name("content://com.android.externalstorage.documents/tree/msf%3A1234"))
        assertEquals("an id that is only separators", "folder", name("content://com.android.externalstorage.documents/tree/%3A"))
        // `lastPathSegment` is decoded once by Uri. Whatever "%" sequences
        // are left belong to the id as the provider wrote it — decoding them
        // AGAIN would turn "%20" into a space the folder does not have.
        assertEquals("100%20done", name("content://com.android.externalstorage.documents/tree/primary%3ADownload%2F100%2520done"))
    }

    // ---------------------------------------------------------------- listing + sizes

    @Test
    fun listingNeverWalksFoldersSizesArriveOffThread() {
        file("sub/b.bin", 500)
        file("sub/deep/c.bin", 250)
        val t0 = System.nanoTime()
        repo.open(CommanderRepository.Root.APP_STORAGE)
        val listingMs = (System.nanoTime() - t0) / 1_000_000
        assertTrue("listing is a plain listFiles (took ${listingMs}ms)", listingMs < 2_000)

        val sub = repo.listing.value.entries.first { it.isDirectory }
        // The listing itself carries no folder size (it used to walk the tree
        // on the UI thread to fill this in).
        assertEquals(-1L, sub.size)

        // The size is delivered by the background walker.
        await("the off-thread size") { repo.dirSizes.value[sub.path] != null }
        assertEquals(750L, repo.dirSizes.value[sub.path])
    }

    @Test
    fun storageTotalArrivesOffThreadAndRefreshesAfterADelete() {
        file("sub/b.bin", 500)
        file("top.bin", 200)
        assertNull(repo.storageUsed.value)
        repo.refreshStorageUsed()
        await("the storage total") { repo.storageUsed.value != null }
        assertEquals(700L, repo.storageUsed.value)

        assertTrue(repo.delete(entry(File(appRoot, "top.bin"))))
        assertFalse(File(appRoot, "top.bin").exists())
        await("the refreshed total") { repo.storageUsed.value == 500L }
    }

    // ---------------------------------------------------------------- share / open with

    private fun entry(f: File) = CommanderRepository.Entry(
        name = f.name, isDirectory = false, size = f.length(), modified = 0,
        source = CommanderRepository.Source.APP, path = f.absolutePath, mime = "video/mp4",
    )

    /** A repository whose "provider" records what it is asked for and answers with a recognisable uri. */
    private class FakeProvider {
        val asked = mutableListOf<File>()
        val uri = Uri.parse("content://test.provider/movie.mp4")
        fun repository(context: Context, appRoot: File) =
            CommanderRepository(context, appRoot, appFileUri = { asked.add(it); uri })
    }

    @Suppress("DEPRECATION")
    @Test
    fun shareAsksTheProviderForAppFilesInsteadOfHandingOutThePath() {
        val f = file("movie.mp4", 4096)
        val provider = FakeProvider()
        val chooser = provider.repository(context, appRoot).shareIntent(entry(f))!!

        assertEquals("the provider was asked for exactly this file", listOf(f), provider.asked)
        val send = chooser.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)!!
        assertEquals(provider.uri, send.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))
        assertEquals(Intent.ACTION_SEND, send.action)
        assertEquals("video/mp4", send.type)
        assertTrue("the receiver gets read access", send.flags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0)
    }

    @Suppress("DEPRECATION")
    @Test
    fun openWithUsesTheProviderUriToo() {
        val f = file("notes.pdf", 10)
        val provider = FakeProvider()
        val open = provider.repository(context, appRoot).openWithIntent(entry(f).copy(mime = "application/pdf"))!!

        assertEquals(listOf(f), provider.asked)
        assertEquals(provider.uri, open.data)
        assertEquals(Intent.ACTION_VIEW, open.action)
        assertEquals("application/pdf", open.type)
        assertTrue(open.flags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0)
    }

    @Test
    fun publicAndChosenFolderEntriesKeepTheirOwnContentUris() {
        val provider = FakeProvider()
        val r = provider.repository(context, appRoot)
        val public = CommanderRepository.Entry(
            "x.mp4", false, 1, 0, CommanderRepository.Source.MEDIASTORE,
            "content://media/external/downloads/5", "video/mp4",
        )
        assertEquals(Uri.parse(public.path), r.openWithIntent(public)!!.data)
        val send = r.shareIntent(public)!!.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)!!
        assertEquals(Uri.parse(public.path), send.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))
        assertTrue("never the app-file provider", provider.asked.isEmpty())
    }

    @Suppress("DEPRECATION")
    @Test
    fun realProviderRoundTrip() {
        // androidx.core's FileProvider decides membership with
        // `path.startsWith(root + "/")` — a POSIX-only check that can never
        // match on a Windows host, where canonical paths use "\". The app's
        // own logic is covered above; this is the real provider, on the
        // hosts where it can run (the Linux CI runner).
        org.junit.Assume.assumeTrue("FileProvider needs POSIX path separators", File.separatorChar == '/')
        val f = file("movie.mp4", 4096)
        val send = CommanderRepository(context, appRoot).shareIntent(entry(f))!!
            .getParcelableExtra<Intent>(Intent.EXTRA_INTENT)!!
        val uri = send.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)!!
        assertTrue("got $uri", uri.toString().startsWith("content://"))
        assertEquals(FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", f), uri)
        // What the receiving app does with it:
        assertArrayEquals(f.readBytes(), context.contentResolver.openInputStream(uri)!!.use { it.readBytes() })
    }

    // ---------------------------------------------------------------- guard

    @Test
    fun nothingOutsideTheAppTreeIsDeletedOrRenamed() {
        val outside = File(context.filesDir, "outside.bin").apply { writeBytes(ByteArray(5)) }
        val sneaky = CommanderRepository.Entry(
            "outside.bin", false, 5, 0, CommanderRepository.Source.APP, outside.absolutePath,
        )
        assertFalse(repo.delete(sneaky))
        assertFalse(repo.rename(sneaky, "renamed.bin"))
        assertTrue("untouched", outside.isFile)
    }

    /**
     * Second security review (2026-11-24): a torrent can plant a symbolic
     * link inside the download folder, pointing at any file of the app's
     * private storage. Listed, such a link could be shared or opened as if
     * it were a downloaded video — and "deleting" a folder that contained a
     * linked sub-folder walked INTO its target. The link test is
     * substituted: a Windows host cannot create real symlinks.
     */
    @Test
    fun symbolicLinksAreNeitherListedNorFollowedWhenDeleting() {
        file("folder/keep.bin", 10)
        file("link-dir/inner.bin", 10) // stands for a linked FOLDER: its content lives at the target
        file("link-file", 10) // stands for a linked FILE
        val linked = CommanderRepository(context, appRoot, isLink = { it.name.startsWith("link-") })
        linked.open(CommanderRepository.Root.APP_STORAGE)
        assertEquals(listOf("folder"), linked.listing.value.entries.map { it.name })

        val linkDir = File(appRoot, "link-dir")
        assertFalse("a link is refused by its path too", linked.delete(entry(linkDir)))
        assertTrue("…and what it points at is untouched", File(linkDir, "inner.bin").isFile)

        assertTrue(linked.delete(entry(File(appRoot, "folder"))))
        assertFalse("the folder itself is gone", File(appRoot, "folder").exists())
        assertTrue("deleting never goes through a link", File(linkDir, "inner.bin").isFile)
    }

    /**
     * The same with a REAL symbolic link inside the folder — what
     * deleteRecursively() used to walk into. Only on hosts that can create
     * symlinks (the Linux CI runner); reported as skipped elsewhere.
     */
    @Test
    fun deletingAFolderRemovesALinkInsideItButNeverWhatItPointsAt() {
        file("box/keep.bin", 10)
        val target = File(context.filesDir, "elsewhere").apply { mkdirs() }
        val secret = File(target, "secret.bin").apply { writeBytes(ByteArray(9)) }
        val link = File(File(appRoot, "box"), "link-sub")
        val created = runCatching { Files.createSymbolicLink(link.toPath(), target.toPath()) }
        org.junit.Assume.assumeTrue("this machine cannot create symbolic links: ${created.exceptionOrNull()}", created.isSuccess)

        assertTrue(repo.delete(entry(File(appRoot, "box"))))
        assertFalse(File(appRoot, "box").exists())
        assertTrue("the link's target is untouched", secret.isFile)
    }
}
