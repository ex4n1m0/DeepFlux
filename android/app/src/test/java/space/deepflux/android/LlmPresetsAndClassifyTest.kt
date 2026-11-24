package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.agent.LlmPresets
import space.deepflux.android.iptv.Classify

/** Tier 2 (2026-11-23): provider presets + reasoning effort (desktop config.py / agent/llm.py twin). */
class LlmPresetsTest {

    @Test
    fun defaultsAreTheBuiltInVeniceAgentAtLowEffort() {
        val e = LlmPresets.resolve(null, "", "", "")
        assertEquals("venice", e.providerId)
        assertEquals("https://api.venice.ai/api/v1", e.baseUrl)
        assertEquals("deepseek-v4-1-flash-uncensored", e.model)
        assertEquals("low", e.effort)
    }

    @Test
    fun eachProviderTalksToItsOwnEndpointWithItsOwnModel() {
        val d = LlmPresets.resolve("deepseek", "", "", "low")
        assertEquals("https://api.deepseek.com", d.baseUrl)
        assertEquals("deepseek-flash", d.model)
        assertEquals("low", d.effort)

        val o = LlmPresets.resolve("openrouter", "", "", "max")
        assertEquals("https://openrouter.ai/api/v1", o.baseUrl)
        assertEquals("deepseek/deepseek-v4.1-flash", o.model)
        assertEquals("max is spelled xhigh on OpenRouter", "xhigh", o.effort)
    }

    @Test
    fun levelsTheProviderDoesNotHaveAreMapped() {
        assertEquals("Venice has no medium", "high", LlmPresets.resolve("venice", "", "", "medium").effort)
        assertEquals("none", LlmPresets.resolve("venice", "", "", "none").effort)
        assertEquals("max", LlmPresets.resolve("venice", "", "", "max").effort)
    }

    @Test
    fun unknownLevelsAndProvidersFallBackToTheDefaults() {
        assertEquals("low", LlmPresets.resolve("venice", "", "", "sideways").effort)
        assertEquals("venice", LlmPresets.resolve("no-such-provider", "", "", "low").providerId)
    }

    @Test
    fun openrouterBareModelNamesGetTheirVendorSlug() {
        assertEquals("deepseek/deepseek-v4.1-flash", LlmPresets.resolve("openrouter", "", "deepseek-flash", "low").model)
        assertEquals("deepseek/deepseek-v4-pro", LlmPresets.resolve("openrouter", "", "deepseek-v4-pro", "low").model)
        assertEquals("deepseek/other", LlmPresets.resolve("openrouter", "", "other", "low").model)
        assertEquals("a slug is never prefixed twice", "vendor/model", LlmPresets.resolve("openrouter", "", "vendor/model", "low").model)
        assertEquals("other providers keep the name as typed", "deepseek-flash", LlmPresets.resolve("deepseek", "", "deepseek-flash", "low").model)
    }

    @Test
    fun customEndpointsNeedABaseUrlAndChooseWhetherToSendEffort() {
        assertThrows(IllegalArgumentException::class.java) { LlmPresets.resolve("custom", "", "", "low") }
        val silent = LlmPresets.resolve("custom", "http://llm.lan:8000/v1/", "qwen", "high")
        assertEquals("http://llm.lan:8000/v1", silent.baseUrl)
        assertEquals("qwen", silent.model)
        assertNull("by default a custom endpoint is not sent reasoning_effort", silent.effort)
        assertEquals("high", LlmPresets.resolve("custom", "http://x/v1", "m", "high", customSendsEffort = true).effort)
    }

    @Test
    fun theBuiltInKeyOnlyServesTheProviderItBelongsTo() {
        assertTrue(LlmPresets.builtInKeyWorksFor("venice"))
        assertTrue(LlmPresets.builtInKeyWorksFor(null))
        assertTrue(LlmPresets.builtInKeyWorksFor(""))
        for (other in listOf("deepseek", "openrouter", "custom")) {
            assertTrue(other, !LlmPresets.builtInKeyWorksFor(other))
        }
    }
}

/** Tier 2 (2026-11-23): Live / Movies / Series classification — the desktop's iptv/classify.py rules. */
class ClassifyTest {

