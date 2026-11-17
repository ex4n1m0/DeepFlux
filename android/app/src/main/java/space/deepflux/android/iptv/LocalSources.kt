package space.deepflux.android.iptv

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract

/**
 * Local video folders as Live TV sources (1.3.5) — the Android port of the
 * desktop's `kind: "local_folder"` playlist source (iptv/local_folder.py):
 * every video file under a folder becomes a list entry (grouped by its
 * subfolder), so DOWNLOADED videos show up in the player tab next to the
 * TV channels.
 *
 * Two roots:
 *  * the app's own downloads dir (built-in "Downloads" source, plain files);
 *  * any SAF folder the user adds (DocumentsContract child-document walk —
 *    same contract the Commander uses).
 */
object LocalSources {

    /** Desktop VIDEO_EXTS (iptv/local_folder.py), compacted. */
    private val VIDEO_EXT = Regex(
        "\\.(mp4|m4v|mkv|webm|mov|avi|flv|ts|mpg|mpeg|3gp|ogv|wmv|m2ts)$",
        RegexOption.IGNORE_CASE,
    )

    const val MAX_ENTRIES = 2000
    private const val MAX_DEPTH = 5

    /** The built-in source id (never persisted; recognized by IPTVRepository). */
    const val DOWNLOADS_ID = "__downloads__"

    fun isVideo(name: String): Boolean = VIDEO_EXT.containsMatchIn(name.substringBefore('?'))

    /** Display name: file stem with separators turned into spaces. */
    fun displayName(fileName: String): String =
        fileName.substringBeforeLast('.')
            .replace(Regex("[._]+"), " ")
            .replace(Regex("\\s+"), " ")
            .trim()

    // ------------------------------------------------------------------ app downloads dir (File API)

    fun scanDownloads(root: java.io.File): List<M3UParser.Channel> {
        val out = ArrayList<M3UParser.Channel>()
        fun walk(dir: java.io.File, rel: String, depth: Int) {
            if (out.size >= MAX_ENTRIES || depth > MAX_DEPTH) return
            val files = runCatching { dir.listFiles() }.getOrNull() ?: return
            for (f in files.sortedWith(compareByDescending<java.io.File> { it.isFile }.thenBy { it.name.lowercase() })) {
                if (out.size >= MAX_ENTRIES) return
                if (f.isDirectory) {
                    walk(f, if (rel.isBlank()) f.name else "$rel/${f.name}", depth + 1)
                } else if (isVideo(f.name)) {
                    out.add(M3UParser.Channel(name = displayName(f.name), url = f.absolutePath, groupId = rel))
                }
            }
        }
        walk(root, "", 0)
        return out
    }

    // ------------------------------------------------------------------ SAF tree (DocumentsContract)

    /**
     * Every video under a picked SAF tree, grouped by subfolder path.
     * Blocking — call off the main thread.
     */
    fun scanSafTree(context: Context, treeUri: Uri): List<M3UParser.Channel> {
        val out = ArrayList<M3UParser.Channel>()
        runCatching {
            val resolver = context.contentResolver
            val rootDoc = DocumentsContract.buildDocumentUriUsingTree(
                treeUri,
                DocumentsContract.getTreeDocumentId(treeUri),
            )
            fun queryDir(docUri: Uri, rel: String, depth: Int) {
                if (out.size >= MAX_ENTRIES || depth > MAX_DEPTH) return
                val children = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, DocumentsContract.getDocumentId(docUri))
                resolver.query(
                    children,
                    arrayOf(
                        DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                        DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                        DocumentsContract.Document.COLUMN_MIME_TYPE,
                    ),
                    null, null,
                    "${DocumentsContract.Document.COLUMN_DISPLAY_NAME} ASC",
                )?.use { c ->
                    val iId = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
                    val iName = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
                    val iMime = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_MIME_TYPE)
                    while (c.moveToNext() && out.size < MAX_ENTRIES) {
                        val name = c.getString(iName) ?: continue
                        val mime = c.getString(iMime) ?: ""
                        val docUriChild = DocumentsContract.buildDocumentUriUsingTree(treeUri, c.getString(iId))
                        if (mime == DocumentsContract.Document.MIME_TYPE_DIR) {
                            queryDir(docUriChild, if (rel.isBlank()) name else "$rel/$name", depth + 1)
                        } else if (isVideo(name)) {
                            out.add(M3UParser.Channel(name = displayName(name), url = docUriChild.toString(), groupId = rel))
                        }
                    }
                }
            }
            queryDir(rootDoc, "", 0)
        }.onFailure { android.util.Log.w("LocalSources", "saf scan failed: ${it.message}") }
        return out
    }
}
