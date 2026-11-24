package space.deepflux.android.dlmgr

import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.DocumentsContract
import android.util.Log
import android.webkit.MimeTypeMap
import java.io.File
import java.io.FileInputStream
import java.io.IOException
import java.io.OutputStream
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

/**
 * Where a finished download goes so the user can find it.
 *
 * Downloads always run in the app's own storage — libtorrent cannot write
 * through scoped storage, and the app's folder is invisible to most file
 * managers — so when a download finishes, a copy is made for the user:
 * into the folder chosen in Settings → "Save location" (any folder a
 * document provider offers), or, when none is chosen (or the app can no
 * longer open it), into the public Downloads collection.
 *
 * The chosen folder used to be remembered and nothing more: nothing read it
 * (owner, 2026-11-24: "make it work").
 */
object FinishedExport {

    sealed class Result {
        /** A copy is in the user's folder. */
        object Copied : Result()

        /** The same file (name and size) was already there — an earlier export. */
        object AlreadyThere : Result()

        /** Nothing to copy to on this Android version (no public Downloads collection before Android 10). Not a failure. */
        object NoDestination : Result()

        /** No copy was made; the download itself is untouched in the app's storage. */
        class Failed(val reason: String) : Result()
    }

    /**
     * One export job's view of the chosen folder: every folder is listed
     * ONCE per job. A torrent of thousands of files in one folder used to
     * re-read that folder's whole listing for each of them. It is a memory,
     * not the truth: whatever is about to be written or deleted is asked of
     * the provider itself.
     */
    class Run {
        internal val listings = HashMap<String, MutableMap<String, Entry>>()
    }

    internal class Entry(val id: String, val size: Long, val directory: Boolean)

    // ------------------------------------------------------------------ the choice

    /**
     * Asks the system to keep the app's access to a folder the user just
     * picked, across launches. False when it will not: a folder the app
     * cannot open after a restart is no use as a destination.
     */
    fun keepAccess(context: Context, folderUri: Uri): Boolean = keepAccess(folderUri) {
        context.contentResolver.takePersistableUriPermission(
            it,
            Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
        )
    }

    internal fun keepAccess(folderUri: Uri, take: (Uri) -> Unit): Boolean = runCatching { take(folderUri) }.isSuccess

    /**
     * True while the app may still write to the folder that was picked. The
     * permission is kept by [keepAccess] when the user chooses the folder;
     * the system, or the user, can revoke it later.
     */
    fun canWriteTo(context: Context, folderUri: Uri): Boolean =
        context.contentResolver.persistedUriPermissions.any { it.uri == folderUri && it.isWritePermission }

    /**
     * The folder finished downloads really go to: the chosen one while the
     * app may write to it, else "" — the public Downloads collection.
     * Access to a chosen folder can be lost (the system or the user takes
     * the permission away, its provider is uninstalled); the torrent lane
     * asks this first, so a lost folder puts the old Downloads rules back
     * in force instead of copying every file of a torrent into Downloads.
     */
    fun effectiveFolder(context: Context, folderUri: String): String {
        if (folderUri.isBlank()) return ""
        val tree = runCatching { Uri.parse(folderUri) }.getOrNull() ?: return ""
        return if (canWriteTo(context, tree)) folderUri else ""
    }

    /**
     * Whether a file of a finished torrent is copied out for the user. Into a
     * folder the user chose: every file — that is what the folder is for.
     * Into the public Downloads collection (no folder chosen): video files
     * only, so a torrent of thousands of small files does not flood Downloads.
     */
    fun isCopied(relativePath: String, folderUri: String): Boolean =
        folderUri.isNotBlank() || VIDEO_EXTENSIONS.any { relativePath.endsWith(it, true) }

    // ------------------------------------------------------------------ progress, for the download service

    private val pending = AtomicInteger()
    private val copied = AtomicLong()

    /** When the last byte was copied, or the last job handed in — see [busy]. */
    private val lastActivityAt = AtomicLong()

    /**
     * How long a copy may go without writing a byte before it stops counting
     * as running. A folder provider that hangs (storage pulled out, a cloud
     * folder off the air) must not hold the download service up for good.
     */
    @Volatile
    internal var stallMs: Long = EXPORT_STALL_MS

    /**
     * True while a copy for the user is queued or running AND still getting
     * somewhere — the download service stays up for it. The clock is the
     * monotonic one, so a corrected wall clock changes nothing.
     */
    fun busy(): Boolean = pending.get() > 0 && now() - lastActivityAt.get() < stallMs

    /** Jobs handed in and not finished yet, moving or stuck. */
    internal fun pendingJobs(): Int = pending.get()

