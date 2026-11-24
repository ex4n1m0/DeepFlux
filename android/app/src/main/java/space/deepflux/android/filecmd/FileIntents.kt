package space.deepflux.android.filecmd

import android.content.Intent
import android.net.Uri
import android.util.Log
import android.webkit.MimeTypeMap

/**
 * "Open with" and "Share" for one of the app's files. The Commander has
 * always offered them; the Downloads tab uses them too (1.4.14): a finished
 * download the app cannot play itself — a PDF, an APK, an archive — used to
 * have no way to be opened from there.
 */
object FileIntents {

    /**
     * The type to open [name] with: from its own extension through the
     * system's table (".pdf" → application/pdf, ".apk" → what the installer
     * listens for). The catch-all type when the table does not know it —
     * the chooser then offers every app.
     */
    fun mimeOf(name: String): String {
        val extension = name.substringAfterLast('.', "").lowercase()
        val plain = extension.isNotEmpty() && extension.length <= 16 && extension.all { it.isLetterOrDigit() }
        val known = if (plain) MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension) else null
        return known ?: "*/*"
    }

    /** "Open with" — whatever app the user picks. Null when the intent could not be built. */
    fun open(name: String, uri: Uri, mime: String?): Intent? = runCatching {
        Intent(Intent.ACTION_VIEW, uri).apply {
            setDataAndType(uri, mime?.ifBlank { null } ?: "*/*")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
    }.onFailure { Log.w(TAG, "open with $name: ${it.message}") }.getOrNull()

    /** The system's share sheet for one file. Null when the intent could not be built. */
    fun share(name: String, uri: Uri, mime: String?): Intent? = runCatching {
        val send = Intent(Intent.ACTION_SEND)
        send.setType(mime?.ifBlank { null } ?: "*/*")
        send.putExtra(Intent.EXTRA_STREAM, uri)
        send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        Intent.createChooser(send, name)
    }.onFailure { Log.w(TAG, "share $name: ${it.message}") }.getOrNull()

    private const val TAG = "FileIntents"
}
