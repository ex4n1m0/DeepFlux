package space.deepflux.android.agent

/**
 * One OpenAI-compatible model provider — the Android twin of the desktop's
 * `LLM_PROVIDER_PRESETS` (config.py). Kept as data so switching endpoints
 * needs no code change, and so the wire-level differences between providers
 * live in exactly one place.
 */
data class LlmPreset(
    val id: String,
    /** Shown in Settings (localized labels live in the string catalog). */
    val label: String,
    val baseUrl: String,
    /** Used when the user has not typed a model name. */
    val defaultModel: String,
    /** Whether this endpoint understands `reasoning_effort`. */
    val reasoningEffort: Boolean,
    /** How each level is spelled there, when it differs ("medium" is not a Venice level). */
    val effortMap: Map<String, String> = emptyMap(),
)

/** Where one chat request goes, as which model, thinking how hard. */
data class LlmEndpoint(
    val providerId: String,
    val baseUrl: String,
    val model: String,
    /** The `reasoning_effort` to send, or null when the endpoint takes none. */
    val effort: String?,
)

object LlmPresets {

    val VENICE = LlmPreset(
        "venice", "Venice AI", "https://api.venice.ai/api/v1",
        "deepseek-v4-1-flash-uncensored", reasoningEffort = true, effortMap = mapOf("medium" to "high"),
    )
    val DEEPSEEK = LlmPreset(
        "deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-flash", reasoningEffort = true,
    )
    val OPENROUTER = LlmPreset(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "deepseek/deepseek-v4.1-flash",
        reasoningEffort = true, effortMap = mapOf("max" to "xhigh"),
    )

    /** Any OpenAI-compatible endpoint: the user supplies base URL and model. */
    val CUSTOM = LlmPreset("custom", "Custom", "", "", reasoningEffort = false)

    val ALL: List<LlmPreset> = listOf(VENICE, DEEPSEEK, OPENROUTER, CUSTOM)

    /** The out-of-box agent (owner 2026-11-17): the built-in key is a Venice key. */
    val DEFAULT: LlmPreset = VENICE

    /** Thinking levels, in the order Settings lists them. */
    val EFFORTS: List<String> = listOf("none", "low", "medium", "high", "max")

    /**
     * "low" (owner 2026-11-21): high effort makes every turn — the final
     * answer included — think at length before any visible text.
     */
    const val DEFAULT_EFFORT = "low"

    /** OpenRouter wants vendor-prefixed slugs; the config stores bare names. */
    private val OPENROUTER_SLUGS = mapOf(
        "deepseek-flash" to "deepseek/deepseek-v4.1-flash",
        "deepseek-v4-pro" to "deepseek/deepseek-v4-pro",
    )

    fun byId(id: String?): LlmPreset = ALL.firstOrNull { it.id == id } ?: DEFAULT

    /**
     * The endpoint for the user's settings.
     *
     * @throws IllegalArgumentException for the "custom" provider without a base URL
     */
    fun resolve(
        providerId: String?,
        baseUrl: String,
        model: String,
        effort: String,
        /** Custom endpoints only: whether the user says theirs understands `reasoning_effort`. */
        customSendsEffort: Boolean = false,
    ): LlmEndpoint {
        val preset = byId(providerId)
        val custom = preset.id == CUSTOM.id
        val base = baseUrl.trim().trimEnd('/')
        if (custom && base.isEmpty()) throw IllegalArgumentException("Custom LLM provider requires a base URL")
        val level = if (effort in EFFORTS) effort else DEFAULT_EFFORT
        // Desktop rule: a custom endpoint sends the field only if the user says
        // theirs understands it; every other endpoint follows its preset.
        val sendsEffort = if (custom) customSendsEffort else preset.reasoningEffort
        return LlmEndpoint(
            providerId = preset.id,
            baseUrl = base.ifEmpty { preset.baseUrl },
            model = normalizeModel(preset.id, model.trim().ifEmpty { preset.defaultModel }),
            effort = if (sendsEffort) preset.effortMap[level] ?: level else null,
        )
    }

    fun normalizeModel(providerId: String, model: String): String {
        // A slug that is versioned on OpenRouter is never prefixed blindly
        // ("deepseek/deepseek-flash" does not exist there).
        if (providerId == OPENROUTER.id && !model.contains('/')) {
            return OPENROUTER_SLUGS[model] ?: "deepseek/$model"
        }
        return model
    }

    /** True when the built-in shared key can serve this provider (it is a Venice key). */
    fun builtInKeyWorksFor(providerId: String?): Boolean = byId(providerId).id == VENICE.id
}
