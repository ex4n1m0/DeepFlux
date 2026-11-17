package space.deepflux.android.iptv

import android.util.Log

/**
 * Minimal M3U/M3U8 playlist parser — extracts channel entries from the
 * standard #EXTM3U format. The Android twin of iptv/m3u_parser.py.
 *
 * Format:
 * ```
 * #EXTM3U
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
    )

    fun parse(content: String): List<Channel> =
        content.reader().buffered().use { parseStream(it) }

    /**
     * STREAMING parse (1.3.6): commercial IPTV playlists reach 100k+
     * channels / tens of MB — materializing the whole text (then a 2× copy
     * inside readText) OOM-killed the app against the ~200MB heap limit
     * (owner crash 2026-11-15, tv123.me, 131,289 channels). This entry
     * keeps only the parsed Channel list in memory.
     */
    fun parseStream(reader: java.io.BufferedReader): List<Channel> {
        val channels = mutableListOf<Channel>()
        var pendingName = ""
        var pendingLogo = ""
        var pendingGroup = ""
        var pendingUa = ""

        while (true) {
            val line = reader.readLine() ?: break
            val trimmed = line.trim()
            when {
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
                }
                trimmed.startsWith("#EXTGRP:") -> {
                    pendingGroup = trimmed.removePrefix("#EXTGRP:").trim()
                }
                trimmed.uppercase().startsWith("#EXTVLCOPT:HTTP-USER-AGENT=") -> {
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
                            )
                        )
                    }
                    pendingName = ""
                    pendingLogo = ""
                    pendingGroup = ""
                    pendingUa = ""
                }
            }
        }
        Log.i(TAG, "parsed ${channels.size} channels from playlist")
        return channels
    }

    private fun extractAttr(line: String, attr: String): String? {
        val regex = Regex("$attr=\"([^\"]*)\"")
        return regex.find(line)?.groupValues?.get(1)
    }

    private const val TAG = "M3UParser"

    /** Safety cap — the biggest real-world lists are ~150k entries. */
    private const val MAX_CHANNELS = 200_000
}
