package space.deepflux.android.filecmd

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.MediaStore
import android.util.Log
import androidx.core.content.FileProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.io.File

/**
 * File commander model (ANDROID_STUDY.md §12 Phase B1; v1.3 broad-access
 * rework) — the Android twin of the desktop's dual-pane Commander tab,
 * adapted to scoped storage with the broadest access legitimately
 * available:
 *
 *  * APP_STORAGE — the app's own download tree (direct File access);
 *  * PUBLIC_* — the MediaStore collections any app may read (Downloads,
 *    Videos, Music, Pictures);
 *  * SAF — any folder the USER chose via the system folder picker
 *    (persisted grant, [SafRootsStore]) — browsed through DocumentsContract
 *    child-document queries.
 *
 * OS-enforced restrictions (by design, not bugs): other apps' private
 * directories, /system, /data and storage roots stay inaccessible without
 * root; MANAGE_EXTERNAL_STORAGE ("All files access") is deliberately NOT
 * requested (Play policy-restricted and unnecessary given SAF + the public
 * collections).
 *
 * Pure data + operations; the screen owns panes and selection state.
 */
class CommanderRepository(
    private val context: Context,
    /** App-storage root: the shared downloads dir (torrents + HTTP + HLS). */
    val appRoot: File,
    /**
     * `content://` uri for a file in the app's own storage — the manifest's
     * FileProvider. A constructor parameter so the share/open-with logic is
     * testable on the JVM (the real provider's root check is
     * slash-specific and cannot match a path on a Windows test host).
     */
    private val appFileUri: (File) -> Uri = { f ->
        FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", f)
    },
    /**
     * The real symbolic-link test. A parameter so that a test can exercise
     * the link rules on a Windows dev shell, which cannot create symlinks.
     */
    private val isLink: (File) -> Boolean = { f -> java.nio.file.Files.isSymbolicLink(f.toPath()) },
) {

    enum class Root { APP_STORAGE, PUBLIC_DOWNLOADS, PUBLIC_VIDEOS, PUBLIC_MUSIC, PUBLIC_PICTURES, SAF }

    enum class Source { APP, MEDIASTORE, SAF }

    data class Entry(
        val name: String,
        val isDirectory: Boolean,
        val size: Long,
        val modified: Long,
        val source: Source,
        /**
         * APP: absolute path · MEDIASTORE/SAF: content uri.
         * SAF directories: the DOCUMENT uri (browsable via docId).
         */
        val path: String,
        val mime: String = "",
    )

    data class Listing(
        val root: Root,
        /** APP_STORAGE: relative dir. SAF: display path of the current document. */
        val dir: String = "",
        val entries: List<Entry> = emptyList(),
        val error: String = "",
    ) {
        val breadcrumb: String get() = if (dir.isBlank()) "" else dir
    }

    private val _listing = MutableStateFlow(Listing(Root.APP_STORAGE))
    val listing: StateFlow<Listing> = _listing.asStateFlow()

    val safRoots = SafRootsStore(context)

    /**
     * Recursive sizes of the folders in the CURRENT app-storage listing
     * (path → bytes), computed off the main thread by [FolderSizer]. A
     * folder's entry appears when its walk finishes; until then the row
     * shows "…". (Fix 2026-11-23: listing used to walk every folder
     * synchronously on the UI thread.)
     */
    private val _dirSizes = MutableStateFlow<Map<String, Long>>(emptyMap())
    val dirSizes: StateFlow<Map<String, Long>> = _dirSizes.asStateFlow()

    /** Bytes used by the whole app-storage tree, or null while unknown. */
    private val _storageUsed = MutableStateFlow<Long?>(null)
    val storageUsed: StateFlow<Long?> = _storageUsed.asStateFlow()

    private val sizeScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var sizeJob: Job? = null
    private var storageJob: Job? = null

    /** path → (bytes, computedAt). The short TTL keeps an active download from showing a stale size all session. */
    private val sizeCache = java.util.concurrent.ConcurrentHashMap<String, Pair<Long, Long>>()

    fun open(root: Root, subDir: String = "") {
        when (root) {
            Root.APP_STORAGE -> {
                val dir = File(appRoot, subDir)
                if (!isUnderRoot(dir)) {
                    _listing.value = Listing(root, "", error = "outside app storage")
                    return
                }
                val entries = runCatching {
                    // Symbolic links are not listed at all. A torrent can plant
                    // one inside the download folder, pointing at any file of
                    // the app's private storage; listed, it could then be
                    // shared, opened, or "deleted" through to its target.
                    dir.listFiles()?.filter { !isLink(it) }?.map { f ->
                        Entry(
                            name = f.name,
                            isDirectory = f.isDirectory,
                            // Folder sizes are NOT walked here: this runs on the
                            // UI thread. See [requestSizes] — they arrive off-thread.
                            size = if (f.isDirectory) -1L else f.length(),
                            modified = f.lastModified(),
                            source = Source.APP,
                            path = f.absolutePath,
                            mime = mimeOf(f.name),
                        )
                    }.orEmpty()
                }.getOrElse {
                    Log.w(TAG, "list failed: ${it.message}")
                    _listing.value = Listing(root, subDir, error = it.message?.take(80) ?: "cannot open")
                    return
                }
                _listing.value = Listing(
                    root,
                    subDir,
                    // Directories first, then names — the desktop Commander's default sort.
                    entries.sortedWith(compareByDescending<Entry> { it.isDirectory }.thenBy { it.name.lowercase() }),
                )
                requestSizes(entries.filter { it.isDirectory })
            }
            Root.SAF -> {
                // The screen drives SAF itself (active root + navigation
                // stack) via openSaf(); this path only lands here on a cold
                // open with no explicit root.
                val first = safRoots.roots.value.firstOrNull()
                if (first == null) {
                    _listing.value = Listing(Root.SAF, "", error = "no folders chosen yet")
                } else {
                    openSaf("", first, first.name)
                }
            }
            Root.PUBLIC_DOWNLOADS, Root.PUBLIC_VIDEOS, Root.PUBLIC_MUSIC, Root.PUBLIC_PICTURES -> {
                // MediaStore.Downloads exists from API 29 (lint: NewApi) — on
                // Android 9 fail gracefully instead of NoSuchMethodError.
                if (root == Root.PUBLIC_DOWNLOADS && android.os.Build.VERSION.SDK_INT < 29) {
                    _listing.value = Listing(root, "", error = "Public Downloads needs Android 10+")
                    return
                }
                val collection = when (root) {
                    Root.PUBLIC_DOWNLOADS -> MediaStore.Downloads.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
                    Root.PUBLIC_VIDEOS -> MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
                    Root.PUBLIC_MUSIC -> MediaStore.Audio.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
                    else -> MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
                }
                val entries = runCatching {
                    val out = ArrayList<Entry>()
                    context.contentResolver.query(
                        collection,
                        arrayOf(MediaStore.MediaColumns._ID, MediaStore.MediaColumns.DISPLAY_NAME,
                            MediaStore.MediaColumns.SIZE, MediaStore.MediaColumns.DATE_MODIFIED,
                            MediaStore.MediaColumns.MIME_TYPE),
                        null, null,
                        "${MediaStore.MediaColumns.DATE_MODIFIED} DESC",
                    )?.use { c ->
                        val iId = c.getColumnIndexOrThrow(MediaStore.MediaColumns._ID)
                        val iName = c.getColumnIndexOrThrow(MediaStore.MediaColumns.DISPLAY_NAME)
                        val iSize = c.getColumnIndexOrThrow(MediaStore.MediaColumns.SIZE)
                        val iMod = c.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_MODIFIED)
                        val iMime = c.getColumnIndexOrThrow(MediaStore.MediaColumns.MIME_TYPE)
                        while (c.moveToNext()) {
                            out.add(
                                Entry(
                                    name = c.getString(iName) ?: "(unnamed)",
                                    isDirectory = false,
                                    size = c.getLong(iSize),
                                    modified = c.getLong(iMod) * 1000L,
                                    source = Source.MEDIASTORE,
                                    path = Uri.withAppendedPath(collection, c.getLong(iId).toString()).toString(),
                                    mime = c.getString(iMime) ?: "",
                                )
                            )
                        }
                    }
                    out
                }.getOrElse {
                    Log.w(TAG, "mediastore query failed: ${it.message}")
                    _listing.value = Listing(root, "", error = it.message?.take(80) ?: "cannot open")
                    return
                }
                _listing.value = Listing(root, "", entries)
            }
        }
    }

    // ------------------------------------------------------------------ SAF browsing

    /**
     * List a SAF directory. [dirUriString] = "" → the root of [treeRoot];
     * otherwise a document uri inside that tree. [displayPath] is what the
     * breadcrumb shows (the screen tracks the navigated path names).
     * DocumentsContract child queries — DocumentFile.fromTreeUri() only
     * ever yields the TREE root, so subfolder navigation must go through
     * the children contract.
     */
    fun openSaf(dirUriString: String, treeRoot: SafRootsStore.SafRoot, displayPath: String) {
        val resolver = context.contentResolver
        runCatching {
            val parentUri = if (dirUriString.isBlank()) rootDocUri(treeRoot.uri) else Uri.parse(dirUriString)
            val parentDocId = DocumentsContract.getDocumentId(parentUri)
            val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeRoot.uri, parentDocId)
            val out = ArrayList<Entry>()
            resolver.query(
                childrenUri,
                arrayOf(
                    DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                    DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                    DocumentsContract.Document.COLUMN_MIME_TYPE,
                    DocumentsContract.Document.COLUMN_SIZE,
                    DocumentsContract.Document.COLUMN_LAST_MODIFIED,
                ),
                null, null,
                "${DocumentsContract.Document.COLUMN_DISPLAY_NAME} ASC",
            )?.use { c ->
                val iId = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
                val iName = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
                val iMime = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_MIME_TYPE)
                val iSize = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_SIZE)
                val iMod = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_LAST_MODIFIED)
                while (c.moveToNext()) {
                    val docId = c.getString(iId) ?: continue
                    val mime = c.getString(iMime) ?: ""
                    val isDir = mime == DocumentsContract.Document.MIME_TYPE_DIR
                    val docUri = DocumentsContract.buildDocumentUriUsingTree(treeRoot.uri, docId)
                    out.add(
                        Entry(
                            name = c.getString(iName) ?: "(unnamed)",
                            isDirectory = isDir,
                            size = c.getLong(iSize),
                            modified = c.getLong(iMod),
                            source = Source.SAF,
                            path = docUri.toString(),
                            mime = if (isDir) DocumentsContract.Document.MIME_TYPE_DIR else mime.ifBlank { mimeOf(c.getString(iName) ?: "") },
                        ),
                    )
                }
            }
            _listing.value = Listing(
                Root.SAF,
                displayPath,
                out.sortedWith(compareByDescending<Entry> { it.isDirectory }.thenBy { it.name.lowercase() }),
            )
        }.onFailure {
            Log.w(TAG, "saf list failed: ${it.message}")
            // SecurityException = the persisted grant was revoked (folder
            // removed by the user or the provider) — a re-pick fixes it.
            val hint = if (it is SecurityException) "grant revoked — remove and re-add this folder" else (it.message ?: "cannot open")
            _listing.value = Listing(Root.SAF, "", error = hint.take(120))
        }
    }

    /** The stored SAF root whose tree contains this document/uri (null when none). */
    fun safRootFor(uriString: String): SafRootsStore.SafRoot? {
        val treeId = treeIdOf(Uri.parse(uriString)) ?: return null
        return safRoots.roots.value.find { treeIdOf(it.uri) == treeId }
    }

    // ------------------------------------------------------------------ operations

    /** APP_STORAGE: the parent of the current dir, or null at the root. */
    fun parentOf(listing: Listing): String? {
        if (listing.root != Root.APP_STORAGE || listing.dir.isBlank()) return null
        val trimmed = listing.dir.trimEnd('/')
        return trimmed.substringBeforeLast('/', "")
    }

    fun delete(entry: Entry): Boolean {
        val ok = deleteUnchecked(entry)
        if (ok) refreshSizesAfterChange()
        return ok
    }

    private fun deleteUnchecked(entry: Entry): Boolean = when (entry.source) {
        Source.APP -> {
            val f = File(entry.path)
            // Links are never listed; should one reach here by its path, it
            // is refused like anything outside the root.
            if (!isUnderRoot(f) || isLink(f)) false else deleteTree(f)
        }
        Source.MEDIASTORE -> runCatching {
            // Scoped storage: we own the exports we created (MediaStoreExport),
            // and those are exactly the ones the user will want to clean.
            context.contentResolver.delete(Uri.parse(entry.path), null, null) > 0
        }.getOrDefault(false)
        Source.SAF -> runCatching {
            DocumentsContract.deleteDocument(context.contentResolver, Uri.parse(entry.path))
        }.getOrDefault(false)
    }

    /**
     * Removes a file, or a folder WITH everything in it — without ever
     * following a symbolic link. Kotlin's deleteRecursively() walks with
     * listFiles(), which goes THROUGH a linked folder: deleting a downloaded
     * folder that contained one wiped whatever it pointed at, anywhere in
     * the app's private storage (second security review).
     */
    private fun deleteTree(file: File): Boolean {
        if (isLink(file)) return runCatching { java.nio.file.Files.deleteIfExists(file.toPath()) }.getOrDefault(false)
        if (!file.isDirectory) return file.delete()
        var allGone = true
        file.listFiles()?.forEach { if (!deleteTree(it)) allGone = false }
        return allGone && file.delete()
    }

    fun rename(entry: Entry, newName: String): Boolean {
        val ok = renameUnchecked(entry, newName)
        if (ok) refreshSizesAfterChange()
        return ok
    }

    private fun renameUnchecked(entry: Entry, newName: String): Boolean {
        val clean = newName.trim().replace(Regex("[\\\\/:*?\"<>|]"), "_")
        if (clean.isBlank() || clean == entry.name) return false
        return when (entry.source) {
            Source.APP -> {
                val f = File(entry.path)
                if (!isUnderRoot(f)) return false
                f.renameTo(File(f.parentFile, clean))
            }
            Source.MEDIASTORE -> runCatching {
                val values = android.content.ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, clean)
                }
                context.contentResolver.update(Uri.parse(entry.path), values, null, null) > 0
            }.getOrDefault(false)
            Source.SAF -> runCatching {
                DocumentsContract.renameDocument(context.contentResolver, Uri.parse(entry.path), clean) != null
            }.getOrDefault(false)
        }
    }

    /**
     * A `content://` uri another app can actually read. App-storage files sit
     * in INTERNAL storage (`filesDir/DeepFlux`): the raw path that used to be
     * handed out is unreadable outside the app, so Share and Open with failed
     * for every downloaded file. The manifest's FileProvider exposes exactly
     * that tree (and nothing else).
     */
    private fun shareableUri(entry: Entry): Uri = when (entry.source) {
        Source.APP -> appFileUri(File(entry.path))
        else -> Uri.parse(entry.path)
    }

    fun shareIntent(entry: Entry): android.content.Intent? = runCatching {
        val send = android.content.Intent(android.content.Intent.ACTION_SEND)
        send.setType(entry.mime.ifBlank { "*/*" })
        send.putExtra(android.content.Intent.EXTRA_STREAM, shareableUri(entry))
        send.addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
        android.content.Intent.createChooser(send, entry.name)
    }.onFailure { Log.w(TAG, "share: ${it.message}") }.getOrNull()

    /** "Open with" for anything the app itself cannot render. */
    fun openWithIntent(entry: Entry): android.content.Intent? = runCatching {
        val uri = shareableUri(entry)
        android.content.Intent(android.content.Intent.ACTION_VIEW, uri).apply {
            setDataAndType(uri, entry.mime.ifBlank { "*/*" })
            addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION or android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
        }
    }.onFailure { Log.w(TAG, "open with: ${it.message}") }.getOrNull()

    // ------------------------------------------------------------------ sizes (off the UI thread)

    /**
     * Size the folders of the listing that was just published. Cancels the
     * previous pane's walk (a pane that navigated away must not keep
     * scanning), shows fresh cached sizes at once and walks the rest.
     */
    private fun requestSizes(dirs: List<Entry>) {
        sizeJob?.cancel()
        if (dirs.isEmpty()) {
            _dirSizes.value = emptyMap()
            return
        }
        val paths = dirs.map { it.path }
        _dirSizes.value = paths.mapNotNull { p -> freshSize(p)?.let { p to it } }.toMap()
        sizeJob = sizeScope.launch {
            for (path in paths) {
                val bytes = freshSize(path) ?: run {
                    val walked = FolderSizer.sizeOf(File(path)) { !isActive }
                    if (!isActive) return@launch
                    sizeCache[path] = walked to System.currentTimeMillis()
                    walked
                }
                _dirSizes.update { it + (path to bytes) }
            }
        }
    }

    /**
     * Recompute the app-storage total (the header's "App storage: X"). A
     * fresh cached value shows immediately; the walk itself is off-thread —
     * it used to run on the UI thread on every recomposition.
     */
    fun refreshStorageUsed() {
        storageJob?.cancel()
        freshSize(STORAGE_TOTAL_KEY)?.let { _storageUsed.value = it }
        storageJob = sizeScope.launch {
            val bytes = freshSize(STORAGE_TOTAL_KEY) ?: run {
                val walked = FolderSizer.sizeOf(appRoot) { !isActive }
                if (!isActive) return@launch
                sizeCache[STORAGE_TOTAL_KEY] = walked to System.currentTimeMillis()
                walked
            }
            _storageUsed.value = bytes
        }
    }

    private fun freshSize(path: String): Long? =
        sizeCache[path]?.takeIf { System.currentTimeMillis() - it.second < SIZE_TTL_MS }?.first

    /** Something under the tree changed (delete / rename): drop the cache and re-size the current listing. */
    private fun refreshSizesAfterChange() {
        sizeCache.clear()
        val dirs = _listing.value.entries.filter { it.isDirectory && it.source == Source.APP }
        requestSizes(dirs)
        refreshStorageUsed()
    }

    /** Blocking total — background callers only (the UI reads [storageUsed]). */
    fun appStorageBytes(): Long = FolderSizer.sizeOf(appRoot)

    private fun isUnderRoot(f: File): Boolean =
        f.absolutePath == appRoot.absolutePath ||
            f.absolutePath.startsWith(appRoot.absolutePath + File.separator)

    private fun mimeOf(name: String): String = when {
        name.endsWith(".mp4", true) || name.endsWith(".m4v", true) -> "video/mp4"
        name.endsWith(".webm", true) -> "video/webm"
        name.endsWith(".mkv", true) -> "video/x-matroska"
        name.endsWith(".ts", true) -> "video/mp2t"
        name.endsWith(".mov", true) -> "video/quicktime"
        name.endsWith(".avi", true) -> "video/x-msvideo"
        name.endsWith(".flv", true) -> "video/x-flv"
        name.endsWith(".mp3", true) -> "audio/mpeg"
        name.endsWith(".m4a", true) || name.endsWith(".aac", true) -> "audio/mp4"
        name.endsWith(".flac", true) -> "audio/flac"
        name.endsWith(".opus", true) -> "audio/opus"
        name.endsWith(".ogg", true) -> "audio/ogg"
        name.endsWith(".wav", true) -> "audio/x-wav"
        name.endsWith(".torrent", true) -> "application/x-bittorrent"
        else -> "application/octet-stream"
    }

    companion object {
        private const val TAG = "CommanderRepository"

        /** Cached folder sizes expire quickly: an active download changes them all the time. */
        private const val SIZE_TTL_MS = 60_000L

        /** Cache key of the app-storage total (a relative name no real path can collide with). */
        private const val STORAGE_TOTAL_KEY = "\u0000storage-total"

        /**
         * What to call a folder the user picked in the system picker. The
         * tree uri's last segment is the document id — `primary:Download/
         * Movies` (`lastPathSegment` is already decoded; decoding it again
         * would mangle a name holding a literal "%") — and that id used to
         * be the root's name, minus its volume. Its last folder name reads
         * as a folder. An opaque id stays as it is.
         */
        fun pickedFolderName(uri: Uri): String {
            val id = uri.lastPathSegment.orEmpty()
            val leaf = id.trimEnd('/', ':').substringAfterLast('/')
            val name = leaf.substringAfterLast(':')
            return name.ifBlank { leaf }.take(40).ifBlank { "folder" }
        }
    }
}

/** The tree segment ("tree/<id>") of a document/tree uri, or null. */
private fun treeIdOf(uri: Uri): String? {
    val segs = uri.pathSegments ?: return null
    return if (segs.size >= 2 && segs[0] == "tree") "tree/${segs[1]}" else null
}

/** Root DOCUMENT uri of a picked tree (getDocumentId works on this one). */
private fun rootDocUri(treeUri: Uri): Uri =
    DocumentsContract.buildDocumentUriUsingTree(
        treeUri,
        DocumentsContract.getTreeDocumentId(treeUri),
    )
