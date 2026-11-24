package space.deepflux.android.iptv

import android.util.Log

/**
 * Minimal M3U/M3U8 playlist parser — extracts channel entries from the
 * standard #EXTM3U format. The Android twin of iptv/m3u_parser.py.
 *
 * Format:
 * ```
 * #EXTM3U url-tvg="https://guide.example/xmltv"
 * #EXTINF:-1 tvg-id="id" tvg-name="Name" tvg-logo="url",Display Name
 * http://stream.url.m3u8
 * ```
 */
object M3UParser {

    data class Channel(
        val name: String,
        val url: String,
        val logoUrl: String = "",
        val groupId: String = "",
        /**
         * Per-channel User-Agent required by some CDNs, carried by the
         * playlist itself (EXTINF `http-user-agent="…"` attr or a
         * `#EXTVLCOPT:http-user-agent=…` line). Empty = use the app default.
         * The desktop parser keeps EXTVLCOPT headers too — parity.
         */
        val httpUserAgent: String = "",
        /**
         * EPG linkage from the EXTINF attributes: `tvg-id` is the XMLTV
         * `<channel id>` and `tvg-name` its display name — the reliable way
         * to match a channel to its guide (display names alone often differ:
         * "BBC One HD" vs "BBC One").
         */
        val tvgId: String = "",
        val tvgName: String = "",
    )

    fun parse(content: String): List<Channel> =
        content.reader().buffered().use { parseStream(it) }

    /**
     * STREAMING parse (1.3.6): commercial IPTV playlists reach 100k+
     * channels / tens of MB — materializing the whole text (then a 2× copy
     * inside readText) OOM-killed the app against the ~200MB heap limit
     * (owner crash 2026-11-15, tv123.me, 131,289 channels). This entry
     * keeps only the parsed Channel list in memory.
     *
     * [onGuideUrl] receives the header's `url-tvg="…"` (the playlist's own
     * EPG address) when present — parsed in the same pass, so the guide
     * needs no extra download.
     */
    fun parseStream(reader: java.io.BufferedReader, onGuideUrl: (String) -> Unit = {}): List<Channel> {
        val channels = mutableListOf<Channel>()
        var pendingName = ""
        var pendingLogo = ""
        var pendingGroup = ""
        var pendingUa = ""
        var pendingTvgId = ""
        var pendingTvgName = ""
        var headerSeen = false

        while (true) {
            val line = reader.readLine() ?: break
            val trimmed = line.trim()
            when {
                !headerSeen && trimmed.startsWith("#EXTM3U") -> {
                    headerSeen = true
                    extractAttr(trimmed, "url-tvg")?.takeIf { it.isNotBlank() }?.let(onGuideUrl)
                }
                trimmed.startsWith("#EXTINF:") -> {
                    // Extract display name (after the last comma)
                    val commaIdx = trimmed.lastIndexOf(',')
                    pendingName = if (commaIdx >= 0) {
                        trimmed.substring(commaIdx + 1).trim()
                    } else {
                        trimmed.removePrefix("#EXTINF:").trim()
                    }
                    // Extract attributes
                    pendingLogo = extractAttr(trimmed, "tvg-logo") ?: ""
                    pendingGroup = extractAttr(trimmed, "group-title") ?: ""
                    pendingUa = extractAttr(trimmed, "http-user-agent") ?: ""
                    pendingTvgId = extractAttr(trimmed, "tvg-id") ?: ""
                    pendingTvgName = extractAttr(trimmed, "tvg-name") ?: ""
                }
                trimmed.startsWith("#EXTGRP:") -> {
                    pendingGroup = trimmed.removePrefix("#EXTGRP:").trim()
                }
                trimmed.startsWith("#EXTVLCOPT:HTTP-USER-AGENT=", ignoreCase = true) -> {
                    pendingUa = trimmed.substringAfter('=').trim()
                }
                trimmed.isNotEmpty() && !trimmed.startsWith("#") && pendingName.isNotBlank() -> {
                    if (channels.size < MAX_CHANNELS) {
                        channels.add(
                            Channel(
                                name = pendingName,
                                url = trimmed,
                                logoUrl = pendingLogo,
                                groupId = pendingGroup,
                                httpUserAgent = pendingUa,
                                tvgId = pendingTvgId,
                                tvgName = pendingTvgName,
                            )
                        )
                    }
                    pendingName = ""
                    pendingLogo = ""
                    pendingGroup = ""
                    pendingUa = ""
                    pendingTvgId = ""
                    pendingTvgName = ""
                }
            }
        }
        Log.i(TAG, "parsed ${channels.size} channels from playlist")
        return channels
    }

    /**
     * Lazy-list key for a channel. Playlists list one URL under several
     * names — and one channel (same URL AND name) under several groups;
     * Compose crashes on duplicate keys, so the group and the position are
     * part of the key: unique for any real playlist, stable while the list
     * itself does not change.
     */
    fun listKey(channel: Channel, index: Int): String =
        "${channel.url}|${channel.name}|${channel.groupId}|$index"

    /**
     * Attribute readers, compiled once. `extractAttr` used to build a new
     * Regex for every attribute of every line — five per `#EXTINF`, so
     * about 750,000 compilations for a 150k-channel playlist.
     */
    private val ATTR_REGEXES = java.util.concurrent.ConcurrentHashMap<String, Regex>()

    private fun extractAttr(line: String, attr: String): String? =
        ATTR_REGEXES.getOrPut(attr) { Regex("${Regex.escape(attr)}=\"([^\"]*)\"") }
            .find(line)?.groupValues?.get(1)

    private const val TAG = "M3UParser"

    /** Safety cap — the biggest real-world lists are ~150k entries. */
    private const val MAX_CHANNELS = 200_000
}
