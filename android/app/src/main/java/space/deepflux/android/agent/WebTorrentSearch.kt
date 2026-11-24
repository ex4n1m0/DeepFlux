package space.deepflux.android.agent

import android.util.Log
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Web-first torrent search (v1.3) — the Android answer to "no local Jackett".
 *
 * Strategy (the desktop's web fallback, agent/tools.py `_search_web_sources`
 * + `_fetch_one`, adapted to keyless Android reality):
 *  1. DuckDuckGo queries ("<q> torrent", "<q> magnet") via the existing
 *     keyless [WebSearchClient].
 *  2. Magnet URIs inside snippets/URLs are picked up directly.
 *  3. Results whose host is a KNOWN public torrent site (the domain list
 *     below — curated from Jackett's public indexer definitions, see
 *     ANDROID_STUDY notes) are fetched (bounded) and their magnet anchors
 *     extracted.
 *
 * No indexer service is run or required — this is pure source discovery.
 */
object WebTorrentSearch {

    data class TorrentHit(
        val title: String,
        val magnetUri: String,
        /** The page the magnet was found on ("" when it came from a snippet). */
        val sourceUrl: String,
    )

    /**
     * Blocking — run on a background thread. Never throws: an empty list
     * means "nothing found / web unreachable" (the caller words the reply).
     */
    fun search(query: String, limit: Int = 8): List<TorrentHit> {
        val clean = query.trim()
        if (clean.isEmpty()) return emptyList()
        val hits = linkedMapOf<String, TorrentHit>() // btih -> hit (dedup)

        // ---- 1+2: DuckDuckGo sweep, magnets from snippets/URLs ----
        // Spacing matters: html.duckduckgo.com serves an anomaly challenge
        // to rapid consecutive queries (measured live 2026-11-15) — 1.2s
        // between variants, and one spaced retry when a variant comes back
        // empty (the challenge page parses as zero results, not an error).
        val pages = mutableListOf<WebSearchClient.WebResult>()
        for ((i, variant) in listOf("$clean torrent", "$clean magnet download").withIndex()) {
            if (i > 0) Thread.sleep(1_200)
            var results = runCatching { WebSearchClient.search(variant, 10) }.getOrElse {
                Log.w(TAG, "ddg '$variant' failed: ${it.message}")
                emptyList()
            }
            if (results.isEmpty()) {
                Thread.sleep(2_500)
                results = runCatching { WebSearchClient.search(variant, 10) }.getOrElse { emptyList() }
            }
            for (r in results) {
                extractMagnets("${r.snippet} ${r.url}", "").forEach { hit ->
                    val hash = btih(hit.magnetUri) ?: return@forEach
                    hits.putIfAbsent(hash, hit.copy(title = hit.title.ifBlank { r.title }.take(140)))
                }
                if (isTorrentSite(r.url)) pages += r
            }
            if (hits.size >= limit) break
        }

        // ---- 3: fetch the top torrent-site pages, pull their magnet anchors ----
        if (hits.size < limit) {
            for (page in pages.distinctBy { hostOf(it.url) }.take(4)) {
                if (hits.size >= limit) break
                runCatching { fetchPage(page.url) }.getOrNull()?.let { html ->
                    for (hit in extractAnchoredMagnets(html, page.url)) {
                        val hash = btih(hit.magnetUri) ?: continue
                        hits.putIfAbsent(hash, hit)
                    }
                }
            }
        }

        // Rank: title relevance first, then arrival order.
        val terms = clean.lowercase().split(Regex("\\s+")).filter { it.length > 2 }
        return hits.values
            .sortedByDescending { h -> terms.count { h.title.lowercase().contains(it) } }
            .take(limit)
    }

    // ------------------------------------------------------------------ internals

    /** Bare magnet URIs found anywhere in a text blob (no title context). */
    private fun extractMagnets(text: String, source: String): List<TorrentHit> {
        val out = mutableListOf<TorrentHit>()
        for (m in MAGNET_RE.findAll(text)) {
            val uri = m.value
            out.add(TorrentHit(displayName(uri) ?: "", uri, source))
            if (out.size >= 6) break
        }
        return out
    }