    /** Bytes copied into the user's folders since the process started (never reset). */
    fun copiedBytes(): Long = copied.get()

    private fun now(): Long = System.nanoTime() / 1_000_000

    private val pool = Executors.newFixedThreadPool(2) { task -> Thread(task, "finished-export").apply { isDaemon = true } }

    /**
     * Runs [job] on the shared export pool — two at a time at most: every
     * copy writes the same storage, and a page of downloads finishing
     * together must not become a thread swarm. Nothing in a job may reach
     * past this point: it runs on a thread of its own, where an uncaught
     * exception ends the process. False when the pool took nothing — the
     * caller then still owns whatever it was going to hand over with the job.
     */
    fun submit(job: () -> Unit): Boolean {
        pending.incrementAndGet()
        lastActivityAt.set(now())
        try {
            pool.execute {
                try {
                    job()
                } catch (e: Throwable) {
                    Log.w(TAG, "copy for the user failed: ${forLog(e.message ?: e.javaClass.simpleName)}")
                } finally {
                    pending.decrementAndGet()
                }
            }
            return true
        } catch (e: Exception) {
            pending.decrementAndGet()
            Log.w(TAG, "could not start a copy for the user: ${forLog(e.message ?: "")}")
            return false
        }
    }

    // ------------------------------------------------------------------ the copy

    /**
     * Copies [source] for the user. [folderUri] is the chosen folder's tree
     * URI, or blank for the public Downloads collection. [relativePath] is
     * where the file sits inside a torrent ("Season 1/ep1.mkv"); folders on
     * the way are created. [run] is the job this copy belongs to (see [Run]).
     *
     * A chosen folder the app cannot open counts as none: the copy goes to
     * the public Downloads collection instead of nowhere — unless
     * [fallbackToDownloads] is false, for a lane that has already decided
     * where the file goes (the torrent lane picks its folder once per
     * torrent and must not be re-routed behind its back).
     */
    fun export(
        context: Context,
        source: File,
        folderUri: String,
        relativePath: String = source.name,
        access: (Uri) -> Boolean = { canWriteTo(context, it) },
        run: Run? = null,
        fallbackToDownloads: Boolean = true,
        alreadyInDownloads: (String, Long) -> Uri? = { name, size ->
            MediaStoreExport.alreadyInDownloads(context, name, size)
        },
    ): Result {
        val tree = chosenTree(folderUri, access)
        if (tree == null) {
            if (!fallbackToDownloads && folderUri.isNotBlank()) {
                return Result.Failed("the chosen folder can no longer be opened")
            }
            return toDownloads(context, source, alreadyInDownloads)
        }
        return try {
            copyIntoTree(context, source, tree, relativePath, run)
        } catch (e: Exception) {
            Log.w(TAG, "copy into the chosen folder failed for ${forLog(source.name)}: ${forLog(e.message ?: "")}")
            Result.Failed(e.message ?: "copy failed")
        }
    }

    /**
     * The chosen folder as a usable tree — or null when there is none: nothing
     * chosen, not a folder address, or one the app can no longer open. Each
     * of those means the public Downloads collection, never "copied nowhere".
     */
    private fun chosenTree(folderUri: String, access: (Uri) -> Boolean): Uri? {
        if (folderUri.isBlank()) return null
        val tree = runCatching { Uri.parse(folderUri) }.getOrNull() ?: return null
        if (!access(tree)) return null
        return runCatching { DocumentsContract.getTreeDocumentId(tree) }.map { tree }.getOrNull()
    }

    /** How much of a file's beginning is looked at to tell it from another of the same name and size. */
    private const val SAMPLE_BYTES = 64L * 1024

    /**
     * Whether the file behind [uri] begins like [source] — the caller has
     * seen to it that both are of one size. The beginning only: another file
     * of one name, one size and one beginning is not a real case, and the end
     * of a big file cannot be had from a provider that hands out a pipe
     * without pulling the whole file through (a chosen folder may be a cloud
     * provider's). Anything that cannot be read — or that hands back less
     * than its beginning — counts as different: the caller then copies, and
     * at worst a numbered copy appears, where the other way round a file
     * would go missing.
     */
    private fun sameContent(context: Context, uri: Uri, source: File): Boolean = runCatching {
        val head = minOf(SAMPLE_BYTES, source.length()).toInt()
        val sourceHead = java.io.RandomAccessFile(source, "r").use { file -> ByteArray(head).also { file.readFully(it) } }
        val thereHead = context.contentResolver.openInputStream(uri)?.use { raw ->
            ByteArray(head).also { bytes -> java.io.DataInputStream(raw).readFully(bytes) }
        }
        thereHead?.contentEquals(sourceHead) ?: false
    }.getOrDefault(false)

