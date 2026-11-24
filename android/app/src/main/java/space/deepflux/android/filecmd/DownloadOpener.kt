package space.deepflux.android.filecmd

import android.content.Context
import android.net.Uri
import androidx.core.content.FileProvider
import java.io.File

/**
 * Opens a finished download the app cannot play itself, in whatever app the
 * user picks. False when that could not even be started — nothing is
 * installed that can take it, or the file is not one this app may hand out —
 * and the caller says so.
 */
class DownloadOpener(
    private val context: Context,
    /** The file's `content://` address through the manifest's FileProvider (a parameter, so this is testable off the device). */
    private val fileUri: (File) -> Uri = { f ->
        FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", f)
    },
) {
    fun open(file: File): Boolean {
        if (!file.isFile) return false // gone meanwhile: there is nothing to hand out
        val uri = runCatching { fileUri(file) }.getOrNull() ?: return false
        val intent = FileIntents.open(file.name, uri, FileIntents.mimeOf(file.name)) ?: return false
        return runCatching { context.startActivity(intent) }.isSuccess
    }
}
