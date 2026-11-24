package space.deepflux.android

import android.app.Application
import android.content.Context
import android.content.Intent
import android.content.pm.ProviderInfo
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract
import android.provider.DocumentsProvider
import android.provider.MediaStore
import android.webkit.MimeTypeMap
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowContentResolver
import space.deepflux.android.dlmgr.FinishedExport
import space.deepflux.android.dlmgr.HttpDownloadRepository
import space.deepflux.android.dlmgr.HttpState
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.ui.Strings
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * Settings → "Save location" (owner, 2026-11-24: "make it work"). The folder
 * the user picks is where finished downloads are copied to; it used to be
 * remembered and never read.
 *
 * The copy goes through the Storage Access Framework. To run that code on the
 * JVM, [TestFolderProvider] is a real [DocumentsProvider] over a temp folder,
 * registered with Robolectric — the `DocumentsContract` calls are the
 * production ones, not a fake of them.
 *
 * Robolectric keeps the settings file, the granted-permission list and the
 * recorded inserts for the WHOLE test run: every test here starts from a
 * known state (see [setUp]) and leaves the default behind.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class FinishedExportTest {

    /** A documents provider over one temp folder. Document ids: "root", or the path below it ("Season 1/ep1.mkv"). */
    class TestFolderProvider : DocumentsProvider() {
        private val columns = arrayOf(
            DocumentsContract.Document.COLUMN_DOCUMENT_ID,
            DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_MIME_TYPE,
            DocumentsContract.Document.COLUMN_SIZE,
        )

        private fun fileOf(id: String): File = if (id == ROOT) folder else File(folder, id)
        private fun idOf(file: File): String = if (file == folder) ROOT else file.relativeTo(folder).path.replace(File.separatorChar, '/')

        override fun onCreate(): Boolean = true

        /** One row, in the caller's column order — as every real provider answers. */
        private fun row(cursor: MatrixCursor, file: File, projection: Array<out String>) {
            val dir = file.isDirectory
            val r = cursor.newRow()
            for (column in projection) {
                r.add(
                    when (column) {
                        DocumentsContract.Document.COLUMN_DOCUMENT_ID -> idOf(file)
                        DocumentsContract.Document.COLUMN_DISPLAY_NAME -> file.name
                        DocumentsContract.Document.COLUMN_MIME_TYPE ->
                            if (dir) DocumentsContract.Document.MIME_TYPE_DIR else "application/octet-stream"
                        DocumentsContract.Document.COLUMN_SIZE -> if (dir) null else file.length()
                        else -> null
                    },
                )
            }
        }

        /** A different order from the one the app asks for (size, name, id, type). */
        private val ownOrder = arrayOf(
            DocumentsContract.Document.COLUMN_SIZE,
            DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_DOCUMENT_ID,
            DocumentsContract.Document.COLUMN_MIME_TYPE,
        )

        private fun answer(projection: Array<out String>?, fill: (MatrixCursor, Array<out String>) -> Unit): Cursor {
            val wanted = if (answerInOwnOrder) ownOrder else projection ?: columns
            return MatrixCursor(wanted).also { fill(it, wanted) }
        }

        override fun queryRoots(projection: Array<out String>?): Cursor =
            answer(projection) { c, p -> row(c, folder, p) }

        override fun queryDocument(documentId: String?, projection: Array<out String>?): Cursor =
            answer(projection) { c, p -> row(c, fileOf(documentId!!), p) }

        override fun queryChildDocuments(parentDocumentId: String?, projection: Array<out String>?, sortOrder: String?): Cursor {
            listings.incrementAndGet()
            return answer(projection) { c, p ->
                fileOf(parentDocumentId!!).listFiles()?.sortedBy { it.name }?.filter { it.name !in hiddenFolders }?.forEach { row(c, it, p) }
            }
        }

        /** Any descendant counts — the framework checks new documents against this. */
        override fun isChildDocument(parentDocumentId: String?, documentId: String?): Boolean {
            val ancestor = fileOf(parentDocumentId!!).canonicalFile
            var candidate = fileOf(documentId!!).canonicalFile
            while (candidate != null) {
                if (candidate == ancestor) return true
                candidate = candidate.parentFile
            }
            return false
        }

        override fun openDocument(documentId: String?, mode: String?, signal: CancellationSignal?): ParcelFileDescriptor {
            opens.incrementAndGet()
            val file = fileOf(documentId!!)
            if (file.name == refuseToOpen) {
                // As a provider whose storage went away does; the app sees this one.
                throw java.io.FileNotFoundException("the folder's storage is gone")
            }
            return ParcelFileDescriptor.open(file, ParcelFileDescriptor.parseMode(mode))
        }

        override fun createDocument(parentDocumentId: String?, mimeType: String?, displayName: String?): String? {
            val dir = mimeType == DocumentsContract.Document.MIME_TYPE_DIR
            val target0 = File(fileOf(parentDocumentId!!), displayName!!)
            if (dir && fileInsteadOfFolder) {
                // A file of that name appeared where the folder was asked for,
                // and the provider refuses the folder over it.
                target0.writeText("someone's file")
                throw IllegalArgumentException("Already exists ${target0.path}")
            }
            if (!dir && reuseExistingOnCreate && target0.isFile) {
                // A provider that never picks another name: it answers with
                // the file that is already there.
                lastMime = mimeType
                return idOf(target0)
            }
            created.incrementAndGet()
            var target = target0
            if (target.exists()) {
                // As the system's own providers do: a free "name (n).ext".
                val base = displayName.substringBeforeLast('.')
                val ext = displayName.substringAfterLast('.', "")
                var n = 1
                while (target.exists()) {
                    n++
                    target = File(target.parentFile, if (ext.isEmpty()) "$base ($n)" else "$base ($n).$ext")
                }
            }
            val ok = if (dir) target.mkdir() else target.createNewFile()
            if (ok && dir && hideNewFolders) hiddenFolders.add(target.name)
            if (ok && dir && folderMadeMeanwhile) {
                // As a real provider answers when the folder appeared between
                // the app's look-up and its request.
                throw IllegalArgumentException("Already exists ${target.path}")
            }
            if (ok && !dir) lastMime = mimeType
            return if (ok) idOf(target) else null
        }

        override fun deleteDocument(documentId: String?) {
            deleted.incrementAndGet()
            fileOf(documentId!!).deleteRecursively()
        }

        companion object {
            const val AUTHORITY = "space.deepflux.android.testfolder"
            const val ROOT = "root"
            lateinit var folder: File
            /** Documents of this name are made but not opened for writing. */
            @Volatile var refuseToOpen: String? = null
            /** Answers every query in its own column order, ignoring the one asked for. */
            @Volatile var answerInOwnOrder: Boolean = false
            /** Makes a folder, then refuses it as "already there" — the lost race. */
            @Volatile var folderMadeMeanwhile: Boolean = false
            /** Answers a create request with the file of that name that is already there. */
            @Volatile var reuseExistingOnCreate: Boolean = false
            /** Puts a FILE where a folder was asked for and refuses the folder. */
            @Volatile var fileInsteadOfFolder: Boolean = false
            /** Makes folders, but does not list them yet (a provider that lags). */
            @Volatile var hideNewFolders: Boolean = false
            val hiddenFolders: MutableSet<String> = java.util.concurrent.ConcurrentHashMap.newKeySet()
            /** The type the last file was asked to be created with. */
            @Volatile var lastMime: String? = null
            val created = AtomicInteger()
            val deleted = AtomicInteger()
            val listings = AtomicInteger()
            val opens = AtomicInteger()
        }
    }

    private lateinit var context: Context
    private lateinit var folder: File
    private lateinit var sources: File
    private lateinit var server: TinyHttpServer
    private val repos = mutableListOf<TorrentRepository>()
    private val collectors = mutableListOf<Job>()
    private val treeUri: Uri get() = DocumentsContract.buildTreeDocumentUri(TestFolderProvider.AUTHORITY, TestFolderProvider.ROOT)
    private val allowed: (Uri) -> Boolean = { true }

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        folder = File(context.cacheDir, "tree").apply { mkdirs() }
        TestFolderProvider.folder = folder
        TestFolderProvider.refuseToOpen = null
        TestFolderProvider.answerInOwnOrder = false
        TestFolderProvider.folderMadeMeanwhile = false
        TestFolderProvider.reuseExistingOnCreate = false
        TestFolderProvider.fileInsteadOfFolder = false
        TestFolderProvider.hideNewFolders = false
        TestFolderProvider.hiddenFolders.clear()
        TestFolderProvider.lastMime = null
        TestFolderProvider.created.set(0)
        TestFolderProvider.deleted.set(0)
        TestFolderProvider.listings.set(0)
        TestFolderProvider.opens.set(0)
        shadowOf(MimeTypeMap.getSingleton()).clearMappings()
        val provider = TestFolderProvider()
        provider.attachInfo(
            context,
            ProviderInfo().apply {
                authority = TestFolderProvider.AUTHORITY
                exported = true
                grantUriPermissions = true
                packageName = context.packageName
                // As every real documents provider is declared; the framework refuses any other.
                readPermission = android.Manifest.permission.MANAGE_DOCUMENTS
                writePermission = android.Manifest.permission.MANAGE_DOCUMENTS
            },
        )
        // …and the calling side is allowed through, as the system's own UI is.
        org.robolectric.shadows.ShadowInstrumentation::class.java
            .getDeclaredMethod("grantPermissions", Array<String>::class.java)
            .apply { isAccessible = true }
            .invoke(
                shadowOf(androidx.test.platform.app.InstrumentationRegistry.getInstrumentation()),
                arrayOf(android.Manifest.permission.MANAGE_DOCUMENTS),
            )
        ShadowContentResolver.registerProviderInternal(TestFolderProvider.AUTHORITY, provider)
        // The shadow keeps granted permissions in a static list; start each test with none.
        runCatching {
            context.contentResolver.releasePersistableUriPermission(
                treeUri, Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
            )
        }
        assertFalse("no folder permission is left over", FinishedExport.canWriteTo(context, treeUri))
        sources = File(context.cacheDir, "src").apply { mkdirs() }
        server = TinyHttpServer()
    }

    @After
    fun tearDown() {
        server.close()
        // A test that failed half-way must not leave its folder choice to the next class.
        repos.lastOrNull()?.let { r -> runCatching { backToDefault(r) } }
        collectors.forEach { it.cancel() }
        repos.forEach { it.shutdownForTests() }
    }

    private fun source(name: String, bytes: Int) = File(sources, name).apply { writeBytes(ByteArray(bytes) { (it % 7).toByte() }) }

    private fun inFolder(relative: String): File? = File(folder, relative).takeIf { it.exists() }

    private fun namesIn(dir: File): List<String> = dir.listFiles()?.map { f -> f.name }?.sorted().orEmpty()

    private fun newRepo(): TorrentRepository =
        TorrentRepository(RecordingContext(context), FakeTorrentEngine()).also { repos += it }

    /** Starts collecting the repository's snackbar notices BEFORE anything can emit. */
    private fun noticesOf(repo: TorrentRepository): MutableList<String> {
        val seen = CopyOnWriteArrayList<String>()
        collectors += CoroutineScope(Dispatchers.Unconfined).launch { repo.notices.collect { seen.add(it) } }
        return seen
    }

    /** As when the user picks the folder: the app's access to it is kept across launches. */
    private fun keepFolderForThisTest() {
        context.contentResolver.takePersistableUriPermission(
            treeUri, Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
        )
    }

    /** Robolectric starts with runtime permissions ungranted, and the shadow's grantor is package-private. */
    private fun grantPostNotifications() {
        org.robolectric.shadows.ShadowInstrumentation::class.java
            .getDeclaredMethod("grantPermissions", Array<String>::class.java)
            .apply { isAccessible = true }
            .invoke(
                shadowOf(androidx.test.platform.app.InstrumentationRegistry.getInstrumentation()),
                arrayOf(android.Manifest.permission.POST_NOTIFICATIONS),
            )
    }

    private fun backToDefault(repo: TorrentRepository) {
        repo.clearCustomSaveDir()
        await("the public Downloads folder being the destination") { repo.finishedDownloadFolder() == "" }
    }

    /** The file names asked of the public Downloads collection so far in this run. */
    private fun downloadsAskedFor(): List<String> = shadowOf(context.contentResolver).insertStatements
        .filter { it.uri.authority == MediaStore.AUTHORITY }
        .mapNotNull { it.contentValues.getAsString(MediaStore.Downloads.DISPLAY_NAME) }

    /** [FinishedExport.Result.Failed] prints nothing useful by itself — a failing assertion shows its reason. */
    private fun assertCopied(what: String, result: FinishedExport.Result?) {
        val reason = (result as? FinishedExport.Result.Failed)?.let { it.reason } ?: result.toString()
        assertEquals("$what did not happen: $reason", FinishedExport.Result.Copied, result)
    }

    private fun failedReason(result: FinishedExport.Result?): String {
        assertTrue("it failed, but with $result", result is FinishedExport.Result.Failed)
        return (result as FinishedExport.Result.Failed).reason
    }

    // ---------------------------------------------------------------- the copy into the chosen folder

    @Test
    fun aFinishedFileIsCopiedIntoTheChosenFolder() {
        val src = source("movie.mkv", 300_000)
        assertCopied("the copy", FinishedExport.export(context, src, treeUri.toString(), access = allowed))
        val copy = inFolder("movie.mkv")!!
        assertArrayEquals(src.readBytes(), copy.readBytes())
        assertTrue("the download itself stays where the app can play it", src.isFile)
    }

    @Test
    fun aTorrentsOwnSubFoldersAreKept() {
        val src = source("ep1.mkv", 1000)
        assertCopied(
            "the copy into the torrent's own folder",
            FinishedExport.export(context, src, treeUri.toString(), relativePath = "Season 1/ep1.mkv", access = allowed),
        )
        assertArrayEquals(src.readBytes(), inFolder("Season 1/ep1.mkv")!!.readBytes())
    }

    @Test
    fun aRelativePathNeverLeavesTheChosenFolder() {
        val src = source("ep1.mkv", 1000)
        assertCopied(
            "the copy with a climbing path",
            FinishedExport.export(context, src, treeUri.toString(), relativePath = "../../outside.mkv", access = allowed),
        )
        assertTrue(inFolder("outside.mkv")!!.isFile)
        assertFalse(File(folder.parentFile, "outside.mkv").exists())
        assertEquals(listOf("outside.mkv"), namesIn(folder))
    }

    @Test
    fun theSameFileIsNotCopiedTwice() {
        val src = source("movie.mkv", 5000)
        assertCopied("the first copy", FinishedExport.export(context, src, treeUri.toString(), access = allowed))
        val copy = inFolder("movie.mkv")!!
        copy.setLastModified(1_000_000L)
        val again = FinishedExport.export(context, src, treeUri.toString(), access = allowed)
        assertEquals("the second export: $again", FinishedExport.Result.AlreadyThere, again)
        assertEquals("left exactly as it was", 1_000_000L, copy.lastModified())
        assertEquals("no document was created for the second export", 1, TestFolderProvider.created.get())
    }

    @Test
    fun aDifferentFileOfTheSameNameDoesNotReplaceTheOldOne() {
        File(folder, "movie.mkv").writeBytes(byteArrayOf(1, 2, 3))
        val src = source("movie.mkv", 5000)
        assertCopied("the copy beside an older file", FinishedExport.export(context, src, treeUri.toString(), access = allowed))
        assertArrayEquals("the earlier file is untouched", byteArrayOf(1, 2, 3), inFolder("movie.mkv")!!.readBytes())
        assertArrayEquals(src.readBytes(), inFolder("movie (2).mkv")!!.readBytes())
    }

    @Test
    fun aDifferentFileOfTheSameNameAndSizeInTheChosenFolderGoesBesideIt() {
        // Told apart by name and size alone, the finished download went nowhere.
        val other = ByteArray(500) { 9 }
        File(folder, "movie.mkv").writeBytes(other)
        val src = source("movie.mkv", 500)
        assertCopied("the copy beside a different file", FinishedExport.export(context, src, treeUri.toString(), access = allowed))
        assertArrayEquals("the other file is untouched", other, inFolder("movie.mkv")!!.readBytes())
        assertArrayEquals(src.readBytes(), inFolder("movie (2).mkv")!!.readBytes())
    }

    @Test
    fun aProviderThatHandsBackTheOldFileHasItLeftAlone() {
        // Some providers never pick another name: the create request comes
        // back with the file that is already there. That file is the user's.
        File(folder, "movie.mkv").writeBytes(byteArrayOf(1, 2, 3))
        TestFolderProvider.reuseExistingOnCreate = true
        val src = source("movie.mkv", 5000)
        val result = FinishedExport.export(context, src, treeUri.toString(), access = allowed)
        assertTrue("reported: $result", result is FinishedExport.Result.Failed)
        assertArrayEquals("not emptied by writing over it", byteArrayOf(1, 2, 3), inFolder("movie.mkv")!!.readBytes())
        assertEquals("not opened for writing", 0, TestFolderProvider.opens.get())
        assertEquals("not deleted as a failed copy", 0, TestFolderProvider.deleted.get())
        assertEquals(listOf("movie.mkv"), namesIn(folder))
    }

    @Test
    fun aFailedCopyLeavesNoHalfWrittenFileBehind() {
        val src = source("broken.mkv", 5000)
        TestFolderProvider.refuseToOpen = "broken.mkv"
        val result = FinishedExport.export(context, src, treeUri.toString(), access = allowed)
        assertTrue("it is the opening that failed", failedReason(result).contains("storage is gone"))
        assertEquals(
            "the file was made, then taken away again",
            1 to 1,
            TestFolderProvider.created.get() to TestFolderProvider.deleted.get(),
        )
        assertEquals(emptyList<String>(), namesIn(folder))
    }

    @Test
    fun aFileWhereAFolderShouldGoIsReportedAndNothingElseIsTouched() {
        File(folder, "Season 1").writeText("a file, not a folder")
        val src = source("ep1.mkv", 1000)
        val result = FinishedExport.export(context, src, treeUri.toString(), relativePath = "Season 1/ep1.mkv", access = allowed)
        assertTrue(failedReason(result), failedReason(result).contains("in the way"))
        assertEquals("nothing was created", 0, TestFolderProvider.created.get())
        assertEquals(listOf("Season 1"), namesIn(folder))
    }

    @Test
    fun aFileThatAppearsWhereAFolderShouldGoIsNotUsedAsTheFolder() {
        // Between the app's look-up and its request: the provider refuses the
        // folder because a FILE of that name is there now.
        TestFolderProvider.fileInsteadOfFolder = true
        val src = source("ep1.mkv", 1000)
        val result = FinishedExport.export(context, src, treeUri.toString(), relativePath = "Season 1/ep1.mkv", access = allowed)
        assertTrue(failedReason(result), failedReason(result).contains("in the way"))
        assertEquals("that file is not written into", "someone's file", inFolder("Season 1")!!.readText())
        assertEquals("and not deleted", 0, TestFolderProvider.deleted.get())
        assertEquals(listOf("Season 1"), namesIn(folder))
    }

    @Test
    fun anOldFileTheJobDidNotSeeIsStillLeftAlone() {
        // The job's memory of this folder is from before the user's file was
        // there. The document that comes back is asked about itself.
        val run = FinishedExport.Run()
        assertCopied("a file beside it", FinishedExport.export(context, source("warm.txt", 10), treeUri.toString(), access = allowed, run = run))
        File(folder, "movie.mkv").writeBytes(byteArrayOf(1, 2, 3))
        TestFolderProvider.reuseExistingOnCreate = true
        val result = FinishedExport.export(context, source("movie.mkv", 5000), treeUri.toString(), access = allowed, run = run)
        assertTrue("reported: $result", result is FinishedExport.Result.Failed)
        assertArrayEquals("not emptied by writing over it", byteArrayOf(1, 2, 3), inFolder("movie.mkv")!!.readBytes())
        assertEquals("only the harmless file was ever opened for writing", 1, TestFolderProvider.opens.get())
        assertEquals("nothing deleted", 0, TestFolderProvider.deleted.get())
        assertEquals(listOf("movie.mkv", "warm.txt"), namesIn(folder))
    }

    @Test
    fun aFolderTheProviderDoesNotListYetIsNotMadeTwice() {
        TestFolderProvider.hideNewFolders = true
        val run = FinishedExport.Run()
        for (name in listOf("a.mkv", "b.mkv")) {
            assertCopied(
                "the copy of $name",
                FinishedExport.export(context, source(name, 500), treeUri.toString(), relativePath = "Album/$name", access = allowed, run = run),
            )
        }
        assertEquals(listOf("Album"), namesIn(folder))
        assertEquals(listOf("a.mkv", "b.mkv"), namesIn(File(folder, "Album")))
    }

    @Test
    fun eachFolderIsListedOncePerJobNotOncePerFile() {
        val run = FinishedExport.Run()
        for (name in listOf("a.mkv", "b.mkv", "c.mkv")) {
            assertCopied("the copy of $name", FinishedExport.export(context, source(name, 500), treeUri.toString(), relativePath = "Album/$name", access = allowed, run = run))
        }
        // The root before and after its "Album" folder was made, then "Album" itself.
        assertEquals(3, TestFolderProvider.listings.get())
        assertEquals(listOf("Album"), namesIn(folder))
        assertEquals(listOf("a.mkv", "b.mkv", "c.mkv"), namesIn(File(folder, "Album")))
    }

    @Test
    fun theColumnsAreReadByNameWhateverOrderTheProviderAnswers() {
        val src = source("ep1.mkv", 1000)
        TestFolderProvider.answerInOwnOrder = true
        assertCopied(
            "the first copy",
            FinishedExport.export(context, src, treeUri.toString(), relativePath = "Season 1/ep1.mkv", access = allowed),
        )
        val again = FinishedExport.export(context, src, treeUri.toString(), relativePath = "Season 1/ep1.mkv", access = allowed)
        assertEquals("the second export: $again", FinishedExport.Result.AlreadyThere, again)
    }

    @Test
    fun aFolderThatAppearedMeanwhileIsUsedNotFoughtOver() {
        val src = source("ep1.mkv", 1000)
        TestFolderProvider.folderMadeMeanwhile = true
        // Within one job: the job already looked at this folder and saw no
        // "Season 1" — it must look again, not trust its memory.
        val run = FinishedExport.Run()
        assertCopied("a file beside it", FinishedExport.export(context, source("warm.txt", 10), treeUri.toString(), access = allowed, run = run))
        assertCopied(
            "the copy",
            FinishedExport.export(context, src, treeUri.toString(), relativePath = "Season 1/ep1.mkv", access = allowed, run = run),
        )
        assertArrayEquals(src.readBytes(), inFolder("Season 1/ep1.mkv")!!.readBytes())
        assertEquals(listOf("Season 1", "warm.txt"), namesIn(folder))
    }

    @Test
    fun aTorrentListsEachFolderOnce() {
        keepFolderForThisTest()
        val repo = newRepo()
        backToDefault(repo)
        repo.pickedFinishedDownloadsFolder(treeUri, kept = true)
        await("the stored choice") { repo.finishedDownloadFolder() == treeUri.toString() }
        val downloadFolder = downloaded("Album/a.mkv", "Album/b.mkv", "Album/c.mkv")
        assertEquals(0, repo.exportFinishedFiles(downloadFolder, listOf("Album/a.mkv", "Album/b.mkv", "Album/c.mkv")))
        // The root before and after its "Album" folder was made, then "Album" itself.
        assertEquals(3, TestFolderProvider.listings.get())
        assertEquals(listOf("a.mkv", "b.mkv", "c.mkv"), namesIn(File(folder, "Album")))
        backToDefault(repo)
    }

    @Test
    fun filesAreCreatedWithTheTypeTheirOwnExtensionSays() {
        // A system folder provider keeps a name only when the type agrees
        // with the extension: "notes.pdf" asked for as "application/octet-stream"
        // comes out as "notes.pdf.bin".
        shadowOf(MimeTypeMap.getSingleton()).addExtensionMimeTypeMapping("pdf", "application/pdf")
        assertCopied("the pdf", FinishedExport.export(context, source("notes.pdf", 100), treeUri.toString(), access = allowed))
        assertEquals("application/pdf", TestFolderProvider.lastMime)
        assertCopied("the clip", FinishedExport.export(context, source("clip.avi", 100), treeUri.toString(), access = allowed))
        assertEquals("video/x-msvideo", TestFolderProvider.lastMime)
        assertEquals("an unknown extension keeps the generic type", "application/octet-stream", FinishedExport.mimeFor("archive.zq9"))
    }

    @Test
    fun copiedBytesGrowByExactlyWhatWasCopied() {
        val src = source("movie.mkv", 700_000) // more than one copy buffer
        val before = FinishedExport.copiedBytes()
        assertCopied("the copy", FinishedExport.export(context, src, treeUri.toString(), access = allowed))
        assertEquals(src.length().toLong(), FinishedExport.copiedBytes() - before)
    }

    // ---------------------------------------------------------------- which folder, really

    @Test
    fun aFolderTheAppCanNoLongerOpenMeansTheDownloadsFolder() {
        // The system or the user took the permission away. The copy must not
        // be lost over it — the Settings screen says it goes to Downloads.
        val src = source("lost-access.mkv", 500)
        assertCopied("the copy", FinishedExport.export(context, src, treeUri.toString(), access = { false }))
        assertEquals(emptyList<String>(), namesIn(folder))
        assertEquals(0, TestFolderProvider.created.get())
        await("the copy to Downloads") { "lost-access.mkv" in downloadsAskedFor() }
    }

    @Test
    fun anUnusableFolderAddressMeansTheDownloadsFolder() {
        val src = source("bad-address.mkv", 500)
        assertCopied("the copy", FinishedExport.export(context, src, "not a folder", access = allowed))
        assertEquals(emptyList<String>(), namesIn(folder))
    }

    @Test
    fun withoutAChosenFolderThePublicDownloadsLaneIsUsed() {
        val src = source("no-folder.mkv", 500)
        assertCopied("the export to Downloads", FinishedExport.export(context, src, "", access = allowed))
        assertEquals(emptyList<String>(), namesIn(folder))
        await("the copy to Downloads") { "no-folder.mkv" in downloadsAskedFor() }
    }

    @Test
    fun theFolderReallyUsedIsTheChosenOneOnlyWhileItCanBeWrittenTo() {
        assertEquals("", FinishedExport.effectiveFolder(context, ""))
        assertEquals("no permission yet", "", FinishedExport.effectiveFolder(context, treeUri.toString()))
        assertEquals("not an address", "", FinishedExport.effectiveFolder(context, "not a folder"))
        keepFolderForThisTest()
        assertEquals(treeUri.toString(), FinishedExport.effectiveFolder(context, treeUri.toString()))
        context.contentResolver.releasePersistableUriPermission(treeUri, Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        assertEquals("write permission gone", "", FinishedExport.effectiveFolder(context, treeUri.toString()))
    }

    @Test
    fun everyFileGoesIntoAChosenFolderButOnlyVideoIntoDownloads() {
        val chosen = treeUri.toString()
        assertTrue(FinishedExport.isCopied("Album/01 - Track.flac", chosen))
        assertTrue(FinishedExport.isCopied("Book.pdf", chosen))
        assertFalse("a torrent of thousands of small files must not flood Downloads", FinishedExport.isCopied("Album/01 - Track.flac", ""))
        assertTrue(FinishedExport.isCopied("Ubuntu/ubuntu-24.04.iso", ""))
        assertTrue(FinishedExport.isCopied("Movie.MKV", ""))
    }

    /** A file already in Downloads, served back the way the collection would. */
    private fun inDownloads(content: ByteArray): android.net.Uri =
        android.net.Uri.parse("content://media/external_primary/downloads/${nextDownloadsId++}").also {
            shadowOf(context.contentResolver).registerInputStream(it, java.io.ByteArrayInputStream(content))
        }

    private var nextDownloadsId = 70L

    @Test
    fun aFileAlreadyInDownloadsIsNotCopiedThereAgain() {
        val src = source("movie.mkv", 500)
        val there = inDownloads(src.readBytes())
        val asked = mutableListOf<String>()
        val result = FinishedExport.export(context, src, "", access = allowed, alreadyInDownloads = { name, size ->
            asked.add(name)
            if (name == src.name && size == src.length()) there else null
        })
        assertEquals(FinishedExport.Result.AlreadyThere, result)
        assertEquals("looked for the download by its own name", listOf("movie.mkv"), asked.toList())
        assertEquals("nothing new was asked of the Downloads collection", 0, shadowOf(context.contentResolver).insertStatements.count { it.uri.authority == MediaStore.AUTHORITY })
    }

    @Test
    fun aFileNotYetInDownloadsStillGoesThere() {
        val src = source("movie.mkv", 500)
        val result = FinishedExport.export(context, src, "", access = allowed, alreadyInDownloads = { _, _ -> null })
        assertCopied("the export to Downloads", result)
        await("the copy to Downloads") { "movie.mkv" in downloadsAskedFor() }
    }

    @Test
    fun aDifferentFileOfTheSameNameAndSizeIsCopiedAgain() {
        // Same name, same size, other bytes — a re-release under the old name.
        // Told apart by name and size alone, the finished download went nowhere.
        val src = source("movie.mkv", 500)
        val other = inDownloads(ByteArray(500) { 9 })
        val result = FinishedExport.export(context, src, "", access = allowed, alreadyInDownloads = { _, _ -> other })
        assertCopied("the export to Downloads", result)
        await("the copy to Downloads") { "movie.mkv" in downloadsAskedFor() }
    }

    @Test
    fun aFileThereThatCannotBeReadIsCopiedAgain() {
        // Not the other way round: an unreadable match must never swallow a download.
        val src = source("movie.mkv", 500)
        val nowhere = android.net.Uri.parse("content://media/external_primary/downloads/999")
        val result = FinishedExport.export(context, src, "", access = allowed, alreadyInDownloads = { _, _ -> nowhere })
        assertCopied("the export to Downloads", result)
    }

    @Test
    fun aBigFileIsRecognisedByItsBeginningAlone() {
        // Only the beginning is looked at: the middle of a multi-gigabyte file is never
        // pulled through a provider to compare the end — one name, one size and one
        // beginning is the same file.
        val src = source("big.mkv", 5 * 1024 * 1024)
        val almostTheSame = src.readBytes().also { it[2 * 1024 * 1024] = 99 }
        val twin = inDownloads(almostTheSame)
        val result = FinishedExport.export(context, src, "", access = allowed, alreadyInDownloads = { _, _ -> twin })
        assertEquals(FinishedExport.Result.AlreadyThere, result)
    }

    @Test
    fun aMatchThatHandsBackLessThanTheBeginningIsCopiedAgain() {
        // Not the other way round either: a match that cannot be read as far as asked is no match.
        // A source that a lenient read would mistake for it: the first 100 bytes as the match hands
        // them back, and nothing but zeros where it stops short.
        val bytes = ByteArray(500).also { it[3] = 5; it[60] = 9 }
        val src = File(sources, "short-match.mkv").apply { writeBytes(bytes) }
        val cut = inDownloads(bytes.copyOf(100))
        val result = FinishedExport.export(context, src, "", access = allowed, alreadyInDownloads = { _, _ -> cut })
        assertCopied("the export to Downloads", result)
    }

    /** Answers what the app asks the media store, and remembers it. */
    class RecordingMediaProvider : android.content.ContentProvider() {
        var uri: android.net.Uri? = null
        var projection: Array<out String>? = null
        var selection: String? = null
        var args: Array<out String>? = null
        var rows = 0

        override fun onCreate() = true
        override fun getType(uri: android.net.Uri): String? = null
        override fun insert(uri: android.net.Uri, values: android.content.ContentValues?): android.net.Uri? = null
        override fun delete(uri: android.net.Uri, selection: String?, selectionArgs: Array<out String>?): Int = 0
        override fun update(uri: android.net.Uri, values: android.content.ContentValues?, selection: String?, selectionArgs: Array<out String>?): Int = 0
        override fun query(uri: android.net.Uri, projection: Array<out String>?, selection: String?, selectionArgs: Array<out String>?, sortOrder: String?): android.database.Cursor {
            this.uri = uri
            this.projection = projection
            this.selection = selection
            this.args = selectionArgs
            return android.database.MatrixCursor(arrayOf("_id")).also { c -> repeat(rows) { c.addRow(arrayOf<Any>(77)) } }
        }
    }

    @Test
    fun theLookUpInDownloadsAsksForTheNameTheSizeAndFinishedRowsOnly() {
        // The real look-up, against what the media store is asked — every other test stands in for it.
        val media = RecordingMediaProvider()
        org.robolectric.shadows.ShadowContentResolver.registerProviderInternal(MediaStore.AUTHORITY, media)
        val collection = MediaStore.Downloads.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)

        org.junit.Assert.assertNull("nothing there", space.deepflux.android.dlmgr.MediaStoreExport.alreadyInDownloads(context, "movie.mkv", 500))
        assertEquals("the Downloads collection of the primary volume", collection, media.uri)
        assertEquals(listOf("movie.mkv", "500"), media.args?.toList())
        val asked = media.selection.orEmpty()
        assertTrue("by name: $asked", asked.contains("${MediaStore.MediaColumns.DISPLAY_NAME} = ?"))
        assertTrue("…and by size: $asked", asked.contains("${MediaStore.MediaColumns.SIZE} = ?"))
        assertTrue("…and not one still being written: $asked", asked.contains("${MediaStore.MediaColumns.IS_PENDING} = 0"))

        media.rows = 1
        assertEquals(
            "a row is its address",
            collection.buildUpon().appendPath("77").build(),
            space.deepflux.android.dlmgr.MediaStoreExport.alreadyInDownloads(context, "movie.mkv", 500),
        )

        media.args = null
        org.junit.Assert.assertNull("nothing to look for", space.deepflux.android.dlmgr.MediaStoreExport.alreadyInDownloads(context, "movie.mkv", 0))
        org.junit.Assert.assertNull("…asks nothing", media.args)
    }

    // ---------------------------------------------------------------- the choice itself

    @Test
    fun keepingAccessMakesTheFolderUsable() {
        assertFalse(FinishedExport.canWriteTo(context, treeUri))
        assertTrue(FinishedExport.keepAccess(context, treeUri))
        assertTrue(FinishedExport.canWriteTo(context, treeUri))
    }

    @Test
    fun aRefusedPermissionIsReportedNotThrown() {
        assertFalse(FinishedExport.keepAccess(treeUri) { throw SecurityException("no persistable grant was handed out") })
        assertFalse(FinishedExport.canWriteTo(context, treeUri))
    }

    @Test
    fun theKeptFolderPermissionIsWhatTheAppGoesBy() {
        assertFalse("nothing was picked yet", FinishedExport.canWriteTo(context, treeUri))
        keepFolderForThisTest()
        assertTrue(FinishedExport.canWriteTo(context, treeUri))
        context.contentResolver.releasePersistableUriPermission(treeUri, Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        assertFalse("the system or the user took it away", FinishedExport.canWriteTo(context, treeUri))
    }

    @Test
    fun pickingAFolderMakesItTheDestination() {
        val repo = newRepo()
        // The settings file is one for the whole test run: start from the
        // default, and leave the default behind.
        backToDefault(repo)
        repo.pickedFinishedDownloadsFolder(treeUri, kept = true)
        await("the choice is stored") { repo.finishedDownloadFolder() == treeUri.toString() }
        repo.clearCustomSaveDir()
        backToDefault(repo)
    }

    @Test
    fun aFolderTheSystemWillNotKeepIsNotTaken() {
        val repo = newRepo()
        backToDefault(repo)
        val notices = noticesOf(repo)
        val keptOne = DocumentsContract.buildTreeDocumentUri(TestFolderProvider.AUTHORITY, "root")
        repo.pickedFinishedDownloadsFolder(keptOne, kept = true)
        await("the first choice is stored") { repo.finishedDownloadFolder() == keptOne.toString() }
        val refused = Uri.parse("content://elsewhere/tree/xyz")
        repo.pickedFinishedDownloadsFolder(refused, kept = false)
        await("the notice") { notices.isNotEmpty() }
        assertEquals(listOf(Strings.t.noticeFolderNotKept), notices.toList())
        assertEquals("the earlier choice stands", keptOne.toString(), repo.finishedDownloadFolder())
        backToDefault(repo)
    }

    // ---------------------------------------------------------------- the export pool

    @Test
    fun theExportPoolCountsItsJobsAndNeverLetsOneTakeTheAppDown() {
        val started = CountDownLatch(1)
        val release = CountDownLatch(1)
        FinishedExport.submit {
            started.countDown()
            release.await(10, TimeUnit.SECONDS)
        }
        assertTrue(started.await(10, TimeUnit.SECONDS))
        assertTrue("a running copy keeps the download service up", FinishedExport.busy())
        release.countDown()
        await("the job to end") { !FinishedExport.busy() }
        // A job runs on a thread of its own: on a device an exception getting
        // out ends the process. Here it would reach the default handler.
        val uncaught = CopyOnWriteArrayList<Throwable>()
        val previousHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { _, e -> uncaught.add(e) }
        try {
            FinishedExport.submit { throw IllegalStateException("a folder provider misbehaved") }
            FinishedExport.submit { error("and another one") }
            await("the failed jobs to end") { FinishedExport.pendingJobs() == 0 }
            // The counter drops before a dying thread reaches the handler.
            Thread.sleep(300)
        } finally {
            Thread.setDefaultUncaughtExceptionHandler(previousHandler)
        }
        assertEquals("nothing reached past the pool", emptyList<Throwable>(), uncaught.toList())
        FinishedExport.submit { }
        await("the pool still takes work") { !FinishedExport.busy() }
    }

    @Test
    fun aCopyThatStopsWritingNoLongerHoldsTheService() {
        // A folder provider that hangs (storage pulled out, a cloud folder off
        // the air) must not keep the download service up for good.
        val stallWas = FinishedExport.stallMs
        FinishedExport.stallMs = 300
        try {
            val started = CountDownLatch(1)
            val release = CountDownLatch(1)
            FinishedExport.submit {
                started.countDown()
                release.await(10, TimeUnit.SECONDS)
            }
            assertTrue(started.await(10, TimeUnit.SECONDS))
            assertTrue("just handed in, it counts", FinishedExport.busy())
            await("the copy to count as stalled") { !FinishedExport.busy() }
            assertTrue("the job itself is still there", FinishedExport.pendingJobs() >= 1)
            release.countDown()
            await("the job to end") { FinishedExport.pendingJobs() == 0 }
        } finally {
            FinishedExport.stallMs = stallWas
        }
    }

    // ---------------------------------------------------------------- the torrent lane

    /** A download folder holding the given files (800 bytes each, different bytes per file). */
    private fun downloaded(vararg paths: String): File {
        val downloadFolder = File(context.cacheDir, "dl").apply { mkdirs() }
        for (path in paths) {
            File(downloadFolder, path).apply {
                parentFile?.mkdirs()
                writeBytes(ByteArray(800) { (it % 5 + path.length).toByte() })
            }
        }
        return downloadFolder
    }

    @Test
    fun aTorrentFileIsCopiedInsideTheTorrentsOwnFolders() {
        keepFolderForThisTest()
        val repo = newRepo()
        val downloadFolder = downloaded("Show/Season 1/ep1.mkv")
        assertCopied("the copy", repo.exportTorrentFile(downloadFolder, "Show/Season 1/ep1.mkv", treeUri.toString()))
        assertArrayEquals(
            File(downloadFolder, "Show/Season 1/ep1.mkv").readBytes(),
            inFolder("Show/Season 1/ep1.mkv")!!.readBytes(),
        )
    }

    @Test
    fun aNonVideoTorrentFileGoesOnlyIntoAChosenFolder() {
        val repo = newRepo()
        val downloadFolder = downloaded("Album/01 - Track.flac")
        assertNull("not into the public Downloads collection", repo.exportTorrentFile(downloadFolder, "Album/01 - Track.flac", ""))
        assertEquals(0, TestFolderProvider.created.get())
        assertFalse("01 - Track.flac" in downloadsAskedFor())
        keepFolderForThisTest()
        assertCopied("the copy", repo.exportTorrentFile(downloadFolder, "Album/01 - Track.flac", treeUri.toString()))
        assertArrayEquals(File(downloadFolder, "Album/01 - Track.flac").readBytes(), inFolder("Album/01 - Track.flac")!!.readBytes())
    }

    @Test
    fun aTorrentPathThatLeavesTheDownloadFolderIsNotCopiedOut() {
        keepFolderForThisTest()
        val repo = newRepo()
        val downloadFolder = downloaded("episode.mkv")
        File(context.cacheDir, "secret.txt").writeText("private")
        assertNull(repo.exportTorrentFile(downloadFolder, "../secret.txt", treeUri.toString()))
        assertEquals(emptyList<String>(), namesIn(folder))
        assertEquals(0, TestFolderProvider.created.get())
    }

    @Test
    fun aTorrentFileThatIsNotThereIsNotCopiedOut() {
        keepFolderForThisTest()
        val repo = newRepo()
        val downloadFolder = downloaded("episode.mkv")
        assertNull(repo.exportTorrentFile(downloadFolder, "missing.mkv", treeUri.toString()))
        assertEquals(0, TestFolderProvider.created.get())
    }

    @Test
    fun aSymbolicLinkInsideTheDownloadFolderIsNotCopiedOut() {
        keepFolderForThisTest()
        val repo = newRepo()
        val downloadFolder = downloaded("episode.mkv")
        val created = runCatching {
            // A link to a file INSIDE the download folder: only the link
            // check itself can tell it from a real downloaded file.
            java.nio.file.Files.createSymbolicLink(File(downloadFolder, "link.mkv").toPath(), File(downloadFolder, "episode.mkv").toPath())
        }
        Assume.assumeTrue("this machine cannot create symbolic links: ${created.exceptionOrNull()}", created.isSuccess)
        assertNull(repo.exportTorrentFile(downloadFolder, "link.mkv", treeUri.toString()))
        assertEquals(emptyList<String>(), namesIn(folder))
    }

    @Test
    fun withAFolderItCanNoLongerOpenATorrentKeepsTheDownloadsRule() {
        val repo = newRepo()
        backToDefault(repo)
        // Stored as chosen, but the permission is gone.
        repo.pickedFinishedDownloadsFolder(treeUri, kept = true)
        await("the stored choice") { repo.finishedDownloadFolder() == treeUri.toString() }
        assertFalse(FinishedExport.canWriteTo(context, treeUri))
        val downloadFolder = downloaded("Album/flood-track.flac", "rule-movie.mkv")
        assertEquals("nothing failed — the rule simply changed", 0, repo.exportFinishedFiles(downloadFolder, listOf("Album/flood-track.flac", "rule-movie.mkv")))
        assertEquals("the folder that cannot be opened is not touched", 0, TestFolderProvider.created.get())
        await("the video file in Downloads") { "rule-movie.mkv" in downloadsAskedFor() }
        assertFalse("a non-video file does not flood Downloads", "flood-track.flac" in downloadsAskedFor())
        backToDefault(repo)
    }

    @Test
    fun failedTorrentCopiesAreToldOnceAndDoNotStopTheOthers() {
        keepFolderForThisTest()
        val repo = newRepo()
        backToDefault(repo)
        repo.pickedFinishedDownloadsFolder(treeUri, kept = true)
        await("the stored choice") { repo.finishedDownloadFolder() == treeUri.toString() }
        val notices = noticesOf(repo)
        File(folder, "Season 1").writeText("a file, not a folder")
        val downloadFolder = downloaded("Season 1/a.mkv", "Season 1/b.mkv", "fine.mkv")
        grantPostNotifications()
        val before = copyFailedNotifications().size
        val failed = repo.exportFinishedFiles(downloadFolder, listOf("Season 1/a.mkv", "Season 1/b.mkv", "fine.mkv"))
        assertEquals(2, failed)
        assertEquals(listOf(Strings.t.noticeExportFailedCountFmt.format(2)), notices.toList())
        assertArrayEquals(File(downloadFolder, "fine.mkv").readBytes(), inFolder("fine.mkv")!!.readBytes())
        // Told outside the app too — a snackbar is seen only while it is open.
        await("the notification") { copyFailedNotifications().size == before + 1 }
        val posted = copyFailedNotifications().last()
        val extras = posted.extras
        assertEquals(context.getString(R.string.notif_copy_failed_title), extras.getCharSequence(android.app.Notification.EXTRA_TITLE).toString())
        assertEquals(context.resources.getQuantityString(R.plurals.notif_files_count, 2, 2), extras.getCharSequence(android.app.Notification.EXTRA_TEXT).toString())
        backToDefault(repo)
    }

    /** Notifications saying a copy for the user failed (they pile up over the test run). */
    private fun copyFailedNotifications(): List<android.app.Notification> =
        shadowOf(context.getSystemService(android.app.NotificationManager::class.java)).allNotifications
            .filter {
                it.extras.getCharSequence(android.app.Notification.EXTRA_TITLE) == context.getString(R.string.notif_copy_failed_title)
            }

    @Test
    fun aTorrentWithNothingToCopySaysNothing() {
        val repo = newRepo()
        backToDefault(repo)
        val notices = noticesOf(repo)
        val downloadFolder = downloaded("Album/track.flac")
        assertEquals(0, repo.exportFinishedFiles(downloadFolder, listOf("Album/track.flac")))
        assertTrue("no notice for files that are simply not copied to Downloads", notices.isEmpty())
    }

    @Test
    fun aFileWhoseCopyBlowsUpDoesNotStopTheOthers() {
        val repo = newRepo()
        var asked = 0
        val failed = repo.exportFinishedFiles(File(context.cacheDir, "dl"), listOf("a.mkv", "b.mkv", "c.mkv")) { _, _, _ ->
            asked++
            if (asked == 1) throw IllegalStateException("a folder provider misbehaved")
            FinishedExport.Result.Copied
        }
        assertEquals("every file got its turn", 3, asked)
        assertEquals("a copy that blew up is a file not copied", 1, failed)
    }

    @Test
    fun aTorrentIsNeverReRoutedToDownloadsBehindItsBack() {
        // The torrent lane settles on its folder once. For a file that only a
        // chosen folder justifies copying, Downloads is never the answer —
        // not even when the folder cannot be opened any more.
        val repo = newRepo()
        val downloadFolder = downloaded("Album/mid-job-track.flac")
        assertFalse("no permission for the folder", FinishedExport.canWriteTo(context, treeUri))
        repo.exportTorrentFile(downloadFolder, "Album/mid-job-track.flac", treeUri.toString())
        await("anything that would go to Downloads") { FinishedExport.pendingJobs() == 0 }
        assertFalse("mid-job-track.flac" in downloadsAskedFor())
    }

    // ---------------------------------------------------------------- the plain-download lane

    private fun slowBody(size: Int) = ByteArray(size) { (it % 11).toByte() }

    @Test
    fun aFinishedPlainDownloadIsCopiedIntoTheChosenFolder() {
        // The download lane runs the production permission check, not a stand-in.
        keepFolderForThisTest()
        val body = slowBody(256 * 1024)
        server.route("/file") { TinyHttpServer.Response(body = body) }
        val saveDir = File(context.cacheDir, "dl").apply { mkdirs() }
        val repo = HttpDownloadRepository(context, saveDir, gateOk = { true }, notify = {}, exportFolder = { treeUri.toString() })
        val item = repo.add(server.url("/file"))!!
        await("the download") { repo.items.value.any { it.id == item.id && it.state == HttpState.DONE } }
        // The file is made first and filled afterwards: wait for all of it,
        // not merely for it to exist (a slow run once read it at 0 bytes).
        await("the copy") { inFolder(item.fileName)?.length() == body.size.toLong() }
        assertArrayEquals(body, inFolder(item.fileName)!!.readBytes())
    }

    @Test
    fun aFinishedPlainDownloadGoesToThePublicDownloadsFolderWhenNoneIsChosen() {
        val body = slowBody(8 * 1024)
        server.route("/default-lane-file") { TinyHttpServer.Response(body = body) }
        val saveDir = File(context.cacheDir, "dl").apply { mkdirs() }
        val repo = HttpDownloadRepository(context, saveDir, gateOk = { true }, notify = {}, exportFolder = { "" })
        val item = repo.add(server.url("/default-lane-file"))!!
        await("the download") { repo.items.value.any { it.id == item.id && it.state == HttpState.DONE } }
        // Really handed to the Downloads collection — not merely "not put anywhere else".
        await("the copy to Downloads") { item.fileName in downloadsAskedFor() }
        assertEquals("no chosen folder is involved", 0, TestFolderProvider.created.get())
    }

    @Test
    fun aPlainDownloadThatCouldNotBeCopiedIsToldAboutIt() {
        keepFolderForThisTest()
        val body = slowBody(128 * 1024)
        // Slow enough to arm the failure before the download ends.
        server.route("/file") { TinyHttpServer.Response(body = body, chunkSize = 8 * 1024, chunkDelayMs = 30) }
        val saveDir = File(context.cacheDir, "dl").apply { mkdirs() }
        val notices = CopyOnWriteArrayList<String>()
        val toldOutsideTheApp = CopyOnWriteArrayList<String>()
        val repo = HttpDownloadRepository(
            context, saveDir, gateOk = { true }, notify = { notices.add(it) }, exportFolder = { treeUri.toString() },
        )
        repo.onCopyFailed = { toldOutsideTheApp.add(it) }
        val item = repo.add(server.url("/file"))!!
        TestFolderProvider.refuseToOpen = item.fileName
        await("the download") { repo.items.value.any { it.id == item.id && it.state == HttpState.DONE } }
        val expected = Strings.t.noticeExportFailedFmt.format(item.fileName)
        await("the notice") { expected in notices }
        assertEquals("said once", 1, notices.count { it == expected })
        await("told outside the app too") { toldOutsideTheApp.isNotEmpty() }
        assertEquals(listOf(item.fileName), toldOutsideTheApp.toList())
    }

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 15_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(25)
        }
    }
}
