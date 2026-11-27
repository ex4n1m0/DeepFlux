package space.deepflux.android.agent

import space.deepflux.android.data.AppPrefs

/** Where one turn's requests go, and with which key (the conversation is passed separately). */
data class ChatTarget(val endpoint: LlmEndpoint, val apiKey: String)

/** What a send needs — or why it cannot go out. */
sealed class ChatPlan {
    data class Go(val target: ChatTarget) : ChatPlan()

    /** Another provider than Venice, and the user has entered no key of their own. [provider] is its id. */
    data class NeedsOwnKey(val provider: String) : ChatPlan()

    /** Venice, with neither the user's key nor a built-in one in this build. */
    object NoKey : ChatPlan()

    /** The settings do not describe a usable endpoint (a custom provider without an address). */
    data class BadSettings(val message: String) : ChatPlan()
}

/**
 * Decides, from the user's settings, what a send needs — pure, so the rules are tested
 * without a model or the network:
 *  * the endpoint is [LlmPresets.resolve] (provider, model, thinking level);
 *  * the user's own key always wins; the BUILT-IN key serves Venice only, and is not
 *    even read for another provider ([LlmPresets.apiKeyFor]) — so a key is "missing"
 *    with two different meanings, and the chat can say which one applies.
 */
fun planSend(prefs: AppPrefs, builtInKey: () -> String): ChatPlan = try {
    val endpoint = LlmPresets.resolve(
        prefs.llmProvider, prefs.llmBaseUrl, prefs.llmModel, prefs.llmEffort, prefs.llmCustomSendsEffort,
    )
    when (val key = LlmPresets.apiKeyFor(endpoint.providerId, prefs.customApiKey, builtInKey)) {
        null ->
            if (LlmPresets.builtInKeyWorksFor(endpoint.providerId)) ChatPlan.NoKey
            else ChatPlan.NeedsOwnKey(endpoint.providerId)
        else -> ChatPlan.Go(ChatTarget(endpoint, key))
    }
} catch (e: IllegalArgumentException) {
    ChatPlan.BadSettings(e.message ?: "")
}