    private fun toDownloads(
        context: Context,
        source: File,
        alreadyInDownloads: (String, Long) -> Uri?,
    ): Result {
        if (Build.VERSION.SDK_INT < 29) return Result.NoDestination
        // The same file (name and size) is already there: an earlier export,
        // or the torrent was downloaded again. Each finish used to add a
        // numbered copy — "ep1.mp4", "(1).mp4", "(2).mp4". Its bytes are
        // looked at too: a different file that merely has the same name and
        // size must not make a finished download go nowhere.
        val there = alreadyInDownloads(source.name, source.length())
        if (there != null && sameContent(context, there, source)) return Result.AlreadyThere
        return if (MediaStoreExport.exportToDownloads(context, source) != null) Result.Copied
        else Result.Failed("the public Downloads folder did not take the file")
    }

    private fun copyIntoTree(
        context: Context,
        source: File,
        treeUri: Uri,
        relativePath: String,
        run: Run?,
    ): FinishedExport.Result {
        val resolver = context.contentResolver
        // Only real path segments count: ".." never leaves the chosen folder.
        val segments = relativePath.split('/', '\\').filter { it.isNotBlank() && it != "." && it != ".." }
        val name = segments.lastOrNull() ?: source.name
        var dirId = DocumentsContract.getTreeDocumentId(treeUri)
        for (folder in segments.dropLast(1)) {
            dirId = folderId(resolver, treeUri, dirId, folder, run)
        }
        val listing = listing(resolver, treeUri, dirId, run)
        val existing = listing[name]
        // The same file (name, size and beginning) is already there: an earlier
        // export, or the torrent was downloaded again. Another file that merely
        // has the same name and size is not taken for it: it goes beside.
        if (existing != null && !existing.directory && existing.size == source.length() &&
            sameContent(context, DocumentsContract.buildDocumentUriUsingTree(treeUri, existing.id), source)
        ) return FinishedExport.Result.AlreadyThere
        val parent = DocumentsContract.buildDocumentUriUsingTree(treeUri, dirId)
        val document = DocumentsContract.createDocument(resolver, parent, mimeFor(name), name)
            ?: throw IOException("the folder would not take the file")
        val documentId = DocumentsContract.getDocumentId(document)
        // Only a file the provider has just made — empty — is written to. A
        // provider that answers with a file that is already there (some never
        // pick another name) hands over the user's file: writing would empty
        // it, so it is neither opened nor deleted afterwards. Asked of the
        // document itself, not only of the job's memory of the folder, which
        // can be old.
        val returned = readDocument(resolver, document)
        if (listing.values.any { it.id == documentId } || returned == null || returned.directory || returned.size > 0) {
            throw IOException("a different file of that name is already in the folder")
        }
        try {
            resolver.openOutputStream(document, "w")?.use { out -> copy(source, out) }
                ?: throw IOException("could not open the file for writing")
        } catch (e: Exception) {
            // Never leave a half-written file behind under the download's name.
            runCatching { DocumentsContract.deleteDocument(resolver, document) }
            throw e
        }
        Log.i(TAG, "copied ${forLog(source.name)} into the chosen folder")
        return FinishedExport.Result.Copied
    }

    private fun copy(source: File, out: OutputStream) {
        FileInputStream(source).use { input ->
            // Through a folder provider every write is a pipe to another
            // process: large chunks keep the round trips few.
            val buffer = ByteArray(COPY_BUFFER_BYTES)
            while (true) {
                val n = input.read(buffer)
                if (n < 0) break
                out.write(buffer, 0, n)
                copied.addAndGet(n.toLong())
                lastActivityAt.set(now())
            }
        }
    }

