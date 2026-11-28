package space.deepflux.android.data

import android.util.Base64
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.PBEKeySpec
import javax.crypto.spec.SecretKeySpec

/**
 * Encrypted settings export/import (plan §13 #6) — the Android twin of the
 * desktop's infra/config_backup.py, BYTE-COMPATIBLE with it: the same JSON
 * envelope ({magic deepflux-settings, kdf pbkdf2-sha256, iterations, salt})
 * around the same Fernet token (AES-128-CBC + HMAC-SHA256), so a .dfc made
 * by the desktop app can be imported here (with section mapping) and vice
 * versa. Fernet is implemented on javax.crypto — no extra dependency.
 *
 * Wrong passphrase and tampered files both fail closed (HMAC is checked
 * BEFORE any decryption — nothing is ever partially applied), and the
 * iteration count is bounded exactly like the desktop's guard.
 */
object SettingsBackup {

    class BackupException(message: String) : Exception(message)

    private const val MAGIC = "deepflux-settings"
    private const val VERSION = 1
    private const val KDF_ITERATIONS = 390_000 // OWASP floor, desktop parity
    private const val SALT_BYTES = 16
    private const val MIN_ITERATIONS = 100_000
    private const val MAX_ITERATIONS = 2_000_000

    /** The desktop providers this app can also talk to (its "local" one needs a GPU it does not bundle). */
    private val ANDROID_PROVIDERS = setOf("venice", "deepseek", "openrouter", "custom")

    /** The sidecar files [buildPayload] writes at the root of `files/`. */
    private val EXPORTED_SIDECARS = setOf(
        "browser_bookmarks.json", "browser_history.json", "rss_feeds.json", "player_positions.json", "chat_history.json",
    )

    /** The agent's memory files: USER.md, MEMORY.md and the daily notes — markdown, directly in their folder. */
    private val EXPORTED_MEMORY_FILE = Regex("memory/(?:[A-Za-z0-9_-]+|daily/[A-Za-z0-9_-]+)\\.md")

    /** True for a file name an export of this app can contain — the only ones an import writes. */
    fun isExportedSidecar(name: String): Boolean = name in EXPORTED_SIDECARS || EXPORTED_MEMORY_FILE.matches(name)

    // ---------------------------------------------------------------- crypto

