package space.deepflux.android.engine

import org.libtorrent4j.AlertListener
import org.libtorrent4j.TorrentStatus
import java.io.File

/** What came of adding a .torrent file. */
sealed class TorrentFileAdd {
    /**
     * Added — or already in the session, which comes to the same for the caller.
     * [name] is the torrent's own name (blank only for a nameless info dict —
     * the hash stands in then). [infoHash] is the hash the ENGINE names the
     * torrent by: of the two a hybrid torrent has, its v2 one — else its v1
     * one. [v1Hash] is its v1 info-hash, which a magnet may carry under `btih:`
     * just the same; null without one. The torrent is the entry's when the
     * entry's hash is EITHER of the two.
     */
    class Added(val infoHash: String, val name: String, val v1Hash: String? = null) : TorrentFileAdd()

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

    /**
     * Adds a magnet (fetches metadata via DHT). Returns the magnet's `btih` hash —
     * 40 hex, whichever of a torrent's hashes it carries — or null for an invalid magnet.
     */
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
     * The paths of the torrent's files as the torrent names them
     * ("Season 1/ep1.mkv"); null before the engine knows its files or for no
     * such torrent. Read from the SESSION — never through an alert: an alert is
     * the engine's to free the moment the listener has returned, and its handle
     * is a view into it (the binding wraps that handle without owning it).
     * Reading it later is a use after free (emulator, 2026-11-25: SIGSEGV in
     * torrent_handle.is_valid, at a torrent's finish).
     */
    fun filePathsOf(infoHashHex: String): List<String>?

    /**
     * Writes the torrent's metadata as a .torrent file, with its trackers —
     * once the engine knows it. False when there is nothing to write yet
     * or the write failed; [to] then does not exist.
     */
    fun saveTorrentFile(infoHashHex: String, to: File): Boolean

    /**
     * Asks the engine to try these addresses as peers of the torrent — a magnet's
     * `x.pe=` hints, which a .torrent does not carry: a torrent restored from its
     * kept file has lost them. Only what can be a literal address may be passed
     * ([MagnetParser.peerHints] holds them to that): what the engine cannot make
     * an address of is left and logged.
     */
    fun addPeers(infoHashHex: String, peers: List<Pair<String, Int>>)

    /**
     * The hash the ENGINE names a torrent by, whichever of its hashes found it —
     * for a hybrid torrent its v2 one. An entry is keyed by the magnet's hash,
     * which may be the other one; what comes FROM the engine (its alerts) is this
     * hash. Null when the engine does not know the torrent.
     */
    fun engineHashOf(infoHashHex: String): String?

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
