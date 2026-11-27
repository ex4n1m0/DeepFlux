package space.deepflux.android.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import space.deepflux.android.agent.LlmPresets
import space.deepflux.android.data.AppPrefs
import space.deepflux.android.data.EmbeddedKey
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.DimText
import space.deepflux.android.ui.theme.OkGreen

/**
 * Settings → "AI & services" → the AI agent (1.4.16): WHICH provider the chat talks to,
 * which model, how hard it thinks, and the key — the Android twin of the desktop's
 * AI settings page. Until this card the app talked to Venice, with the default model
 * and no thinking level, whatever the user would have wanted.
 *
 * Provider and thinking level apply at once (they are choices, like the device mode);
 * the key, the model and the base URL have Save buttons (typed text).
 */
@Composable
internal fun AiAgentCard(repository: TorrentRepository, prefs: AppPrefs) {
    val provider = LlmPresets.byId(prefs.llmProvider)
    val isCustom = provider.id == LlmPresets.CUSTOM.id
    // The built-in key is a Venice key — for every other provider only the user's own will do.
    val builtInWorks = LlmPresets.builtInKeyWorksFor(provider.id)
    val embedded = remember { EmbeddedKey.hasEmbedded() }

    CompactCard(null) {
        Text(Strings.t.aiAgentTitle, style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(4.dp))
        FlowRow(modifier = Modifier.fillMaxWidth()) {
            LlmPresets.ALL.forEach { candidate ->
                val selected = candidate.id == provider.id
                TextButton(onClick = { repository.setLlmProvider(candidate.id) }) {
                    Text(
                        (if (selected) "● " else "") + Strings.llmProviderLabel(candidate.id),
                        color = if (selected) Accent else DimText,
                        style = MaterialTheme.typography.labelMedium,
                    )
                }
            }
        }

        // ---- the key
        Spacer(Modifier.height(4.dp))
        Text(
            when {
                !builtInWorks -> Strings.t.llmNeedsOwnKey
                embedded -> Strings.t.builtInKey
                else -> Strings.t.noBuiltInKey
            },
            style = MaterialTheme.typography.bodySmall,
            // Green only where there is something to use: the built-in key for Venice.
            color = if (builtInWorks && embedded) OkGreen else DimText,
        )
        Spacer(Modifier.height(6.dp))
        var key by remember(prefs.customApiKey) { mutableStateOf(prefs.customApiKey) }
        OutlinedTextField(
            value = key,
            onValueChange = { key = it },
            label = {
                Text(
                    if (builtInWorks) Strings.t.customKey
                    else Strings.t.llmKeyForFmt.format(Strings.llmProviderLabel(provider.id)),
                )
            },
            supportingText = { Text(keyHint(provider.id), style = MaterialTheme.typography.labelSmall) },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
        )
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            TextButton(onClick = { repository.setCustomApiKey(key) }) {
                Text(Strings.t.saveKey)
            }
        }

        // ---- the model, and for a custom endpoint its address
        HorizontalDivider(color = MaterialTheme.colorScheme.outline, thickness = 0.5.dp)
        Spacer(Modifier.height(8.dp))
        var model by remember(prefs.llmModel) { mutableStateOf(prefs.llmModel) }
        var baseUrl by remember(prefs.llmBaseUrl) { mutableStateOf(prefs.llmBaseUrl) }
        OutlinedTextField(
            value = model,
            onValueChange = { model = it },
            label = { Text(Strings.t.llmModelLabel) },
            supportingText = {
                Text(
                    if (isCustom) Strings.t.llmModelCustomHint else Strings.t.llmModelDefaultFmt.format(provider.defaultModel),
                    style = MaterialTheme.typography.labelSmall,
                )
            },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        if (isCustom) {
            Spacer(Modifier.height(6.dp))
            OutlinedTextField(
                value = baseUrl,
                onValueChange = { baseUrl = it },
                label = { Text(Strings.t.llmBaseUrlLabel) },
                supportingText = { Text(Strings.t.llmBaseUrlHint, style = MaterialTheme.typography.labelSmall) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
        }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            TextButton(
                enabled = model.trim() != prefs.llmModel || baseUrl.trim() != prefs.llmBaseUrl,
                onClick = { repository.setLlmModelAndBaseUrl(model, baseUrl) },
            ) {
                Text(Strings.t.save)
            }
        }

        // ---- how hard the model thinks
        HorizontalDivider(color = MaterialTheme.colorScheme.outline, thickness = 0.5.dp)
        Spacer(Modifier.height(4.dp))
        // A custom endpoint gets the field only when the user says it understands it.
        if (isCustom) {
            SwitchRow(Strings.t.llmCustomSendsEffort, Strings.t.llmCustomSendsEffortDesc, prefs.llmCustomSendsEffort) {
                repository.setLlmCustomSendsEffort(it)
            }
        }
        if (provider.reasoningEffort || prefs.llmCustomSendsEffort) {
            Text(Strings.t.llmThinking, style = MaterialTheme.typography.bodyLarge)
            Text(Strings.t.llmThinkingDesc, style = MaterialTheme.typography.labelMedium, color = DimText)
            FlowRow(modifier = Modifier.fillMaxWidth()) {
                LlmPresets.EFFORTS.forEach { level ->
                    val selected = level == prefs.llmEffort
                    TextButton(onClick = { repository.setLlmEffort(level) }) {
                        Text(
                            (if (selected) "● " else "") + Strings.llmEffortLabel(level),
                            color = if (selected) Accent else DimText,
                            style = MaterialTheme.typography.labelMedium,
                        )
                    }
                }
            }
        }
    }
}

/** Where to get a key for this provider — the desktop's `key_hint` of the same preset. */
private fun keyHint(providerId: String): String = when (providerId) {
    LlmPresets.VENICE.id -> Strings.t.llmKeyHintVenice
    LlmPresets.DEEPSEEK.id -> Strings.t.llmKeyHintDeepseek
    LlmPresets.OPENROUTER.id -> Strings.t.llmKeyHintOpenrouter
    else -> Strings.t.llmKeyHintCustom
}
