package space.deepflux.android.engine

import android.util.Log
import org.libtorrent4j.AlertListener
import org.libtorrent4j.Sha1Hash
import org.libtorrent4j.SessionManager
import org.libtorrent4j.SessionParams
import org.libtorrent4j.SettingsPack
import org.libtorrent4j.TorrentInfo
import org.libtorrent4j.TorrentStatus
import org.libtorrent4j.alerts.Alert
import java.io.File

/**
 * Thin wrapper over libtorrent4j's [SessionManager] — the Android twin of
 * the desktop's engine/torrent_engine.py queue/facade design. Every method
 * is invoked from the repository's single-threaded dispatcher only (the
 * desktop achieves the same with a command queue), so the session is never
 * touched concurrently.
 *
 * Same libtorrent core as the desktop app; the surface here is exactly what
 * the desktop engine exposes: magnet + .torrent add, per-torrent and
 * session pause/resume, sequential download, remove, status snapshots.
 */
class TorrentEngine {

    private val session = SessionManager()

    @Volatile
    var listener: AlertListener? = null

    private val forwardingListener = object : AlertListener {
        override fun types(): IntArray? = null // null = receive every alert type
        override fun alert(alert: Alert<*>) {
            listener?.alert(alert)
        }
    }

    val isRunning: Boolean
        get() = runCatching { session.isRunning }.getOrDefault(false)

    fun start() {
        if (isRunning) return
        // mmap DISABLED: libtorrent 2.x memory-maps torrent files, and mmap on
        // Android's scoped-storage FUSE layer crashes the system MediaProvider
        // on large sparse files (verified live on an API 36 emulator, 2026-11-12:
        // libfuse_jni NodeTracker abort — TWICE; cutoff=0 did NOT disable it,
        // semantics are "files SMALLER than the cutoff are mapped", so 1 = map
        // nothing, forcing the posix disk I/O path).
        val settings = SettingsPack()
        settings.setInteger(
            org.libtorrent4j.swig.settings_pack.int_types.mmap_file_size_cutoff.swigValue(),
            1,
        )
        session.start(SessionParams(settings))
        session.addListener(forwardingListener)
        Log.i(TAG, "libtorrent session started (${libtorrentVersion()}), mmap disabled")
    }

    fun stop() {
        runCatching { session.stop() }
            .onFailure { Log.w(TAG, "session stop: ${it.message}") }
    }

    /**
     * Absolute path of the first playable video file inside the torrent
     * (null before metadata lands, or when there is no video at all).
     * Sequenced downloads fill the head first, so playback can start early.
     */
    fun firstVideoFile(infoHashHex: String, saveDir: File): String? {
        return runCatching {
            val handle = session.find(Sha1Hash.parseHex(infoHashHex)) ?: return null
            val info = handle.torrentFile() ?: return null
            val files = info.files()
            for (i in 0 until files.numFiles()) {
                val rel = files.filePath(i) ?: continue
                if (VIDEO_EXTENSIONS.any { rel.endsWith(it, ignoreCase = true) }) {
                    return File(saveDir, rel).absolutePath
                }
            }
            null
        }.getOrNull()
    }

    fun libtorrentVersion(): String =
        runCatching { org.libtorrent4j.LibTorrent.version() }.getOrDefault("unknown")

    /** Adds a magnet (fetches metadata via DHT). Returns the v1 info-hash, or null for an invalid magnet. */
    fun addMagnet(magnet: String, saveDir: File): String? {
        val hash = MagnetParser.infoHashHex(magnet) ?: return null
        // 3-arg overload verified against the 2.1.0-39 sources: flags is
        // or_'d onto libtorrent's defaults — an empty flags object = defaults.
        // (Passing null would NPE inside the swig call.)
        runCatching {
            if (session.find(Sha1Hash.parseHex(hash)) == null) {
                saveDir.mkdirs()
                session.download(magnet, saveDir, org.libtorrent4j.swig.torrent_flags_t())
            }
        }.onFailure { Log.w(TAG, "addMagnet: ${it.message}"); return null }
        return hash
    }

    /** Adds a local .torrent file. Returns the v1 info-hash, or null if unparseable. */
    fun addTorrentFile(path: String, saveDir: File): String? {
        val info = runCatching { TorrentInfo(File(path)) }.getOrNull() ?: return null
        val hash = info.infoHash().toHex().lowercase()
        runCatching {
            if (session.find(Sha1Hash.parseHex(hash)) == null) {
                saveDir.mkdirs()
                session.download(info, saveDir)
            }
        }.onFailure { Log.w(TAG, "addTorrentFile: ${it.message}"); return null }
        return hash
    }

    /**
     * Applies whole-torrent sequential piece order. No `setSequentialDownload`
     * exists in libtorrent4j — the closest is `setSequentialRange(n)`, so we
     * pass the full piece count. Returns false while metadata is still
     * pending (no piece count yet); the repository retries on each poll.
     */
    fun applySequential(infoHashHex: String): Boolean {
        val handle = runCatching { session.find(Sha1Hash.parseHex(infoHashHex)) }.getOrNull() ?: return false
        return runCatching {
            val st = handle.status()
            if (st.state() == TorrentStatus.State.DOWNLOADING_METADATA) return false
            val pieces = st.numPieces()
            if (pieces <= 0) return false
            handle.setSequentialRange(pieces)
            true
        }.getOrDefault(false)
    }

    fun pauseTorrent(infoHashHex: String) {
        runCatching { session.find(Sha1Hash.parseHex(infoHashHex))?.pause() }
            .onFailure { Log.w(TAG, "pause: ${it.message}") }
    }

    fun resumeTorrent(infoHashHex: String) {
        runCatching { session.find(Sha1Hash.parseHex(infoHashHex))?.resume() }
            .onFailure { Log.w(TAG, "resume: ${it.message}") }
    }

    fun removeTorrent(infoHashHex: String) {
        runCatching {
            val handle = session.find(Sha1Hash.parseHex(infoHashHex)) ?: return
            session.remove(handle)
        }.onFailure { Log.w(TAG, "remove: ${it.message}") }
    }

    /** Whole-session pause — used by the Wi-Fi/charging gates. */
    fun pauseSession() {
        runCatching { session.pause() }.onFailure { Log.w(TAG, "session pause: ${it.message}") }
    }

    fun resumeSession() {
        runCatching { session.resume() }.onFailure { Log.w(TAG, "session resume: ${it.message}") }
    }

    /**
     * Status snapshot for a set of info-hashes. Dead/removed handles yield
     * null — the repository prunes them. Each read is individually guarded:
     * a handle dying mid-poll must never kill the poller.
     */
    fun snapshot(infoHashes: List<String>): List<Pair<String, TorrentStatus?>> =
        infoHashes.map { hex ->
            hex to runCatching {
                session.find(Sha1Hash.parseHex(hex))?.status()
            }.getOrNull()
        }

    /** Tracker announce URLs for one torrent (agent diagnostics, §13 #10). */
    fun trackerUrls(infoHashHex: String): List<String> =
        runCatching {
            session.find(Sha1Hash.parseHex(infoHashHex))
                ?.trackers()
                ?.mapNotNull { it.url() }
                ?.filter { it.isNotBlank() }
                ?: emptyList()
        }.getOrDefault(emptyList())

    private companion object {
        const val TAG = "TorrentEngine"
        // Matches the A4-matrix-verified playable set (2026-11-14).
        val VIDEO_EXTENSIONS = listOf(".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".ts", ".flv", ".3gp")
    }
}
