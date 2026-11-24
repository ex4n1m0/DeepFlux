package space.deepflux.android.data

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.emptyPreferences
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import java.io.IOException

private val Context.settingsDataStore by preferencesDataStore(name = "settings")

data class AppPrefs(
    val wifiOnly: Boolean = true,
    val chargingOnly: Boolean = false,
    val sequentialDefault: Boolean = true,
    val customApiKey: String = "",
    val customSaveDirUri: String = "",
    /** UI language: "en" (default) or "zh". */
    val language: String = "en",
    /** OpenSubtitles consumer key (plan §13 #8); blank = feature off. */
    val opensubtitlesApiKey: String = "",
    /** Browser homepage (v1.3); blank = the start page with quick links. */
    val homepage: String = "https://deepflux.space",
    /** Device mode (owner 2026-11-15): "" = ask on first run, then "tv" | "mobile" | "tablet". */
    val uiMode: String = "",
    /** In-app updater (§13 #7): startup feed check, ~daily gate. */
    val updatesAutoCheck: Boolean = true,
    /** Version the user explicitly skipped; never re-offered except by manual check. */
    val updateSkipVersion: String = "",
    /** Bookkeeping for the ~daily auto-check gate — not user-facing. */
    val lastUpdateCheck: Long = 0L,
)

/**
 * App-level settings, DataStore-backed. The Android counterpart of the
 * desktop config sections (config.py) — small, typed, and filtered-by-
 * construction like the desktop's dataclass sections.
 */
class AppSettings(private val context: Context) {

    val prefs: Flow<AppPrefs> = context.settingsDataStore.data
        .catch { if (it is IOException) emit(emptyPreferences()) else throw it }
        .map { p ->
            AppPrefs(
                wifiOnly = p[WIFI_ONLY] ?: true,
                chargingOnly = p[CHARGING_ONLY] ?: false,
                sequentialDefault = p[SEQUENTIAL] ?: true,
                customApiKey = p[CUSTOM_API_KEY] ?: "",
                customSaveDirUri = p[CUSTOM_SAVE_DIR] ?: "",
                language = p[LANGUAGE] ?: "en",
                opensubtitlesApiKey = p[OPENSUBTITLES_KEY] ?: "",
                homepage = p[HOMEPAGE] ?: "https://deepflux.space",
                uiMode = p[UI_MODE] ?: "",
                updatesAutoCheck = p[UPDATES_AUTO] ?: true,
                updateSkipVersion = p[UPDATE_SKIP] ?: "",
                lastUpdateCheck = p[UPDATE_LAST_CHECK] ?: 0L,
            )
        }

    suspend fun setWifiOnly(value: Boolean) {
        context.settingsDataStore.edit { it[WIFI_ONLY] = value }
    }

    suspend fun setChargingOnly(value: Boolean) {
        context.settingsDataStore.edit { it[CHARGING_ONLY] = value }
    }

    suspend fun setSequentialDefault(value: Boolean) {
        context.settingsDataStore.edit { it[SEQUENTIAL] = value }
    }

    suspend fun setCustomApiKey(value: String) {
        context.settingsDataStore.edit { it[CUSTOM_API_KEY] = value.trim() }
    }

    suspend fun setJackettUrl(value: String) {
        // Removed with the Jackett feature (owner decision 2026-11-15) —
        // kept as a no-op so older .dfc imports call it harmlessly.
    }

    suspend fun setJackettApiKey(value: String) {
        // See setJackettUrl.
    }

    suspend fun setCustomSaveDir(uri: String) {
        context.settingsDataStore.edit { it[CUSTOM_SAVE_DIR] = uri }
    }

    /** Finished downloads go to the public Downloads folder again. */
    suspend fun clearCustomSaveDir() {
        context.settingsDataStore.edit { it.remove(CUSTOM_SAVE_DIR) }
    }

    suspend fun saveChatHistory(json: String) {
        context.settingsDataStore.edit { it[CHAT_HISTORY] = json }
    }

    fun loadChatHistory(): String {
        return runCatching {
            kotlinx.coroutines.runBlocking {
                context.settingsDataStore.data.first()[CHAT_HISTORY] ?: ""
            }
        }.getOrDefault("")
    }

    suspend fun setLanguage(value: String) {
        context.settingsDataStore.edit { it[LANGUAGE] = value }
    }

    suspend fun setOpensubtitlesApiKey(value: String) {
        context.settingsDataStore.edit { it[OPENSUBTITLES_KEY] = value.trim() }
    }

    suspend fun setHomepage(value: String) {
        context.settingsDataStore.edit { it[HOMEPAGE] = value.trim() }
    }

    suspend fun setUiMode(value: String) {
        context.settingsDataStore.edit { it[UI_MODE] = value }
    }

    suspend fun setUpdatesAutoCheck(value: Boolean) {
        context.settingsDataStore.edit { it[UPDATES_AUTO] = value }
    }

    suspend fun setUpdateSkipVersion(value: String) {
        context.settingsDataStore.edit { it[UPDATE_SKIP] = value }
    }

    suspend fun setLastUpdateCheck(value: Long) {
        context.settingsDataStore.edit { it[UPDATE_LAST_CHECK] = value }
    }

    /** One-shot read for background workers (the updater's gate). */
    suspend fun prefsSnapshot(): AppPrefs = prefs.first()

    /** Synchronous boot read — same pattern as loadChatHistory. */
    fun loadLanguage(): String {
        return runCatching {
            kotlinx.coroutines.runBlocking {
                context.settingsDataStore.data.first()[LANGUAGE] ?: "en"
            }
        }.getOrDefault("en")
    }

    private companion object {
        val WIFI_ONLY = booleanPreferencesKey("wifi_only")
        val CHARGING_ONLY = booleanPreferencesKey("charging_only")
        val SEQUENTIAL = booleanPreferencesKey("sequential_default")
        val CUSTOM_API_KEY = stringPreferencesKey("custom_api_key")
        val CHAT_HISTORY = stringPreferencesKey("chat_history_json")
        val CUSTOM_SAVE_DIR = stringPreferencesKey("custom_save_dir_uri")
        val LANGUAGE = stringPreferencesKey("language")
        val OPENSUBTITLES_KEY = stringPreferencesKey("opensubtitles_api_key")
        val HOMEPAGE = stringPreferencesKey("browser_homepage")
        val UI_MODE = stringPreferencesKey("ui_mode")
        val UPDATES_AUTO = booleanPreferencesKey("updates_auto_check")
        val UPDATE_SKIP = stringPreferencesKey("update_skip_version")
        val UPDATE_LAST_CHECK = longPreferencesKey("update_last_check")
    }
}
