package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.data.AppUpdater

/**
 * P8 (1.4.5): the updater's feed parser is the security boundary between
 * the website and the installer — anything malformed must fail CLOSED
 * (null), never half-apply (desktop parse_feed stance). Robolectric supplies
 * the REAL org.json (the unit-test stub throws "not mocked").
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class AppUpdaterTest {

    private val validFeed = """
        {
          "version": "1.4.5",
          "versionCode": 21,
          "file": "DeepFlux-1.4.5-Android.apk",
          "size": 86406067,
          "sha256": "5d12928869b8f7eb8e6cb964dd55b01f4877d37cc0d34403c8226f55fc293f79",
          "released": "2026-11-16",
          "notes": "test notes"
        }
    """.trimIndent()

    private fun validJson(mutate: (org.json.JSONObject) -> Unit): String {
        val obj = org.json.JSONObject(validFeed)
        mutate(obj)
        return obj.toString()
    }

    @Test
    fun parsesValidFeed() {
        val info = AppUpdater.parseFeed(validFeed)
        assertNotNull(info)
        info!!
        assertEquals("1.4.5", info.version)
        assertEquals(21, info.versionCode)
        assertEquals("DeepFlux-1.4.5-Android.apk", info.fileName)
        assertEquals(86406067L, info.size)
        assertEquals("5d12928869b8f7eb8e6cb964dd55b01f4877d37cc0d34403c8226f55fc293f79", info.sha256)
        assertEquals("2026-11-16", info.released)
        assertEquals("test notes", info.notes)
    }

    @Test
    fun versionCodeIsOptionalFallsBackToZero() {
        val info = AppUpdater.parseFeed(validJson { it.remove("versionCode") })
        assertNotNull(info)
        assertEquals(0, info!!.versionCode)
    }

    @Test
    fun uppercaseShaIsNormalized() {
        val info = AppUpdater.parseFeed(
            validJson { it.put("sha256", "5D12928869B8F7EB8E6CB964DD55B01F4877D37CC0D34403C8226F55FC293F79") },
        )
        assertEquals("5d12928869b8f7eb8e6cb964dd55b01f4877d37cc0d34403c8226f55fc293f79", info!!.sha256)
    }

    @Test
    fun rejectsTwoComponentVersion() {
        assertNull(AppUpdater.parseFeed(validJson { it.put("version", "1.4") }))
    }

    @Test
    fun rejectsNonBaselinFileName() {
        assertNull(AppUpdater.parseFeed(validJson { it.put("file", "../evil.apk") }))
        assertNull(AppUpdater.parseFeed(validJson { it.put("file", "OtherApp-1.0-Android.apk") }))
        assertNull(AppUpdater.parseFeed(validJson { it.put("file", "DeepFlux-1.4.5-Android.apk.exe") }))
    }

    @Test
    fun rejectsNonHexOrShortSha() {
        assertNull(AppUpdater.parseFeed(validJson { it.put("sha256", "zzz12928869b8f7eb8e6cb964dd55b01f4877d37cc0d34403c8226f55fc293f79") }))
        assertNull(AppUpdater.parseFeed(validJson { it.put("sha256", "5d1292") }))
    }

    @Test
    fun rejectsUnreasonableSizes() {
        assertNull(AppUpdater.parseFeed(validJson { it.put("size", 1024) }))
        assertNull(AppUpdater.parseFeed(validJson { it.put("size", 9_000_000_000L) }))
    }

    @Test
    fun rejectsGarbageAndMissingFields() {
        assertNull(AppUpdater.parseFeed("not json at all"))
        assertNull(AppUpdater.parseFeed("{}"))
        assertNull(AppUpdater.parseFeed(validJson { it.remove("sha256") }))
        assertNull(AppUpdater.parseFeed(validJson { it.remove("file") }))
        assertNull(AppUpdater.parseFeed(validJson { it.remove("version") }))
        assertNull(AppUpdater.parseFeed(validJson { it.remove("size") }))
    }
}
