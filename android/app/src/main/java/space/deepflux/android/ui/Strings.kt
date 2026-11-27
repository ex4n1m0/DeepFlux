package space.deepflux.android.ui

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/**
 * Language switcher — ENGLISH default, zh-CN opt-in. `isChinese` is Compose
 * snapshot state, so every `Strings.t.x` read inside a composition
 * recomposes when the language flips (non-Compose readers — the chat
 * thread, the player — just see a plain read). The choice is persisted
 * (AppSettings.language) and restored in MainActivity before the first
 * frame; before this existed the app showed a MIX of English literals and
 * the always-Chinese ZhStrings — one catalog, wired everywhere, fixes that.
 */
object Strings {
    var isChinese: Boolean by mutableStateOf(false) // DEFAULT: English
        private set

    fun setLanguage(chinese: Boolean) {
        isChinese = chinese
    }

    // Convenience accessor: `Strings.t.downloads`
    val t: AppStrings get() = if (isChinese) ZhStrings else EnStrings

    /** An AI provider's name in the active language — the ids are `LlmPresets.ALL`'s. */
    fun llmProviderLabel(id: String): String = when (id) {
        "venice" -> t.llmProviderVenice
        "deepseek" -> t.llmProviderDeepseek
        "openrouter" -> t.llmProviderOpenrouter
        else -> t.llmProviderCustom
    }

    /** A thinking level's name in the active language — the ids are `LlmPresets.EFFORTS`'. */
    fun llmEffortLabel(id: String): String = when (id) {
        "none" -> t.llmEffortNone
        "low" -> t.llmEffortLow
        "medium" -> t.llmEffortMedium
        "high" -> t.llmEffortHigh
        else -> t.llmEffortMax
    }
}
