package space.deepflux.android.engine

import org.libtorrent4j.AlertListener
import org.libtorrent4j.TorrentStatus
import java.io.File

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

    /** Adds a local .torrent file. Returns the v1 info-hash, or null if unparseable — or if it carries a symbolic link. */
    fun addTorrentFile(path: String, saveDir: File): String?

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