    /** split secret: first half = HMAC signing, second half = AES (Fernet spec). */
    private fun deriveKeys(password: String, salt: ByteArray, iterations: Int): Pair<SecretKeySpec, SecretKeySpec> {
        val spec = PBEKeySpec(password.toCharArray(), salt, iterations, 32 * 8)
        val key = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).encoded
        return SecretKeySpec(key, 0, 16, "HmacSHA256") to SecretKeySpec(key, 16, 16, "AES")
    }

    fun encrypt(payload: ByteArray, password: String): ByteArray {
        if (password.isEmpty()) throw BackupException("a passphrase is required")
        val salt = ByteArray(SALT_BYTES).also { SecureRandom().nextBytes(it) }
        val (signing, encryption) = deriveKeys(password, salt, KDF_ITERATIONS)

        val iv = ByteArray(16).also { SecureRandom().nextBytes(it) }
        val cipher = Cipher.getInstance("AES/CBC/PKCS5Padding")
        cipher.init(Cipher.ENCRYPT_MODE, encryption, IvParameterSpec(iv))
        val ct = cipher.doFinal(payload)

        val ts = System.currentTimeMillis() / 1000
        val body = ByteArrayOutputStream().apply {
            write(0x80)
            repeat(8) { i -> write(((ts ushr ((7 - i) * 8)) and 0xFF).toInt()) }
            write(iv)
            write(ct)
        }.toByteArray()
        val mac = Mac.getInstance("HmacSHA256").apply { init(signing) }.doFinal(body)
        // KEEP the '=' padding — Python's urlsafe_b64decode (the desktop's
        // Fernet) rejects unpadded tokens (verified live: "Incorrect padding").
        val token = Base64.encodeToString(body + mac, Base64.URL_SAFE or Base64.NO_WRAP)

        val env = JSONObject()
            .put("magic", MAGIC)
            .put("version", VERSION)
            .put("kdf", "pbkdf2-sha256")
            .put("iterations", KDF_ITERATIONS)
            .put("salt", Base64.encodeToString(salt, Base64.NO_WRAP))
            .put("token", token)
        return env.toString(2).toByteArray(Charsets.UTF_8)
    }

    fun decrypt(blob: ByteArray, password: String): ByteArray {
        val env = runCatching { JSONObject(String(blob, Charsets.UTF_8)) }
            .getOrElse { throw BackupException("not a DeepFlux settings file") }
        if (env.optString("magic") != MAGIC) throw BackupException("not a DeepFlux settings file")
        val iterations = env.optInt("iterations", -1)
        if (iterations < MIN_ITERATIONS || iterations > MAX_ITERATIONS) {
            throw BackupException("corrupted settings file (unreasonable KDF iterations)")
        }
        val salt = runCatching { Base64.decode(env.optString("salt"), Base64.NO_WRAP) }
            .getOrElse { throw BackupException("corrupted settings file") }
        val token = env.optString("token")
        val raw = runCatching { Base64.decode(token, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING) }
            .getOrElse { throw BackupException("corrupted settings file") }

        // Fernet wire format: 0x80 | ts(8) | iv(16) | ct(≥16) | hmac(32)
        if (raw.size < 1 + 8 + 16 + 16 + 32) throw BackupException("corrupted settings file")
        if (raw[0] != 0x80.toByte()) throw BackupException("corrupted settings file")
        val hmacAt = raw.size - 32
        val body = raw.copyOfRange(0, hmacAt)
        val mac = raw.copyOfRange(hmacAt, raw.size)
        val (signing, encryption) = deriveKeys(password, salt, iterations)
        val expected = Mac.getInstance("HmacSHA256").apply { init(signing) }.doFinal(body)
        if (!MessageDigest.isEqual(expected, mac)) {
            throw BackupException("wrong passphrase or corrupted file")
        }
        val iv = raw.copyOfRange(9, 25)
        val ct = raw.copyOfRange(25, hmacAt)
        return try {
            Cipher.getInstance("AES/CBC/PKCS5Padding")
                .apply { init(Cipher.DECRYPT_MODE, encryption, IvParameterSpec(iv)) }
                .doFinal(ct)
        } catch (e: Exception) {
            throw BackupException("wrong passphrase or corrupted file")
        }
    }

    // ---------------------------------------------------------------- payload

    /**
     * Android payload: {format: "deepflux-android", prefs: {…}, files:
     * {sidecar-name: content}} — the sidecars are the browser bookmarks/
     * history, RSS feeds and player positions. A DESKTOP payload (the full
     * desktop config with its sections) is also accepted on import; the
     * portable fields map over.
     */
    fun buildPayload(prefs: AppPrefs, filesDir: File): ByteArray {
        val files = JSONObject()
        listOf(
            "browser_bookmarks.json", "browser_history.json", "rss_feeds.json",
            "player_positions.json", "chat_history.json",
        ).forEach { name ->
            val f = File(filesDir, name)
            if (f.isFile && f.length() < 4 * 1024 * 1024) files.put(name, f.readText(Charsets.UTF_8))
        }
        // Memory store: every file under files/memory.
        File(filesDir, "memory").takeIf { it.isDirectory }?.walkTopDown()?.filter { it.isFile }?.forEach { f ->
            val rel = "memory/" + f.relativeTo(File(filesDir, "memory")).invariantSeparatorsPath
            if (f.length() < 512 * 1024) files.put(rel, f.readText(Charsets.UTF_8))
        }
        val payload = JSONObject()
            .put("format", "deepflux-android")
            .put("version", 1)
            .put(
                "prefs",
                JSONObject()
                    .put("wifi_only", prefs.wifiOnly)
                    .put("charging_only", prefs.chargingOnly)
                    .put("sequential_default", prefs.sequentialDefault)
                    .put("custom_api_key", prefs.customApiKey)
                    .put("opensubtitles_api_key", prefs.opensubtitlesApiKey)
                    .put("language", prefs.language)
                    // 1.4.16: which AI the chat talks to. The user's own key travels in
                    // custom_api_key above; a key BUILT INTO this build is never exported.
                    .put("llm_provider", prefs.llmProvider)
                    .put("llm_base_url", prefs.llmBaseUrl)
                    .put("llm_model", prefs.llmModel)
                    .put("llm_effort", prefs.llmEffort)
                    .put("llm_custom_sends_effort", prefs.llmCustomSendsEffort),
            )
            .put("files", files)
        return payload.toString().toByteArray(Charsets.UTF_8)
    }

    /** True when the decrypted payload is an Android export (vs desktop). */
    fun isAndroidPayload(payload: ByteArray): Boolean = runCatching {
        JSONObject(String(payload, Charsets.UTF_8)).optString("format") == "deepflux-android"
    }.getOrDefault(false)

    /**
     * Apply a decrypted payload: prefs are written through the setters by
     * the caller (they own the repository); files land as sidecars so the
     * stores pick them up on the post-restart load. Returns a summary of
     * what was restored. DESKTOP payloads map their portable fields (the
     * user's own LLM key). Jackett fields are ignored — the feature was
     * removed from Android (web search only, owner decision 2026-11-15);
     * keys in OLD exports are simply skipped.
     */
    fun applyFilesAndPrefs(payload: ByteArray, filesDir: File, setPref: (String, String) -> Unit): String {
        val data = runCatching { JSONObject(String(payload, Charsets.UTF_8)) }
            .getOrElse { throw BackupException("decrypted content is not valid settings") }
        val sb = StringBuilder()
        if (isAndroidPayload(payload)) {
            val p = data.optJSONObject("prefs") ?: JSONObject()
            fun str(k: String) = p.optString(k, "")
            setPref("custom_api_key", str("custom_api_key"))
            setPref("opensubtitles_api_key", str("opensubtitles_api_key"))
            setPref("language", str("language"))
            setPref("wifi_only", p.optBoolean("wifi_only", true).toString())
            setPref("charging_only", p.optBoolean("charging_only", false).toString())
            setPref("sequential_default", p.optBoolean("sequential_default", true).toString())
            // The AI provider settings exist since 1.4.16: an export made before it has
            // none, and must leave the ones on this device alone.
            if (p.has("llm_provider")) setPref("llm_provider", str("llm_provider"))
            if (p.has("llm_base_url")) setPref("llm_base_url", str("llm_base_url"))
            if (p.has("llm_model")) setPref("llm_model", str("llm_model"))
            if (p.has("llm_effort")) setPref("llm_effort", str("llm_effort"))
            if (p.has("llm_custom_sends_effort")) setPref("llm_custom_sends_effort", p.optBoolean("llm_custom_sends_effort", false).toString())
            val files = data.optJSONObject("files") ?: JSONObject()
            var count = 0
            files.keys().forEach { name ->
                // Only what this app itself puts into an export (1.4.16; it used to take any
                // name without ".." — and so could plant a saved task, or the torrent list).
                if (!isExportedSidecar(name)) return@forEach
                val target = File(filesDir, name)
                target.parentFile?.mkdirs()
                target.writeText(files.optString(name), Charsets.UTF_8)
                count++
            }
            sb.append("restored $count data files + preferences")
        } else {
            // Desktop config: map the portable sections.
            val llm = data.optJSONObject("llm")
            if (llm != null) {
                // The desktop's provider and key belong together — a key made for one
                // service is no good at another, so they come as a pair: with no key on the
                // desktop, the key on this device is cleared too (it used to be kept, and
                // went to the newly imported provider — 2026-11-28, review).
                // "local" (the desktop's built-in model) has no counterpart here.
                val provider = llm.optString("provider", "")
                if (provider in ANDROID_PROVIDERS) {
                    setPref("llm_provider", provider)
                    setPref("llm_base_url", llm.optString("base_url", ""))
                    setPref("llm_model", llm.optString("model", ""))
                    setPref("llm_effort", llm.optString("reasoning_effort", "").ifBlank { "low" })
                    setPref("llm_custom_sends_effort", llm.optBoolean("custom_reasoning_effort", false).toString())
                    setPref("custom_api_key", llm.optString("api_key", ""))
                }
            }
            sb.append("imported desktop settings (AI provider and key)")
        }
        return sb.toString()
    }
}
