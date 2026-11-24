package space.deepflux.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.iptv.M3UParser
import space.deepflux.android.iptv.PlaylistStore
import java.io.File

/**
 * Fix batch 2026-11-23 — playlist persistence and parsing contracts.
 *
 * `local` folder sources used to be re-read as `m3u` after a restart (their
 * content:// url was then fetched as a playlist URL and the source broke).
 * The kind is asserted as its ON-DISK string, not via the code's constants:
 * the sidecar format is the contract.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class PlaylistStoreTest {

    @Test
    fun localFolderKindSurvivesRestart() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        PlaylistStore(ctx).add(
            PlaylistStore.Playlist(
                id = "loc",
                name = "My videos",
                kind = "local",
                url = "content://tree/primary%3AMovies",
            ),
        )
        // A new store = the next app start, reading the same sidecar.
        val restored = PlaylistStore(ctx).playlists.value.single()
        assertEquals("local", restored.kind)
        assertEquals("content://tree/primary%3AMovies", restored.url)
    }

    @Test
    fun xtreamCredentialsSurviveRestart() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        PlaylistStore(ctx).add(
            PlaylistStore.Playlist(
                id = "x",
                name = "Panel",
                kind = "xtream",
                url = "http://panel",
                username = "user",
                password = "secret",
            ),
        )
        val restored = PlaylistStore(ctx).playlists.value.single()
        assertEquals("xtream", restored.kind)
        assertEquals("secret", restored.password)
    }

    @Test
    fun unknownKindFallsBackToM3u() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        File(ctx.filesDir, "iptv_playlists.json").writeText(
            """{"active":"a","playlists":[{"id":"a","name":"Old","kind":"something-else","url":"http://x/list.m3u"}]}""",
        )
        assertEquals("m3u", PlaylistStore(ctx).playlists.value.single().kind)
    }

    @Test
    fun theContentOverrideSurvivesRestartAndAnUnknownValueIsNoOverride() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        PlaylistStore(ctx).add(
            PlaylistStore.Playlist(id = "v", name = "VOD", kind = "m3u", url = "http://x/v.m3u", contentType = "vod"),
        )
        assertEquals("vod", PlaylistStore(ctx).playlists.value.single().contentType)

        // On disk the field is "ct"; a value this build does not know (a
        // newer build's) must not come back as a bogus override.
        File(ctx.filesDir, "iptv_playlists.json").writeText(
            """{"active":"a","playlists":[""" +
                """{"id":"a","name":"A","kind":"m3u","url":"http://x/a.m3u","ct":"vod"},""" +
                """{"id":"b","name":"B","kind":"m3u","url":"http://x/b.m3u","ct":"from-a-newer-build"},""" +
                """{"id":"c","name":"C","kind":"m3u","url":"http://x/c.m3u"}]}""",
        )
        assertEquals(listOf("vod", "auto", "auto"), PlaylistStore(ctx).playlists.value.map { it.contentType })
    }
}

/** EPG linkage attributes + the header's own guide address + list keys. */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class M3UParserTest {

    @Test
    fun parsesEpgAttributesAndTheGuideHeader() {
        val m3u = listOf(
            """#EXTM3U url-tvg="https://guide.example/xmltv"""",
            """#EXTINF:-1 tvg-id="bbc.one" tvg-name="BBC One" tvg-logo="https://x/l.png" group-title="News",BBC One HD""",
            "http://a/b.m3u8",
            """#EXTINF:-1 group-title="News",Plain""",
            "http://a/c.m3u8",
        ).joinToString("\n")
        var guide = ""
        val channels = M3UParser.parseStream(m3u.reader().buffered()) { guide = it }

        assertEquals("https://guide.example/xmltv", guide)
        assertEquals(2, channels.size)
        assertEquals("bbc.one", channels[0].tvgId)
        assertEquals("BBC One", channels[0].tvgName)
        assertEquals("BBC One HD", channels[0].name)
        assertEquals("News", channels[0].groupId)
        // Attributes never leak from one entry into the next.
        assertEquals("", channels[1].tvgId)
        assertEquals("", channels[1].tvgName)
        assertEquals("", channels[1].logoUrl)
    }

    @Test
    fun noGuideHeaderMeansNoCallback() {
        val channels = M3UParser.parseStream("#EXTM3U\n#EXTINF:-1,A\nhttp://a\n".reader().buffered()) {
            throw AssertionError("url-tvg must not be reported")
        }
        assertEquals(1, channels.size)
    }

    @Test
    fun everyAttributeIsReadAndTheVlcUserAgentLineIsCaseBlind() {
        val m3u = listOf(
            """#EXTM3U url-tvg="https://guide.example/xmltv"""",
            """#EXTINF:-1 tvg-id="x" tvg-name="X Name" tvg-logo="https://x/l.png" group-title="Docs" http-user-agent="UA/1",X""",
            "http://a/x",
            """#EXTINF:-1, Y""",
            "#extvlcopt:http-user-agent=UA/2",
            "http://a/y",
        ).joinToString("\n")
        // Pins the attribute readers now that they are compiled once instead of per line.
        val channels = M3UParser.parseStream(m3u.reader().buffered())
        assertEquals("x", channels[0].tvgId)
        assertEquals("X Name", channels[0].tvgName)
        assertEquals("https://x/l.png", channels[0].logoUrl)
        assertEquals("Docs", channels[0].groupId)
        assertEquals("UA/1", channels[0].httpUserAgent)
        assertEquals("UA/2", channels[1].httpUserAgent)
    }

    @Test
    fun listKeysNeverCollideOnRepeatedChannels() {
        val c = M3UParser.Channel(name = "A", url = "http://a", groupId = "G")
        // The same channel listed in two groups / twice: positions differ, keys differ.
        assert(M3UParser.listKey(c, 0) != M3UParser.listKey(c, 1))
        // Stable for the same list position.
        assertEquals(M3UParser.listKey(c, 3), M3UParser.listKey(c.copy(), 3))
    }
}
