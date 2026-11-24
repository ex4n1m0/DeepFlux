package space.deepflux.android.iptv

import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Xtream Codes API client — the Android twin of iptv/xtream.py.
 * Logs in with server URL + username + password, fetches live channels
 * and VOD, and returns M3U-compatible channel lists.
 *
 * The response → channel mapping lives in the companion object as pure
 * functions ([parseLiveStreams] / [parseVodStreams]) so the URL rules are
 * unit-tested without a server.
 */
class XtreamClient(
    private val serverUrl: String,
    private val username: String,
    private val password: String,
) {
    private val baseUrl: String get() = serverUrl.trimEnd('/') + "/player_api.php"

    private fun apiUrl(action: String, extra: Map<String, String> = emptyMap()): String {
        val params = mutableListOf(
            "username=${URLEncoder.encode(username, "UTF-8")}",
            "password=${URLEncoder.encode(password, "UTF-8")}",
        )
        if (action.isNotBlank()) params.add("action=$action")
        for ((k, v) in extra) params.add("$k=${URLEncoder.encode(v, "UTF-8")}")
        return "$baseUrl?${params.joinToString("&")}"
    }

    data class XtreamAuth(
        val auth: Int,
        val status: String,
        val expDate: String,
        val isActive: String,
    )

    data class XtreamChannel(
        val name: String,
        val streamUrl: String,
        val logo: String,
        val categoryId: String,
        val streamType: String, // "live" | "movie" | "series"
    )

    /** Login check. Returns auth info or throws. */
    fun authenticate(): XtreamAuth {
        val json = fetchJson(apiUrl(""))
        val userInfo = json.optJSONObject("user_info") ?: throw IllegalStateException("No user_info in response")
        return XtreamAuth(
            auth = userInfo.optInt("auth", 0),
            status = userInfo.optString("status", ""),
            expDate = userInfo.optString("exp_date", ""),
            isActive = userInfo.optString("is_active", "0"),
        )
    }

    /** Fetch live TV channels. */
    fun getLiveChannels(): List<XtreamChannel> {
        val channels = parseLiveStreams(fetchJson(apiUrl("get_live_streams")), serverUrl, username, password)
        Log.i(TAG, "fetched ${channels.size} live channels")
        return channels
    }

    /** Fetch VOD (movies). */
    fun getVodChannels(): List<XtreamChannel> {
        val channels = parseVodStreams(fetchJson(apiUrl("get_vod_streams")), serverUrl, username, password)
        Log.i(TAG, "fetched ${channels.size} VOD items")
        return channels
    }

    /** Fetch categories for grouping. */
    fun getCategories(streamType: String): Map<String, String> {
        val action = when (streamType) {
            "live" -> "get_live_categories"
            "movie" -> "get_vod_categories"
            else -> return emptyMap()
        }
        return parseCategories(fetchJson(apiUrl(action)))
    }

    /**
     * Convert Xtream channels to our M3U Channel format. With [categoryNames]
     * (id → name, from [getCategories]) the group is the provider's real
     * category name; without it the stream type ("live" | "movie") is kept —
     * one group for everything, the pre-fix behaviour.
     */
    fun toM3UChannels(
        xtreamChannels: List<XtreamChannel>,
        categoryNames: Map<String, String> = emptyMap(),
    ): List<M3UParser.Channel> =
        xtreamChannels.map { xc ->
            M3UParser.Channel(
                name = xc.name,
                url = xc.streamUrl,
                logoUrl = xc.logo,
                groupId = categoryNames[xc.categoryId]?.takeIf { it.isNotBlank() } ?: xc.streamType,
            )
        }

    private fun fetchJson(url: String): JSONObject {
        val conn = URL(url).openConnection() as HttpURLConnection
        return try {
            conn.requestMethod = "GET"
            conn.connectTimeout = 10_000
            conn.readTimeout = 20_000
            val code = conn.responseCode
            if (code != 200) throw IllegalStateException("Xtream HTTP $code")
            val body = conn.inputStream.bufferedReader().use { it.readText() }
            JSONObject(body)
        } finally {
            conn.disconnect()
        }
    }

    companion object {
        private const val TAG = "XtreamClient"

        /**
         * Stream URL id: `stream_id`, NOT `num`. `num` is the channel number
         * — a different value on most panels — and building the URL from it
         * 404s (the desktop builds from stream_id, iptv/xtream.py). `num` is
         * only a last resort for panels that omit stream_id.
         */
        private fun streamId(item: JSONObject): String {
            val id = item.optLong("stream_id", -1L)
            return if (id >= 0) id.toString() else item.optLong("num", 0L).toString()
        }

        /** `/live/<user>/<pass>/<stream_id>.<ext>` — `direct_source` streams are MPEG-TS, the rest HLS. */
        fun liveStreamUrl(serverUrl: String, username: String, password: String, item: JSONObject): String {
            val ext = if (item.optString("direct_source").isNotBlank()) "ts" else "m3u8"
            return "${serverUrl.trimEnd('/')}/live/$username/$password/${streamId(item)}.$ext"
        }

        /** `/movie/<user>/<pass>/<stream_id>.<container_extension>` */
        fun vodStreamUrl(serverUrl: String, username: String, password: String, item: JSONObject): String {
            val ext = item.optString("container_extension").ifBlank { "mp4" }
            return "${serverUrl.trimEnd('/')}/movie/$username/$password/${streamId(item)}.$ext"
        }

        fun parseLiveStreams(json: JSONObject, serverUrl: String, username: String, password: String): List<XtreamChannel> {
            val arr = json.optJSONArray("info") ?: return emptyList()
            val out = ArrayList<XtreamChannel>(arr.length())
            for (i in 0 until arr.length()) {
                val item = arr.optJSONObject(i) ?: continue
                out.add(
                    XtreamChannel(
                        name = item.optString("name", ""),
                        streamUrl = liveStreamUrl(serverUrl, username, password, item),
                        logo = item.optString("stream_icon", ""),
                        categoryId = item.optString("category_id", ""),
                        streamType = "live",
                    )
                )
            }
            return out
        }

        fun parseVodStreams(json: JSONObject, serverUrl: String, username: String, password: String): List<XtreamChannel> {
            val arr = json.optJSONArray("info") ?: return emptyList()
            val out = ArrayList<XtreamChannel>(arr.length())
            for (i in 0 until arr.length()) {
                val item = arr.optJSONObject(i) ?: continue
                out.add(
                    XtreamChannel(
                        name = item.optString("name", "") + " (" + item.optString("rating", "") + ")",
                        streamUrl = vodStreamUrl(serverUrl, username, password, item),
                        logo = item.optString("stream_icon", item.optString("cover", "")),
                        categoryId = item.optString("category_id", ""),
                        streamType = "movie",
                    )
                )
            }
            return out
        }

        fun parseCategories(json: JSONObject): Map<String, String> {
            val arr = json.optJSONArray("info") ?: return emptyMap()
            val result = LinkedHashMap<String, String>()
            for (i in 0 until arr.length()) {
                val item = arr.optJSONObject(i) ?: continue
                result[item.optString("category_id", "")] = item.optString("category_name", "")
            }
            return result
        }
    }
}
