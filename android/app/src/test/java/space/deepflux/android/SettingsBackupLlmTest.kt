package space.deepflux.android

import android.app.Application
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.data.AppSettings
import space.deepflux.android.data.AppPrefs
import space.deepflux.android.data.SettingsBackup
import java.io.File

/**
 * 1.4.16 — the AI provider settings in the encrypted settings export (.dfc): they travel
 * with it, an export made before they existed leaves them alone, and a DESKTOP export
 * brings the provider and the key together or not at all (a key made for one service is
 * no good at another).
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class SettingsBackupLlmTest {

    private fun freshDir(): File =
        File(ApplicationProvider.getApplicationContext<Context>().filesDir, "backup-${System.nanoTime()}").apply { mkdirs() }

    /** What an import would write, in the order it would write it. */
    private fun imported(payload: ByteArray): Map<String, String> {
        val writes = linkedMapOf<String, String>()
        SettingsBackup.applyFilesAndPrefs(payload, freshDir()) { key, value -> writes[key] = value }
        return writes
    }

    private fun androidPayload(prefs: JSONObject = JSONObject()) = JSONObject()
        .put("format", "deepflux-android").put("version", 1).put("prefs", prefs).put("files", JSONObject())
        .toString().toByteArray()

    @Test
    fun anExportCarriesTheAiSettingsAndAnImportWritesThemBack() {
        val mine = AppPrefs(
            llmProvider = "deepseek", customApiKey = "sk-user", llmModel = "m1",
            llmEffort = "high", llmCustomSendsEffort = true, wifiOnly = false,
        )
        val payload = SettingsBackup.buildPayload(mine, freshDir())
        val exported = JSONObject(String(payload, Charsets.UTF_8)).getJSONObject("prefs")
        assertEquals("deepseek", exported.getString("llm_provider"))
        assertEquals("sk-user", exported.getString("custom_api_key"))
        assertEquals("m1", exported.getString("llm_model"))
        assertEquals("high", exported.getString("llm_effort"))
        assertTrue(exported.getBoolean("llm_custom_sends_effort"))

        val writes = imported(payload)
        assertEquals(
            mapOf(
                "custom_api_key" to "sk-user", "llm_provider" to "deepseek", "llm_base_url" to "",
                "llm_model" to "m1", "llm_effort" to "high", "llm_custom_sends_effort" to "true",
            ),
            writes.filterKeys { it.startsWith("llm_") || it == "custom_api_key" },
        )
        assertEquals("the rest still comes along", "false", writes["wifi_only"])
    }

    @Test
    fun anExportMadeBeforeTheseSettingsExistedLeavesThemAlone() {
        val writes = imported(androidPayload(JSONObject().put("wifi_only", true).put("language", "zh")))
        assertFalse(writes.keys.any { it.startsWith("llm_") })
        assertEquals("true", writes["wifi_only"])
        assertEquals("zh", writes["language"])
    }

    @Test
    fun aDesktopExportBringsTheProviderAndTheKeyTogether() {
        val desktop = JSONObject().put(
            "llm",
            JSONObject().put("provider", "openrouter").put("api_key", "sk-desktop")
                .put("base_url", "").put("model", "deepseek-v4-pro").put("reasoning_effort", "max"),
        ).toString().toByteArray()

        val writes = imported(desktop)
        assertEquals("openrouter", writes["llm_provider"])
        assertEquals("sk-desktop", writes["custom_api_key"])
        assertEquals("deepseek-v4-pro", writes["llm_model"])
        assertEquals("max", writes["llm_effort"])
        assertEquals("false", writes["llm_custom_sends_effort"])
    }

    @Test
    fun aDesktopExportWithoutAThinkingLevelGetsTheDefaultRatherThanAnEmptyOne() {
        val desktop = JSONObject().put("llm", JSONObject().put("provider", "venice").put("api_key", "sk-v")).toString().toByteArray()
        assertEquals("low", imported(desktop)["llm_effort"])
    }

    @Test
    fun aDesktopExportOfTheProviderThisAppCannotTalkToBringsNeitherItsProviderNorItsKey() {
        // The desktop's "local" is a model served on the user's own GPU; this app has none of it.
        val desktop = JSONObject().put("llm", JSONObject().put("provider", "local").put("api_key", "sk-should-not-land")).toString().toByteArray()
        val writes = imported(desktop)
        assertFalse(writes.keys.any { it.startsWith("llm_") })
        assertFalse("custom_api_key" in writes)
    }
}

/** The AI settings in the store itself: written trimmed, read back as written, one edit for model + address. */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class LlmSettingsStoreTest {

    @Test
    fun theAiSettingsRoundTripThroughTheStore() = runBlocking {
        val settings = AppSettings(ApplicationProvider.getApplicationContext())
        try {
            settings.setLlmProvider("openrouter")
            settings.setLlmModelAndBaseUrl("  m1  ", " http://x/v1/ ")
            settings.setLlmEffort("max")
            settings.setLlmCustomSendsEffort(true)

            val p = settings.prefs.first()
            assertEquals("openrouter", p.llmProvider)
            assertEquals("m1", p.llmModel)
            assertEquals("only whitespace is trimmed — the address is the user's", "http://x/v1/", p.llmBaseUrl)
            assertEquals("max", p.llmEffort)
            assertTrue(p.llmCustomSendsEffort)
            assertEquals("nothing else moved", "low", AppPrefs().llmEffort)

            settings.setLlmModel("m2")
            settings.setLlmBaseUrl("http://y/v1")
            val q = settings.prefs.first()
            assertEquals("m2", q.llmModel)
            assertEquals("http://y/v1", q.llmBaseUrl)
            assertEquals("max", q.llmEffort)
        } finally {
            // The store is a process-wide singleton: leave the out-of-box values for whatever runs next.
            settings.setLlmProvider("venice")
            settings.setLlmModelAndBaseUrl("", "")
            settings.setLlmEffort("low")
            settings.setLlmCustomSendsEffort(false)
        }
        Unit
    }

    @Test
    fun outOfTheBoxItIsVeniceLowThinkingAndNoCustomFlag() {
        val p = AppPrefs()
        assertEquals("venice", p.llmProvider)
        assertEquals("", p.llmBaseUrl)
        assertEquals("", p.llmModel)
        assertEquals("owner 2026-11-21: high made every turn think at length before any text", "low", p.llmEffort)
        assertFalse(p.llmCustomSendsEffort)
    }
}
