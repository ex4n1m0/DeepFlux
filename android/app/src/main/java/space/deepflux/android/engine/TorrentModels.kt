package space.deepflux.android.engine

/**
 * UI-facing snapshot of one managed torrent. The Android twin of the
 * desktop's TorrentRecord/engine-state concepts (engine/torrent_engine.py)
 * — deliberately Qt/Android-free data so the engine layer stays testable.
 */
data class TorrentItem(
    val infoHash: String,
    val name: String,
    val magnetUri: String,
    val sequential: Boolean,
    val addedAt: Long,
    val state: TorrentState,
    /** 0..1 */
    val progress: Float,
    val totalBytes: Long,
    val doneBytes: Long,
    /** B/s */
    val downloadRate: Long,
    val uploadRate: Long,
    val peers: Int,
    val seeds: Int,
    val errorMessage: String = "",
    /** Absolute path of the first video file once metadata is known (v0.2 streaming). */
    val firstVideoPath: String = "",
)

enum class TorrentState {
    CHECKING,
    METADATA,
    DOWNLOADING,
    FINISHED,
    SEEDING,
    PAUSED,
    ERROR,
    UNKNOWN,
}

/** True while the torrent still needs engine time (drives the FGS + gates). */
val TorrentItem.isActive: Boolean
    get() = state == TorrentState.DOWNLOADING ||
        state == TorrentState.METADATA ||
        state == TorrentState.CHECKING
