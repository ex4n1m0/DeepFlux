package space.deepflux.android.engine

import org.libtorrent4j.AlertListener
import org.libtorrent4j.TorrentStatus
import java.io.File

/** What came of adding a .torrent file. */
sealed class TorrentFileAdd {
    /**
     * Added. [name] is the torrent's own name (blank only for a nameless info
     * dict — the hash stands in then). [v2Prefix] is the first 40 hex of its v2
     * info-hash, which a magnet may carry under `btih:`; null without a v2 hash.
     */
    class Added(val infoHash: String, val name: String, val v2Prefix: String? = null) : TorrentFileAdd()

    /** Not a .torrent the engine can read. */
    object NotATorrent : TorrentFileAdd()

    /** It carries a symbolic link: refused, nothing was created. */
    object HasSymbolicLinks : TorrentFileAdd()

    /** The engine could not add it. */
    class Failed(val reason: String?) : TorrentFileAdd()
}

/**
 * What [TorrentRepository] needs from the BitTorrent engine.
 *
 * [TorrentEngine] is the libtorrent4j implementation. The seam exists for
 * tests: the real engine creates its native session on construction, so
 * nothing owning it can run on the JVM — repository logic (the torrent-add
 * confirmation, the network gates, notifications) is tested against a fake.
 * Every member is invoked from the repository's single-threaded dispatcher.
 */
interface TorrentEngineApi {

    /** Receives every libtorrent alert (null while no one is listening). */
    var listener: AlertListener?

    val isRunning: Boolean

    fun start()
    fun stop()

    /** Adds a magnet (fetches metadata via DHT). Returns the v1 info-hash, or null for an invalid magnet. */
    fun addMagnet(magnet: String, saveDir: File): String?

    /** Adds a local .torrent file — what came of it is a [TorrentFileAdd]. */
    fun addTorrentFile(path: String, saveDir: File): TorrentFileAdd

    /**
     * The torrent's own name once the engine knows its files; null before
     * that (a magnet still fetching its metadata) or for no such torrent.
     * The status snapshot does not carry it: this libtorrent binding fills
     * the name in only when asked, and the app never asked — so a magnet
     * without a `dn=` title stayed "Fetching…" as its name for good.
     */
    fun torrentName(infoHashHex: String): String?

    /**
     * Writes the torrent's metadata as a .torrent file, with its trackers —
     * once the engine knows it. False when there is nothing to write yet
     * or the write failed; [to] then does not exist.
     */
    fun saveTorrentFile(infoHashHex: String, to: File): Boolean

    /**
     * True when any file of the added torrent is a symbolic link (known once
     * its metadata is). Such a torrent is removed again: the link could
     * point at any file of the app's private storage.
     */
    fun hasSymlinkEntries(infoHashHex: String): Boolean

    fun firstVideoFile(infoHashHex: String, saveDir: File): String?

    /** Applies whole-torrent sequential piece order; false while metadata is still pending. */
    fun applySequential(infoHashHex: String): Boolean

    fun pauseTorrent(infoHashHex: String)
    fun resumeTorrent(infoHashHex: String)
    fun removeTorrent(infoHashHex: String)

    /** Whole-session pause — used by the Wi-Fi/charging gates. */
    fun pauseSession()
    fun resumeSession()

    /**
     * Status snapshot for a set of info-hashes. Dead/removed handles yield
     * null — the repository prunes them.
     */
    fun snapshot(infoHashes: List<String>): List<Pair<String, TorrentStatus?>>

    /** Tracker announce URLs for one torrent (agent diagnostics, §13 #10). */
    fun trackerUrls(infoHashHex: String): List<String>

    fun libtorrentVersion(): String
}
