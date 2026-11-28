package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * 1.4.16 — how the AI provider settings are WIRED, read from the sources. The rules are
 * tested outright (LlmPlanTest, ChatClientWireTest, ChatLoopTest); these pin the lines that
 * connect them, which no JVM test executes:
 *
 *  * the chat's own model client — the tests inject a scripted one, so the production
 *    line that turns the settings into a client is never run by them;
 *  * the chat goes through [space.deepflux.android.agent.planSend], and nowhere through
 *    the old "effective key" shortcut, which would hand the built-in key to ANY provider;
 *  * the Settings card and the backup do what their names say.
 */
class LlmWiringTest {

    /** A source file of the app module, found from wherever the test runner starts. */
    private fun source(relative: String): String {
        var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
        val marker = "src/main/java/space/deepflux/android/player/PlayerActivity.kt"
        while (dir != null && !File(dir, marker).isFile) dir = dir.parentFile
        return dir?.let { File(it, relative).takeIf { f -> f.isFile }?.readText() }
            ?: error("not found from ${System.getProperty("user.dir")}: $relative")
    }

    private val chat by lazy { source("src/main/java/space/deepflux/android/ui/chat/ChatScreen.kt") }
    private val card by lazy { source("src/main/java/space/deepflux/android/ui/settings/AiAgentCard.kt") }
    private val backup by lazy { source("src/main/java/space/deepflux/android/data/SettingsBackup.kt") }

    @Test
    fun theChatsOwnClientCarriesTheWholeTargetAddressKeyModelAndThinkingLevel() {
        // The one line that makes a client for a real request. Each part is the rule's:
        // the address of the provider the user picked, the key the key rule chose, the
        // model, and the thinking level (null where the endpoint takes none).
        assertTrue(
            chat.contains("ChatClient(it.endpoint.baseUrl, it.apiKey, it.endpoint.model, it.endpoint.effort)"),
        )
        // Not the constructor's own defaults any more: those are the out-of-box Venice
        // endpoint, whatever the user picked.
        assertFalse("the client must not be built from the defaults", chat.contains("ChatClient(apiKey = it)"))
    }

    @Test
    fun theBuiltInKeyComesFromTheOnePlaceAndIsHandedToThePlanNotToAProvider() {
        assertTrue(chat.contains("private val builtInKey: () -> String = { EmbeddedKey.veniceKey() }"))
        // The whole decision goes through the plan — the guard is part of the statement.
        assertTrue(chat.contains("when (val plan = planSend(repository.prefs.value, builtInKey)) {"))
        // The old shortcut ("the user's key, else the built-in one") knows nothing of the
        // provider: with it, a custom endpoint would have been sent the built-in key.
        assertFalse("the provider-blind key shortcut must not pick the chat's key", chat.contains("EmbeddedKey.effectiveKey("))
    }

    @Test
    fun aRefusalNamesTheProviderThatWasAskedFor() {
        assertTrue(chat.contains("Strings.t.needsOwnKeyFmt.format(Strings.llmProviderLabel(plan.provider))"))
    }

    @Test
    fun theSettingsCardSavesModelAndAddressInOneStepAndTheThinkingLevelAtOnce() {
        assertTrue(card.contains("onClick = { repository.setLlmModelAndBaseUrl(model, baseUrl) }"))
        assertTrue(card.contains("onClick = { repository.setLlmEffort(level) }"))
        assertTrue(card.contains("onClick = { repository.setLlmProvider(candidate.id) }"))
        // The built-in key's green line is for Venice only — another provider has no use for it.
        assertTrue(card.contains("color = if (builtInWorks && embedded) OkGreen else DimText,"))
        // A custom endpoint's thinking level follows the user's switch, not the preset.
        assertTrue(card.contains("if (provider.reasoningEffort || prefs.llmCustomSendsEffort) {"))
    }

    @Test
    fun theTypedAddressIsTheCustomProvidersAloneAndTheKeyHasASecondWall() {
        // (Two reviews, 2026-11-28.) The rules themselves are LlmPlanTest's; these are the lines.
        val presets = source("src/main/java/space/deepflux/android/agent/LlmPresets.kt")
        assertTrue(presets.contains("baseUrl = if (custom) base else preset.baseUrl,"))
        assertFalse("the typed address never replaces another provider's own", "base.ifEmpty { preset.baseUrl }" in presets)
        assertTrue(presets.contains("endpoint.providerId == VENICE.id && endpoint.baseUrl == VENICE.baseUrl"))
        val plan = source("src/main/java/space/deepflux/android/agent/ChatPlan.kt")
        assertTrue(plan.contains("LlmPresets.apiKeyFor(endpoint, prefs.customApiKey, builtInKey)"))
        assertFalse("nothing may print the key by printing the plan", "data class ChatTarget" in plan || "data class Go(" in plan)
    }

    @Test
    fun theSettingsImportHandsEveryAiFieldToItsOwnSetter() {
        val screen = source("src/main/java/space/deepflux/android/ui/settings/SettingsScreen.kt")
        listOf(
            "\"llm_provider\" -> repository.setLlmProvider(value)",
            "\"llm_base_url\" -> repository.setLlmBaseUrl(value)",
            "\"llm_model\" -> repository.setLlmModel(value)",
            "\"llm_effort\" -> repository.setLlmEffort(value)",
            "\"llm_custom_sends_effort\" -> repository.setLlmCustomSendsEffort(value.toBooleanStrict())",
        ).forEach { assertEquals("missing: $it", 1, Regex(Regex.escape(it)).findAll(screen).count()) }
    }

    @Test
    fun theBackupExportsOnlyTheUsersOwnKeyNeverOneBuiltIntoTheBuild() {
        assertTrue(backup.contains(".put(\"custom_api_key\", prefs.customApiKey)"))
        assertFalse("a key built into the build must never be written into an export", backup.contains("EmbeddedKey"))
        assertTrue("the desktop's local-model provider is not this app's", backup.contains("setOf(\"venice\", \"deepseek\", \"openrouter\", \"custom\")"))
    }
}