    /**
     * The MIME type a file is created with. From the file's own extension
     * through the system's table first: a system folder provider keeps the
     * name only when the type agrees with the extension — "notes.pdf" created
     * as "application/octet-stream" would come out as "notes.pdf.bin".
     */
    internal fun mimeFor(name: String): String {
        val extension = name.substringAfterLast('.', "").lowercase()
        val plain = extension.isNotEmpty() && extension.length <= 16 && extension.all { it.isLetterOrDigit() }
        val fromTable = if (plain) MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension) else null
        return fromTable ?: MediaStoreExport.guessMimeType(name)
    }

    /**
     * The folder called [name] directly inside [parentId], made when it is
     * not there yet. Another export can make the same folder at the same
     * moment — a real provider then refuses ("already exists") — and the
     * folder is simply found.
     */
    private fun folderId(resolver: ContentResolver, treeUri: Uri, parentId: String, name: String, run: Run?): String {
        listing(resolver, treeUri, parentId, run)[name]?.let {
            if (it.directory) return it.id
            throw IOException("a file named \"$name\" is in the way of the folder")
        }
        val parent = DocumentsContract.buildDocumentUriUsingTree(treeUri, parentId)
        val made = try {
            DocumentsContract.createDocument(resolver, parent, DocumentsContract.Document.MIME_TYPE_DIR, name)
                ?.let { DocumentsContract.getDocumentId(it) }
        } catch (e: Exception) {
            Log.i(TAG, "making the folder \"${forLog(name)}\" was refused: ${forLog(e.message ?: "")}")
            null
        }
        // What the job remembered about this folder may be out of date now.
        run?.listings?.remove(parentId)
        val after = listing(resolver, treeUri, parentId, run)[name]
        return when {
            // A file of that name is there: ours was refused, and what the provider answered cannot be that folder.
            after != null && !after.directory -> throw IOException("a file named \"$name\" is in the way of the folder")
            after != null -> after.id
            // The provider says it made the folder, the listing does not show it yet: its answer is what there is.
            else -> made?.also { id ->
                // …and the job remembers it, or the next file makes a second folder of that name.
                run?.listings?.getOrPut(parentId) { HashMap() }[name] = Entry(id, -1, true)
            } ?: throw IOException("could not create the folder \"$name\"")
        }
    }

    /** What folder [parentId] holds, by display name — from the job's memory when it looked before. */
    private fun listing(
        resolver: ContentResolver,
        treeUri: Uri,
        parentId: String,
        run: Run?,
    ): MutableMap<String, Entry> {
        if (run == null) return readListing(resolver, treeUri, parentId)
        return run.listings.getOrPut(parentId) { readListing(resolver, treeUri, parentId) }
    }

    private fun readListing(resolver: ContentResolver, treeUri: Uri, parentId: String): MutableMap<String, Entry> {
        val out = HashMap<String, Entry>()
        val children = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentId)
        resolver.query(
            children,
            arrayOf(
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE,
                DocumentsContract.Document.COLUMN_SIZE,
            ),
            // The modern query form (Android 8+; the selection overloads are
            // wrappers around it). Documents providers serve this one directly.
            android.os.Bundle.EMPTY,
            null,
        )?.use { c ->
            // By name, not position: a provider may answer with its own column order.
            val idColumn = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
            val nameColumn = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
            val mimeColumn = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_MIME_TYPE)
            val sizeColumn = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_SIZE)
            while (c.moveToNext()) {
                val name = c.getString(nameColumn) ?: continue
                out[name] = Entry(c.getString(idColumn), sizeOf(c, sizeColumn), c.getString(mimeColumn) == DocumentsContract.Document.MIME_TYPE_DIR)
            }
        }
        return out
    }

    /** One document, asked of the provider directly. Null when it answers nothing. */
    private fun readDocument(resolver: ContentResolver, document: Uri): Entry? {
        resolver.query(
            document,
            arrayOf(
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_MIME_TYPE,
                DocumentsContract.Document.COLUMN_SIZE,
            ),
            android.os.Bundle.EMPTY,
            null,
        )?.use { c ->
            if (!c.moveToNext()) return null
            val sizeColumn = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_SIZE)
            val mimeColumn = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_MIME_TYPE)
            val idColumn = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
            return Entry(c.getString(idColumn), sizeOf(c, sizeColumn), c.getString(mimeColumn) == DocumentsContract.Document.MIME_TYPE_DIR)
        }
        return null
    }

    /** A size the provider states; anything else (none given, not a number) counts as unknown. */
    private fun sizeOf(c: android.database.Cursor, sizeColumn: Int): Long =
        runCatching { if (c.isNull(sizeColumn)) -1L else c.getLong(sizeColumn) }.getOrDefault(-1L)

    /** Text for the log. Torrent names are whatever their maker typed: no line breaks, not endless. */
    private fun forLog(text: String): String = text.filterNot { it.isISOControl() }.take(160)

    /** The video files a torrent's finished copy is made of when no folder is chosen (see [isCopied]). */
    private val VIDEO_EXTENSIONS = listOf(".mp4", ".mkv", ".webm", ".avi", ".mov", ".iso")

    private const val COPY_BUFFER_BYTES = 256 * 1024

    private const val EXPORT_STALL_MS = 5 * 60_000L

    private const val TAG = "FinishedExport"
}
