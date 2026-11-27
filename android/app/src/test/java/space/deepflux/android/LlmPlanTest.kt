package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.agent.ChatPlan
import space.deepflux.android.agent.LlmPresets
import space.deepflux.android.agent.planSend
import space.deepflux.android.data.AppPrefs

/**
 * 1.4.16 — what a chat send needs, decided from the settings: the provider's address, the
 * model, the thinking level and the key. The rules are pure, so they are tested outright.
 *
 * The one with teeth is the KEY rule. The built-in shared key is a Venice key: it must reach
 * Venice and nothing else — never another company's API, never an address the user typed —
 * and for any other provider it must not even be READ.
 */
class LlmPlanTest {

    private var builtInReads = 0

    /** Padded on purpose: whatever comes from here is used trimmed. */
    private val builtIn: () -> String = {
        builtInReads++
        "  builtin-venice-key  "
    }

    private fun prefs(
        provider: String = "venice",
        key: String = "",
        baseUrl: String = "",
        model: String = "",
        effort: String = "low",
        customEffort: Boolean = false,
    ) = AppPrefs(
        llmProvider = provider, customApiKey = key, llmBaseUrl = baseUrl,
        llmModel = model, llmEffort = effort, llmCustomSendsEffort = customEffort,
    )

    private fun target(p: AppPrefs) = (planSend(p, builtIn) as ChatPlan.Go).target

    @Test
    fun outOfTheBoxItIsVeniceItsDefaultModelLowThinkingAndTheBuiltInKey() {
        val t = target(prefs())
        assertEquals("venice", t.endpoint.providerId)
        assertEquals("https://api.venice.ai/api/v1", t.endpoint.baseUrl)
        assertEquals(LlmPresets.VENICE.defaultModel, t.endpoint.model)
        assertEquals("low", t.endpoint.effort)
        assertEquals("builtin-venice-key", t.apiKey)
        assertEquals(1, builtInReads)
    }

    @Test
    fun theUsersOwnKeyWinsOverTheBuiltInOne() {
        assertEquals("sk-mine", target(prefs(key = "  sk-mine  ")).apiKey)
        assertEquals("the built-in key is not read when the user has one", 0, builtInReads)
    }

    @Test
    fun anotherProviderWithoutTheUsersOwnKeyIsRefusedAndTheBuiltInKeyIsNeverRead() {
        val cases = listOf(
            "deepseek" to prefs(provider = "deepseek"),
            "openrouter" to prefs(provider = "openrouter"),
            "custom" to prefs(provider = "custom", baseUrl = "http://somewhere-else.example/v1"),
        )
        for ((provider, p) in cases) {
            val plan = planSend(p, builtIn)
            assertEquals(provider, ChatPlan.NeedsOwnKey(provider), plan)
        }
        assertEquals("not once, for any of them", 0, builtInReads)
    }

    @Test
    fun anotherProviderWithTheUsersOwnKeyGoesToItsOwnAddress() {
        val t = target(prefs(provider = "deepseek", key = "sk-user", effort = "high"))
        assertEquals("deepseek", t.endpoint.providerId)
        assertEquals("https://api.deepseek.com", t.endpoint.baseUrl)
        assertEquals(LlmPresets.DEEPSEEK.defaultModel, t.endpoint.model)
        assertEquals("high", t.endpoint.effort)
        assertEquals("sk-user", t.apiKey)
        assertEquals(0, builtInReads)
    }

    @Test
    fun aCustomEndpointNeedsAnAddress() {
        val plan = planSend(prefs(provider = "custom", key = "sk-user", baseUrl = "   "), builtIn)
        assertTrue(plan is ChatPlan.BadSettings)
        assertTrue((plan as ChatPlan.BadSettings).message.contains("base URL", ignoreCase = true))
    }

    @Test
    fun aCustomEndpointSendsTheThinkingLevelOnlyWhenTheUserSaysTheEndpointTakesIt() {
        val quiet = target(prefs(provider = "custom", key = "sk-user", baseUrl = "http://llm.lan:8000/v1/", model = "qwen", effort = "high"))
        assertEquals("no trailing slash — the client appends /chat/completions", "http://llm.lan:8000/v1", quiet.endpoint.baseUrl)
        assertEquals("qwen", quiet.endpoint.model)
        assertNull(quiet.endpoint.effort)

        val speaking = target(prefs(provider = "custom", key = "sk-user", baseUrl = "http://llm.lan:8000/v1", model = "qwen", effort = "high", customEffort = true))
        assertEquals("high", speaking.endpoint.effort)
    }

    @Test
    fun veniceHasNoMediumSoItIsSentAsHighAndAnUnknownLevelBecomesLow() {
        assertEquals("high", target(prefs(effort = "medium")).endpoint.effort)
        assertEquals("none", target(prefs(effort = "none")).endpoint.effort)
        assertEquals("max", target(prefs(effort = "max")).endpoint.effort)
        assertEquals("low", target(prefs(effort = "sideways")).endpoint.effort)
        assertEquals("low", target(prefs(effort = "")).endpoint.effort)
    }

    @Test
    fun openrouterGetsTheVendorInFrontOfTheModel() {
        assertEquals("deepseek/deepseek-v4-pro", target(prefs(provider = "openrouter", key = "k", model = "deepseek-v4-pro")).endpoint.model)
        assertEquals("never twice", "vendor/model", target(prefs(provider = "openrouter", key = "k", model = "vendor/model")).endpoint.model)
        assertEquals("its top level is spelled xhigh", "xhigh", target(prefs(provider = "openrouter", key = "k", effort = "max")).endpoint.effort)
    }

    @Test
    fun veniceWithoutAnyKeyAtAllSaysSo() {
        val plan = planSend(prefs(), { "   " })
        assertEquals(ChatPlan.NoKey, plan)
    }

    @Test
    fun aProviderNobodyKnowsReadsAsTheDefault() {
        val t = target(prefs(provider = "no-such-provider"))
        assertEquals("venice", t.endpoint.providerId)
        assertEquals(LlmPresets.VENICE.defaultModel, t.endpoint.model)
        assertFalse(t.apiKey.isBlank())
    }

    @Test
    fun aModelTypedWithSpacesAroundItIsTrimmedAndAnEmptyOneMeansTheDefault() {
        assertEquals("gpt-x", target(prefs(key = "k", model = "  gpt-x ")).endpoint.model)
        assertEquals(LlmPresets.VENICE.defaultModel, target(prefs(key = "k", model = "   ")).endpoint.model)
    }
}