    @Test
    fun groupKeywordsDecide() {
        assertEquals(Classify.MOVIES, Classify.classifyEntry("Movies HD", "http://x/1.m3u8", "Some Film"))
        assertEquals(Classify.SERIES, Classify.classifyEntry("TV Shows", "http://x/1.m3u8", "Some Show"))
        assertEquals(Classify.LIVE, Classify.classifyEntry("UK Entertainment", "http://x/1.m3u8", "Dave"))
    }

    @Test
    fun urlPatternsAndFileTypesDecide() {
        assertEquals(Classify.MOVIES, Classify.classifyEntry("", "http://x/movie/123.mp4", "Film"))
        assertEquals(Classify.SERIES, Classify.classifyEntry("", "http://x/series/456", "Show"))
        assertEquals(Classify.MOVIES, Classify.classifyEntry("", "http://x/file.MKV", "Film"))
        assertEquals(Classify.LIVE, Classify.classifyEntry("", "http://x/live/1.m3u8", "Channel"))
    }

    @Test
    fun anEpisodeNumberMakesItSeries() {
        assertEquals(Classify.SERIES, Classify.classifyEntry("VOD", "http://x/1.mp4", "Show S01E02"))
        assertEquals(Classify.SERIES, Classify.classifyEntry("", "http://x/series/9", "s02e001"))
        // …unless it is a live-extension stream in a group with no series hint.
        assertEquals(Classify.LIVE, Classify.classifyEntry("", "http://x/live/1.m3u8", "Channel S01E02"))
    }

    @Test
    fun liveChannelNamingBeatsMovieGroups() {
        // 24/7 channels filed under "UK Movies" are not VOD.
        assertEquals(Classify.LIVE, Classify.classifyEntry("UK Movies", "http://x/1.m3u8", "UK: Sky Cinema FHD"))
        assertEquals(Classify.LIVE, Classify.classifyEntry("", "http://x/1.m3u8", "US|ESPN HD"))
        // A release year means it is a film despite the prefix.
        assertEquals(Classify.MOVIES, Classify.classifyEntry("UK Movies", "http://x/1.mp4", "UK: Some Film (2019)"))
    }

    @Test
    fun aDatedUploadPathIsVideoOnDemandEvenForLiveLookingUrls() {
        // The atsushi444 case: Chinese group names, .m3u8 URLs, dated paths.
        assertEquals(Classify.MOVIES, Classify.classifyEntry("国产视频", "http://x/video/m3u8/2025/09/06/abc/index.m3u8", "某视频"))
        assertEquals(Classify.SERIES, Classify.classifyEntry("国产视频", "http://x/video/m3u8/2025/09/06/abc/index.m3u8", "剧 S02E03"))
        assertEquals(Classify.LIVE, Classify.classifyEntry("国产视频", "http://x/video/m3u8/abc/index.m3u8", "某视频"))
    }

    @Test
    fun thePerSourceOverrideWins() {
        val group = "UK Movies"
        val url = "http://x/live/1.m3u8"
        assertEquals(Classify.LIVE, Classify.section(group, url, "Film 2019", forced = "live"))
        assertEquals(Classify.MOVIES, Classify.section("Anything", url, "Some title", forced = "vod"))
        assertEquals(Classify.SERIES, Classify.section("Anything", url, "Title S01E01", forced = "VOD"))
        assertEquals(Classify.MOVIES, Classify.section(group, url, "Film 2019", forced = "auto"))
        assertEquals("an unknown value runs the heuristics", Classify.LIVE, Classify.section(group, url, "UK: Sky Cinema FHD", forced = "whatever"))
    }

    @Test
    fun extensionsIgnoreTheQueryString() {
        assertEquals(".m3u8", Classify.extensionOf("http://x/a/b.M3U8?token=abc"))
        assertEquals(".mkv", Classify.extensionOf("http://x/a/b.mkv#t=1"))
        assertEquals("", Classify.extensionOf("http://x/a/b"))
        assertEquals("", Classify.extensionOf("http://x/a.b/c"))
        assertEquals(Classify.MOVIES, Classify.classifyEntry("", "http://x/a/b.mkv?token=1", "Film"))
    }
}