    /** `<a href="magnet:…">Title</a>` pairs — the torrent-site layout. */
    private fun extractAnchoredMagnets(html: String, source: String): List<TorrentHit> {
        val out = mutableListOf<TorrentHit>()
        for (m in ANCHOR_RE.findAll(html)) {
            val uri = m.groupValues[1]
            val title = m.groupValues[2].trim()
            if (btih(uri) == null) continue
            out.add(TorrentHit(title.ifBlank { displayName(uri) ?: "" }.take(140), withName(uri, title), source))
            if (out.size >= 12) break
        }
        return out
    }

    /** Bounded HTML fetch (desktop FETCH_BODY_LIMIT idea: never buffer a monster). */
    private fun fetchPage(url: String): String? {
        val conn = URL(url).openConnection() as HttpURLConnection
        return try {
            conn.requestMethod = "GET"
            conn.connectTimeout = 8_000
            conn.readTimeout = 15_000
            conn.setRequestProperty("User-Agent", DESKTOP_UA)
            conn.setRequestProperty("Accept-Language", "en-US,en;q=0.8")
            if (conn.responseCode != 200) return null
            val bytes = conn.inputStream.use { src ->
                val buf = ByteArray(16 * 1024)
                val out = java.io.ByteArrayOutputStream()
                while (true) {
                    val n = src.read(buf)
                    if (n < 0) break
                    if (out.size() + n > MAX_PAGE_BYTES) return null // over cap = fail closed
                    out.write(buf, 0, n)
                }
                out.toByteArray()
            }
            String(bytes, Charsets.UTF_8)
        } catch (e: Exception) {
            Log.w(TAG, "page fetch failed: ${e.message}")
            null
        } finally {
            conn.disconnect()
        }
    }

    /**
     * Known public torrent domains — insight from Jackett's public indexer
     * definitions (which sites exist, which stay up) reduced to a browse
     * list. Suffix-matched so mirrors/subdomains count.
     */
    private val TORRENT_DOMAINS = listOf(
        "1337x.to", "1337x.st", "x1337x.ws",
        "eztv.re", "eztv.tf", "eztv.yt",
        "thepiratebay.org",
        "kickasstorrents.to", "katcr.to",
        "torrentgalaxy.to", "torrentgalaxy.tf",
        "rutracker.org", "rutracker.net",
        "nyaa.si",
        "limetorrents.info", "limetorrents.co",
        "bitsearch.to", "bt4gprx.com", "bt4g.org",
        "torrentdownloads.info", "yourbittorrent.com",
        "torrentfunk.com", "idope.se",
    )

    private fun isTorrentSite(url: String): Boolean {
        val host = hostOf(url) ?: return false
        return TORRENT_DOMAINS.any { host == it || host.endsWith(".$it") }
    }

    private fun hostOf(url: String): String? =
        runCatching { URL(url).host.lowercase() }.getOrNull()?.takeIf { it.isNotBlank() }

    private fun btih(magnet: String): String? =
        BTIH_RE.find(magnet)?.groupValues?.get(1)?.lowercase()

    /** `dn=` display name from a magnet, URL-decoded. */
    private fun displayName(magnet: String): String? =
        Regex("dn=([^&]+)").find(magnet)?.groupValues?.get(1)?.let {
            // A stray "%" in a name is not an escape: the decoder throws on it.
            runCatching { java.net.URLDecoder.decode(it, "UTF-8") }.getOrNull()
        }

    /** Ensure the magnet carries the anchor's title as dn= (nicer download rows). */
    private fun withName(magnet: String, title: String): String =
        if (title.isBlank() || magnet.contains("dn=")) magnet
        else magnet + "&dn=" + URLEncoder.encode(title.take(120), "UTF-8")

    private val MAGNET_RE = Regex("magnet:\\?xt=urn:btih:[a-zA-Z0-9]+[^\\s\"'<>\\])]*")
    private val BTIH_RE = Regex("urn:btih:([a-zA-Z0-9]+)")
    private val ANCHOR_RE = Regex(
        "<a[^>]*href=\"(magnet:\\?xt=urn:btih:[a-zA-Z0-9]+[^\"]*)\"[^>]*>(.*?)</a>",
        RegexOption.DOT_MATCHES_ALL,
    )

    private const val TAG = "WebTorrentSearch"
    private const val MAX_PAGE_BYTES = 1_500_000
    private const val DESKTOP_UA =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
