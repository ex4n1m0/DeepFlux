package space.deepflux.android.dlmgr

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import android.util.Log
import java.io.File
import java.io.FileInputStream

/**
 * Copies completed files to the public Downloads folder via MediaStore —
 * so users can see and share them from any file manager.
 *
 * The Android equivalent of the desktop's "open containing folder":
 * on Android, the app-specific directory is invisible to most file
 * managers, so finished files are copied out. This is the DEFAULT
 * destination; a folder the user chose in Settings is served by
 * [FinishedExport], which calls this when none is chosen.
 */
object MediaStoreExport {

    /**
     * Copies a completed file to the public Downloads collection.
     * Returns the content URI, or null on failure (including Android 9,
     * where the Downloads collection does not exist — lint: NewApi).
     */
    fun exportToDownloads(context: Context, file: File, displayName: String? = null): Uri? {
        if (android.os.Build.VERSION.SDK_INT < 29) return null
        var itemUri: Uri? = null
        return try {
            val name = displayName ?: file.name
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, guessMimeType(name))
                put(MediaStore.Downloads.IS_PENDING, 1)
            }

            val resolver = context.contentResolver
            val collection = MediaStore.Downloads.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
            itemUri = resolver.insert(collection, values) ?: return null

            // Copy the file content
            resolver.openOutputStream(itemUri)?.use { out ->
                FileInputStream(file).use { input ->
                    input.copyTo(out)
                }
            } ?: return null

            // Clear the pending flag
            values.clear()
            values.put(MediaStore.Downloads.IS_PENDING, 0)
            resolver.update(itemUri, values, null, null)

            Log.i(TAG, "exported $name to Downloads: $itemUri")
            itemUri
        } catch (e: Exception) {
            Log.w(TAG, "export failed for ${file.name}: ${e.message}")
            // A row left "pending" is a file no other app sees, and the
            // system forgets it only a week later.
            itemUri?.let { uri -> runCatching { context.contentResolver.delete(uri, null, null) } }
            null
        }
    }

    internal fun guessMimeType(name: String): String = when {
        name.endsWith(".mp4", true) || name.endsWith(".m4v", true) -> "video/mp4"
        name.endsWith(".webm", true) -> "video/webm"
        name.endsWith(".mkv", true) -> "video/x-matroska"
        name.endsWith(".avi", true) -> "video/x-msvideo"
        name.endsWith(".mov", true) -> "video/quicktime"
        name.endsWith(".mp3", true) -> "audio/mpeg"
        name.endsWith(".flac", true) -> "audio/flac"
        name.endsWith(".zip", true) -> "application/zip"
        name.endsWith(".iso", true) -> "application/x-iso9660-image"
        else -> "application/octet-stream"
    }

    private const val TAG = "MediaStoreExport"
}
