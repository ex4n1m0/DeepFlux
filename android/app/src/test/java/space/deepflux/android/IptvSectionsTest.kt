package space.deepflux.android

import android.app.Application
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.iptv.Classify
import space.deepflux.android.iptv.IPTVRepository
import space.deepflux.android.iptv.LocalSources
import space.deepflux.android.iptv.PlaylistStore
import java.io.File

/**
 * The sections (Live TV / Movies / Series) the Live TV screen publishes with
 * a loaded source:
 *  - a folder of files is never a live channel, whatever it is called;
 *  - the per-source content override decides for a playlist — and without
 *    one the entry rules do.
 * Both run from the source's CACHE, so no network is involved.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class IptvSectionsTest {

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 10_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(25)
        }
    }

    @Test
    fun aFolderOfFilesIsNeverALiveChannel() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        val dir = File(ctx.filesDir, "DeepFlux").apply { mkdirs() }
        File(dir, "Movie A.mp4").writeBytes(byteArrayOf(1))
        File(dir, "Show S01E01.mkv").writeBytes(byteArrayOf(1))
        // A ".ts" with no telling name: the rules alone call it a live stream.
        // (No "UK: …" here — ":" cannot be part of a file name on a Windows host.)
        File(dir, "Holiday clip.ts").writeBytes(byteArrayOf(1))
        val repo = IPTVRepository(context = ctx, downloadsDir = dir)

        repo.select(LocalSources.DOWNLOADS_ID)
        await("the downloads source") { repo.state.value.channels.size == 3 && !repo.state.value.loading }

        assertEquals(listOf(Classify.MOVIES, Classify.MOVIES, Classify.SERIES), repo.state.value.sections.sorted())
    }

    /** Two entries in one cached playlist: an episode and a film, neither under a telling group. */
    private fun cachePlaylist(ctx: Context, id: String) {
        File(ctx.filesDir, "iptv_cache_$id.m3u").writeText(
            "#EXTM3U\n" +
                "#EXTINF:-1 group-title=\"X\",Some Show S01E01\nhttp://x/1.m3u8\n" +
                "#EXTINF:-1 group-title=\"X\",Big Film\nhttp://x/2.m3u8\n",
        )
    }

    private fun repoOver(ctx: Context, id: String, contentType: String): IPTVRepository {
        cachePlaylist(ctx, id)
        // A port nothing listens on: the refresh fails, the cached entries stay.
        PlaylistStore(ctx).add(PlaylistStore.Playlist(id = id, name = "S$id", kind = "m3u", url = "http://127.0.0.1:9/none.m3u", contentType = contentType))
        return IPTVRepository(context = ctx, downloadsDir = File(ctx.filesDir, "DeepFlux"))
    }

    @Test
    fun withoutAnOverrideTheEntryRulesDecide() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        val repo = repoOver(ctx, "auto1", PlaylistStore.CONTENT_AUTO)
        await("the cached playlist") { repo.state.value.channels.size == 2 && !repo.state.value.loading }
        // Both look like live .m3u8 streams to the rules.
        assertEquals(listOf(Classify.LIVE, Classify.LIVE), repo.state.value.sections)
    }

    @Test
    fun theSavedOverrideDecidesForTheWholePlaylist() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        val repo = repoOver(ctx, "vod1", PlaylistStore.CONTENT_VOD)
        await("the cached playlist") { repo.state.value.channels.size == 2 && !repo.state.value.loading }
        assertEquals(
            "every entry is a video; the SxxExx name still makes a series",
            listOf(Classify.SERIES, Classify.MOVIES),
            repo.state.value.sections,
        )
    }

    @Test
    fun switchingToASourceWithoutAnOverrideDropsTheOldSections() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        val repo = repoOver(ctx, "vod2", PlaylistStore.CONTENT_VOD)
        await("the cached playlist") { repo.state.value.channels.size == 2 && !repo.state.value.loading }
        assertEquals(Classify.MOVIES, repo.state.value.sections[1])

        val dir = File(ctx.filesDir, "DeepFlux").apply { mkdirs() }
        File(dir, "Clip.mp4").writeBytes(byteArrayOf(1))
        repo.select(LocalSources.DOWNLOADS_ID)
        await("the downloads source") {
            val s = repo.state.value
            s.playlist?.id == LocalSources.DOWNLOADS_ID && s.channels.size == 1 && !s.loading
        }
        assertEquals("sections always match the channels on screen", listOf(Classify.MOVIES), repo.state.value.sections)
    }
}
