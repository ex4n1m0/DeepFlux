package space.deepflux.android.iptv

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Xtream Codes API client — the Android twin of iptv/xtream.py.
 * Logs in with server URL + username + password, fetches live channels
 * and VOD, and returns M3U-compatible channel lists.
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
        val json = fetchJson(apiUrl("get_live_streams"))
        val channels = mutableListOf<XtreamChannel>()
        val arr = json.optJSONArray("info") ?: return emptyList()
        val streamBase = serverUrl.trimEnd('/') + "/live/$username/$password"
        for (i in 0 until arr.length()) {
            val item = arr.optJSONObject(i) ?: continue
            val id = item.optInt("num", 0)
            channels.add(
                XtreamChannel(
                    name = item.optString("name", ""),
                    streamUrl = "$streamBase/$id.m3u8",
                    logo = item.optString("stream_icon", ""),
                    categoryId = item.optString("category_id", ""),
                    streamType = "live",
                )
            )
        }
        Log.i(TAG, "fetched ${channels.size} live channels")
        return channels
    }

    /** Fetch VOD (movies). */
    fun getVodChannels(): List<XtreamChannel> {
        val json = fetchJson(apiUrl("get_vod_streams"))
        val channels = mutableListOf<XtreamChannel>()
        val arr = json.optJSONArray("info") ?: return emptyList()
        val streamBase = serverUrl.trimEnd('/') + "/movie/$username/$password"
        for (i in 0 until arr.length()) {
            val item = arr.optJSONObject(i) ?: continue
            val id = item.optInt("num", 0)
            val ext = item.optString("container_extension", "mp4")
            channels.add(
                XtreamChannel(
                    name = item.optString("name", "") + " (" + item.optString("rating", "") + ")",
                    streamUrl = "$streamBase/$id.$ext",
                    logo = item.optString("stream_icon", item.optString("cover", "")),
                    categoryId = item.optString("category_id", ""),
                    streamType = "movie",
                )
            )
        }
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
        val json = fetchJson(apiUrl(action))
        val result = mutableMapOf<String, String>()
        val arr = json.optJSONArray("info") ?: return emptyMap()
        for (i in 0 until arr.length()) {
            val item = arr.optJSONObject(i) ?: continue
            result[item.optString("category_id", "")] = item.optString("category_name", "")
        }
        return result
    }

    /** Convert Xtream channels to our M3U Channel format. */
    fun toM3UChannels(xtreamChannels: List<XtreamChannel>): List<M3UParser.Channel> =
        xtreamChannels.map { xc ->
            M3UParser.Channel(
                name = xc.name,
                url = xc.streamUrl,
                logoUrl = xc.logo,
                groupId = xc.streamType,
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
    }
}
