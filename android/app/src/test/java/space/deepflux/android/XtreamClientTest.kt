package space.deepflux.android

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.iptv.XtreamClient

/**
 * Fix batch 2026-11-23: Xtream stream URLs are built from `stream_id`. They
 * used to use `num` (the channel number — a different value on most panels),
 * which 404s. Robolectric supplies the real org.json.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class XtreamClientTest {

    private fun live(num: Int, streamId: Any? = null, direct: String? = null): JSONObject =
        JSONObject().apply {
            put("num", num)
            put("name", "Channel $num")
            put("category_id", "7")
            put("stream_icon", "http://x/logo.png")
            if (streamId != null) put("stream_id", streamId)
            if (direct != null) put("direct_source", direct)
        }

    @Test
    fun liveUrlUsesStreamIdNotNum() {
        val url = XtreamClient.liveStreamUrl("http://s:8080/", "user", "pass", live(num = 3, streamId = 4231))
        assertEquals("http://s:8080/live/user/pass/4231.m3u8", url)
    }

    @Test
    fun directSourceStreamIsMpegTs() {
        val url = XtreamClient.liveStreamUrl("http://s", "u", "p", live(num = 1, streamId = 10, direct = "1"))
        assertEquals("http://s/live/u/p/10.ts", url)
    }

    @Test
    fun streamIdSentAsTextIsAccepted() {
        val url = XtreamClient.liveStreamUrl("http://s", "u", "p", live(num = 1, streamId = "999"))
        assertEquals("http://s/live/u/p/999.m3u8", url)
    }

    @Test
    fun fallsBackToNumOnlyWhenStreamIdIsMissing() {
        val url = XtreamClient.liveStreamUrl("http://s", "u", "p", live(num = 3, streamId = null))
        assertEquals("http://s/live/u/p/3.m3u8", url)
    }

    @Test
    fun vodUrlUsesStreamIdAndContainer() {
        val item = JSONObject().put("num", 5).put("stream_id", 77).put("container_extension", "mkv")
        assertEquals("http://s/movie/u/p/77.mkv", XtreamClient.vodStreamUrl("http://s", "u", "p", item))
    }

    @Test
    fun channelsCarryTheirCategoryAndGroupsUseItsName() {
        val json = JSONObject().put(
            "info",
            org.json.JSONArray().put(live(num = 3, streamId = 4231)).put(
                live(num = 4, streamId = 9).put("category_id", "99"),
            ),
        )
        val channels = XtreamClient.parseLiveStreams(json, "http://s", "u", "p")
        assertEquals(2, channels.size)
        assertEquals("http://s/live/u/p/4231.m3u8", channels[0].streamUrl)
        assertEquals("7", channels[0].categoryId)

        val cats = XtreamClient.parseCategories(
            JSONObject().put("info", org.json.JSONArray().put(JSONObject().put("category_id", "7").put("category_name", "News"))),
        )
        assertEquals(mapOf("7" to "News"), cats)

        val client = XtreamClient("http://s", "u", "p")
        val grouped = client.toM3UChannels(channels, cats)
        assertEquals("News", grouped[0].groupId)
        // A channel whose category was not in the map keeps the old single group.
        assertEquals("live", grouped[1].groupId)
        // Without categories at all: the pre-fix behaviour.
        assertEquals("live", client.toM3UChannels(channels)[0].groupId)
    }
}
