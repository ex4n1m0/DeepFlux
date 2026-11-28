package space.deepflux.android.engine

import android.util.Log
import org.libtorrent4j.AlertListener
import org.libtorrent4j.FileStorage
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
 * Implements [TorrentEngineApi] — the seam the repository is tested through.
 */
class TorrentEngine : TorrentEngineApi {

    private val session = SessionManager()

    @Volatile
    override var listener: AlertListener? = null

    private val forwardingListener = object : AlertListener {
        override fun types(): IntArray? = null // null = receive every alert type
        override fun alert(alert: Alert<*>) {
            listener?.alert(alert)
        }
    }

    override val isRunning: Boolean
        get() = runCatching { session.isRunning }.getOrDefault(false)

    override fun start() {
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

    override fun stop() {
        runCatching { session.stop() }
            .onFailure { Log.w(TAG, "session stop: ${it.message}") }
    }

    /**
     * Absolute path of the first playable video file inside the torrent
     * (null before metadata lands, or when there is no video at all).
     * Sequenced downloads fill the head first, so playback can start early.
     */
    override fun firstVideoFile(infoHashHex: String, saveDir: File): String? {
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

    override fun libtorrentVersion(): String =
        runCatching { org.libtorrent4j.LibTorrent.version() }.getOrDefault("unknown")

    /**
     * Adds a magnet (fetches metadata via DHT). Returns the torrent's name in this app, or null
     * for an invalid magnet.
     *
     * The name is 40 hex — of a v2-only magnet's 64-hex `btih` its first 20 bytes — which is
     * the name the engine reports the torrent by: the library's own lookup after parsing a
     * magnet is by that same 20-byte "best" name (read from the 2.1.0 binding's bytecode), so
     * `find` below sees the torrent an earlier add left, whichever form the magnet took.
     */
    override fun addMagnet(magnet: String, saveDir: File): String? {
        val hash = MagnetParser.infoHashHex(magnet) ?: return null
        // 3-arg overload verified against the 2.1.0-39 sources: flags is
        // or_'d onto libtorrent's defaults — an empty flags object = defaults.
        // (Passing null would NPE inside the swig call.)
        // The magnet is handed over in the spelling libtorrent parses (MagnetParser.forEngine):
        // as the user wrote a v2-only one, the engine answered "invalid info-hash" (emulator).
        runCatching {
            if (session.find(Sha1Hash.parseHex(hash)) == null) {
                saveDir.mkdirs()
                session.download(MagnetParser.forEngine(magnet), saveDir, org.libtorrent4j.swig.torrent_flags_t())
            }
        }.onFailure { Log.w(TAG, "addMagnet: ${it.message}"); return null }
        return hash
    }

    /** Adds a local .torrent file — see [TorrentFileAdd]. */
    override fun addTorrentFile(path: String, saveDir: File): TorrentFileAdd {
        val info = runCatching { TorrentInfo(File(path)) }.getOrNull() ?: return TorrentFileAdd.NotATorrent
        if (hasSymlinkEntries(info)) {
            Log.w(TAG, "addTorrentFile: refused, the torrent contains symbolic links")
            return TorrentFileAdd.HasSymbolicLinks
        }
        val hash = info.infoHash().toHex().lowercase()
        runCatching {
            if (session.find(Sha1Hash.parseHex(hash)) == null) {
                saveDir.mkdirs()
                session.download(info, saveDir)
            }
        }.onFailure { Log.w(TAG, "addTorrentFile: ${it.message}"); return TorrentFileAdd.Failed(it.message) }
        return TorrentFileAdd.Added(hash, runCatching { info.name() }.getOrNull().orEmpty(), v1Hash(info), v2Hash(info))
    }

    /**
     * The torrent's WHOLE v2 info-hash (64 hex), or null without one. The engine's own name for
     * the torrent ([TorrentFileAdd.Added.infoHash]) is only its first 20 bytes — never enough
     * to ask for the torrent again.
     */
    private fun v2Hash(info: TorrentInfo): String? = runCatching {
        val hashes = info.swig().info_hashes()
        if (hashes.has_v2()) hashes.getV2().to_hex().lowercase() else null
    }.getOrNull()?.takeIf { it.length == 64 }

    /**
     * True when any file of the torrent is a symbolic link. A torrent entry
     * may carry a link target, and a link planted inside the download folder
     * can point at any file of the app's private storage — the settings, the
     * chat log — which the Commander, Share and the public-Downloads export
     * would then read as if it were a downloaded video. Such torrents are
     * refused: here for a .torrent file, and by the repository when the
     * metadata of a magnet arrives. Whether the engine would create the link
     * on disk was not verified on a device; the refusal does not depend on it.
     */
    override fun hasSymlinkEntries(infoHashHex: String): Boolean {
        val handle = runCatching { session.find(Sha1Hash.parseHex(infoHashHex)) }.getOrNull() ?: return false
        val info = runCatching { handle.torrentFile() }.getOrNull() ?: return false
        return hasSymlinkEntries(info)
    }

    private fun hasSymlinkEntries(info: TorrentInfo): Boolean = runCatching {
        val files = info.files()
        (0 until files.numFiles()).any { i -> files.fileFlags(i).and_(FileStorage.FLAG_SYMLINK).non_zero() }
    }.getOrDefault(false)

    override fun torrentName(infoHashHex: String): String? = runCatching {
        session.find(Sha1Hash.parseHex(infoHashHex))?.torrentFile()?.name()?.takeIf { it.isNotBlank() }
    }.getOrNull()

    override fun filePathsOf(infoHashHex: String): List<String>? = runCatching {
        val info = session.find(Sha1Hash.parseHex(infoHashHex))?.torrentFile() ?: return null
        val files = info.files()
        (0 until files.numFiles()).mapNotNull { files.filePath(it) }
    }.getOrNull()

    override fun saveTorrentFile(infoHashHex: String, to: File): Boolean {
        val handle = runCatching { session.find(Sha1Hash.parseHex(infoHashHex)) }.getOrNull() ?: return false
        val info = runCatching { handle.torrentFile() }.getOrNull() ?: return false // metadata not here yet
        return runCatching {
            // The binding hands the info dict out one byte at a time.
            val section = info.swig().get_info_section()
            val size = section.size()
            if (size <= 0 || size > MAX_KEPT_METADATA_BYTES) return false
            val bytes = ByteArray(size.toInt())
            for (i in bytes.indices) bytes[i] = section.at(i.toLong())
            // Trackers by tier, as the torrent itself would list them: a
            // private torrent finds its peers nowhere else.
            val byTier = LinkedHashMap<Int, MutableList<String>>()
            handle.trackers().forEach { entry ->
                val url = entry.url()?.trim().orEmpty()
                if (url.isNotEmpty()) byTier.getOrPut(entry.tier()) { mutableListOf() }.let { if (url !in it) it.add(url) }
            }
            val webSeeds = handle.urlSeeds().mapNotNull { it.trim().takeIf(String::isNotEmpty) }
            to.parentFile?.mkdirs()
            val tmp = File(to.path + ".tmp")
            try {
                tmp.writeBytes(KeptTorrent.bytes(byTier.values.toList(), webSeeds, bytes))
                // Checked before it counts: the restart reads this very file with this
                // very parser, so nothing is kept that the restart cannot use — the
                // same torrent, and no symbolic link in it. (The first build of this
                // wrote a file that was no torrent; the unit tests were green, since
                // the real engine cannot run in them. Only reading the file back on a
                // device showed it — 2026-11-25. The bytes are made by [KeptTorrent]
                // now, which a test decodes with a decoder of its own.)
                val kept = runCatching { TorrentInfo(tmp) }.getOrNull()
                val sound = kept != null &&
                    !hasSymlinkEntries(kept) &&
                    kept.infoHash().toHex() == info.infoHash().toHex()
                if (!sound) {
                    Log.w(TAG, "could not keep the torrent's metadata: it does not read back as the same torrent")
                    return false
                }
                if (!tmp.renameTo(to)) {
                    Log.w(TAG, "could not keep the torrent's metadata: the file could not be moved into place")
                    return false
                }
            } finally {
                // After a successful move nothing is left; after anything else nothing may be.
                tmp.delete()
            }
            Log.i(TAG, "kept the torrent's metadata as ${to.name}")
            true
        }.onFailure { Log.w(TAG, "could not keep the torrent's metadata: ${it.message}") }.getOrDefault(false)
    }

    override fun addPeers(infoHashHex: String, peers: List<Pair<String, Int>>) {
        val handle = runCatching { session.find(Sha1Hash.parseHex(infoHashHex)) }.getOrNull() ?: return
        peers.forEach { (host, port) ->
            runCatching {
                val endpoint = org.libtorrent4j.swig.tcp_endpoint(org.libtorrent4j.Address.parseIp(host).swig(), port)
                handle.swig().connect_peer(endpoint)
            }.onFailure { Log.w(TAG, "a peer address of the magnet could not be used ($host:$port): ${it.javaClass.simpleName}: ${it.message}") }
        }
    }

    /**
     * The torrent's v1 info-hash — which a magnet may carry under `btih:` just
     * the same as the v2 hash the engine names a hybrid torrent by. Null
     * without one (a v2-only torrent).
     */
    private fun v1Hash(info: TorrentInfo): String? = runCatching {
        val hashes = info.swig().info_hashes()
        if (hashes.has_v1()) hashes.getV1().to_hex().lowercase() else null
    }.getOrNull()?.takeIf { it.any { c -> c != '0' } }

    /**
     * The engine finds a torrent by either of its hashes and names it by one:
     * the v2 one of a hybrid torrent (emulator, 2026-11-25 — the alert's handle
     * said the v2 hash for a torrent whose magnet had carried the v1 one).
     */
    override fun engineHashOf(infoHashHex: String): String? = runCatching {
        session.find(Sha1Hash.parseHex(infoHashHex))?.infoHash()?.toHex()?.lowercase()
    }.getOrNull()

    /**
     * Applies whole-torrent sequential piece order. No `setSequentialDownload`
     * exists in libtorrent4j — the closest is `setSequentialRange(n)`, so we
     * pass the full piece count. Returns false while metadata is still
     * pending (no piece count yet); the repository retries on each poll.
     */
    override fun applySequential(infoHashHex: String): Boolean {
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

    override fun pauseTorrent(infoHashHex: String) {
        runCatching { session.find(Sha1Hash.parseHex(infoHashHex))?.pause() }
            .onFailure { Log.w(TAG, "pause: ${it.message}") }
    }

    override fun resumeTorrent(infoHashHex: String) {
        runCatching { session.find(Sha1Hash.parseHex(infoHashHex))?.resume() }
            .onFailure { Log.w(TAG, "resume: ${it.message}") }
    }

    override fun removeTorrent(infoHashHex: String) {
        runCatching {
            val handle = session.find(Sha1Hash.parseHex(infoHashHex)) ?: return
            session.remove(handle)
        }.onFailure { Log.w(TAG, "remove: ${it.message}") }
    }

    /** Read from the session, by the torrent's own key — as [snapshot] reads a status. */
    override fun hasDuplicateError(infoHashHex: String): Boolean = runCatching {
        val status = session.find(Sha1Hash.parseHex(infoHashHex))?.status() ?: return false
        status.errorCode().value == TorrentEngineApi.DUPLICATE_TORRENT_ERROR
    }.getOrDefault(false)

    /** Whole-session pause — used by the Wi-Fi/charging gates. */
    override fun pauseSession() {
        runCatching { session.pause() }.onFailure { Log.w(TAG, "session pause: ${it.message}") }
    }

    override fun resumeSession() {
        runCatching { session.resume() }.onFailure { Log.w(TAG, "session resume: ${it.message}") }
    }

    /**
     * Status snapshot for a set of info-hashes. Dead/removed handles yield
     * null — the repository prunes them. Each read is individually guarded:
     * a handle dying mid-poll must never kill the poller.
     */
    override fun snapshot(infoHashes: List<String>): List<Pair<String, TorrentStatus?>> =
        infoHashes.map { hex ->
            hex to runCatching {
                session.find(Sha1Hash.parseHex(hex))?.status()
            }.getOrNull()
        }

    /** Tracker announce URLs for one torrent (agent diagnostics, §13 #10). */
    override fun trackerUrls(infoHashHex: String): List<String> =
        runCatching {
            session.find(Sha1Hash.parseHex(infoHashHex))
                ?.trackers()
                ?.mapNotNull { it.url() }
                ?.filter { it.isNotBlank() }
                ?: emptyList()
        }.getOrDefault(emptyList())

    private companion object {
        const val TAG = "TorrentEngine"
        /** Metadata larger than this is not written out (it would hold the engine for seconds); rare. */
        const val MAX_KEPT_METADATA_BYTES = 8L * 1024 * 1024
        // Matches the A4-matrix-verified playable set (2026-11-14).
        val VIDEO_EXTENSIONS = listOf(".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".ts", ".flv", ".3gp")
    }
}
