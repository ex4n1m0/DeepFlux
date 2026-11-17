package space.deepflux.android.agent

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Keyless web search via DuckDuckGo's HTML interface — the Android twin
 * of the desktop's WebSearchClient (ddgs package). Returns top results
 * (title + URL + snippet) without any API key.
 */
object WebSearchClient {

    data class WebResult(val title: String, val url: String, val snippet: String)

    /**
     * Searches DuckDuckGo. Blocking — run on a background thread.
     * Parses the HTML results page with regex (no JSoup dependency;
     * the desktop uses the ddgs package, we parse the same HTML).
     */
    fun search(query: String, limit: Int = 5): List<WebResult> {
        val q = URLEncoder.encode(query, "UTF-8")
        val conn = URL("https://html.duckduckgo.com/html/?q=$q").openConnection() as HttpURLConnection
        return try {
            conn.requestMethod = "GET"
            conn.connectTimeout = 8_000
            conn.readTimeout = 15_000
            conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Android; Mobile)")

            if (conn.responseCode != 200) {
                throw IllegalStateException("DuckDuckGo HTTP ${conn.responseCode}")
            }

            val html = conn.inputStream.bufferedReader().use { it.readText() }
            parseResults(html, limit)
        } catch (e: Exception) {
            Log.w(TAG, "web search failed: ${e.message}")
            throw IllegalStateException("Web search failed: ${e.message?.take(120)}")
        } finally {
            conn.disconnect()
        }
    }

    private fun parseResults(html: String, limit: Int): List<WebResult> {
        val results = mutableListOf<WebResult>()
        // DuckDuckGo HTML results: <a rel="nofollow" class="result__a" href="URL">Title</a>
        // and <a class="result__snippet" ...>Snippet</a>
        val linkRegex = Regex(
            """<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>""",
            RegexOption.DOT_MATCHES_ALL
        )
        val snippetRegex = Regex(
            """<a[^>]*class="result__snippet"[^>]*>(.*?)</a>""",
            RegexOption.DOT_MATCHES_ALL
        )

        val links = linkRegex.findAll(html).toList()
        val snippets = snippetRegex.findAll(html).toList()

        for (i in links.indices) {
            if (results.size >= limit) break
            val url = cleanUrl(links[i].groupValues[1])
            val title = stripTags(links[i].groupValues[2]).trim()
            val snippet = if (i < snippets.size) stripTags(snippets[i].groupValues[1]).trim() else ""
            if (title.isNotBlank() && url.isNotBlank()) {
                results.add(WebResult(title, url, snippet))
            }
        }
        return results
    }

    /** DuckDuckGo wraps URLs in a redirect; extract the actual URL. */
    private fun cleanUrl(raw: String): String {
        // uddg=<encoded_url>
        val uddg = Regex("uddg=([^&]*)").find(raw)?.groupValues?.get(1)
        if (uddg != null) {
            return java.net.URLDecoder.decode(uddg, "UTF-8")
        }
        return raw
    }

    private fun stripTags(html: String): String =
        html.replace(Regex("<[^>]*>"), "").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"")
            .replace("&#x27;", "'").replace("&nbsp;", " ")

    private const val TAG = "WebSearchClient"
}
