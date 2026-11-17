package space.deepflux.android.util

/**
 * ONE source of truth for "can the in-app player open this file name" —
 * previously four copies of this regex drifted apart (DownloadsScreen,
 * CommanderScreen, ChatSession, TorrentEngine's video list). The set is the
 * A4 format matrix's verified-playable list (2026-11-14).
 */
object Playables {
    val EXT = Regex(
        "\\.(mp4|m4v|webm|mkv|ts|mov|avi|flv|3gp|mp3|m4a|flac|aac|ogg|opus|wav)$",
        RegexOption.IGNORE_CASE,
    )

    fun isPlayable(fileName: String): Boolean = EXT.containsMatchIn(fileName)
}
