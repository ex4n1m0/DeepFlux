package space.deepflux.android.data

import android.util.Log
import space.deepflux.android.BuildConfig
import java.security.MessageDigest

/**
 * Decodes the obfuscated shared VENICE key embedded at build time — the
 * Android mirror of config._decode_shared_value / packaging/gen_embedded_keys.py
 * (owner decision 2026-11-12: the shared key SHIPS in the app, same
 * exposure stance as the Windows setup exe; 2026-11-17: the agent provider
 * is Venice AI's uncensored DeepSeek, so this slot carries the Venice key).
 *
 * BuildConfig carries only (salt_hex, blob_hex) where
 * blob = key XOR sha256(salt + counter_be32) repeated — no plaintext
 * constant exists in the APK. The scheme is obfuscation, not crypto; that
 * is the ceiling for any client-side embedded key (AGENTS.md, KEY HYGIENE).
 */
object EmbeddedKey {

    private const val TAG = "EmbeddedKey"

    fun hasEmbedded(): Boolean = veniceKey().isNotEmpty()

    /** Effective agent key: a user-entered custom key always wins (desktop parity). */
    fun effectiveKey(customKey: String): String =
        customKey.ifBlank { veniceKey() }

    /** Shared Venice AI key — the built-in uncensored-DeepSeek agent (1.4.12). */
    fun veniceKey(): String = decodePair(
        BuildConfig.EMBEDDED_VENICE_SALT,
        BuildConfig.EMBEDDED_VENICE_BLOB,
    )

    /**
     * Shared TMDb key (1.3.5, movie/series covers). Empty in keyless builds —
     * the app then falls back to the keyless TVmaze provider.
     */
    fun tmdbKey(): String = decodePair(
        BuildConfig.EMBEDDED_TMDB_SALT,
        BuildConfig.EMBEDDED_TMDB_BLOB,
    )

    /** TPDB (theporndb.net) — adult movies/scenes/JAV (1.4.0). */
    fun tpdbKey(): String = decodePair(
        BuildConfig.EMBEDDED_TPDB_SALT,
        BuildConfig.EMBEDDED_TPDB_BLOB,
    )

    /** StashDB — community adult DB, second adult source (1.4.0). */
    fun stashdbKey(): String = decodePair(
        BuildConfig.EMBEDDED_STASHDB_SALT,
        BuildConfig.EMBEDDED_STASHDB_BLOB,
    )

    private fun decodePair(saltHex: String, blobHex: String): String {
        val salt = saltHex.hexToBytesOrNull() ?: return ""
        val blob = blobHex.hexToBytesOrNull() ?: return ""
        if (salt.isEmpty() || blob.isEmpty()) return ""
        return try {
            val pad = keystream(salt, blob.size)
            val out = ByteArray(blob.size) { i -> (blob[i].toInt() xor pad[i].toInt()).toByte() }
            val text = String(out, Charsets.UTF_8)
            // Fail closed on tamper/garbage, exactly like the desktop decoder.
            if (text.isNotEmpty() && text.all { it.code in 32..126 }) text else ""
        } catch (t: Throwable) {
            Log.w(TAG, "embedded key decode failed (tampered pair?)")
            ""
        }
    }

    private fun keystream(salt: ByteArray, length: Int): ByteArray {
        val md = MessageDigest.getInstance("SHA-256")
        val out = ByteArray(length)
        var pos = 0
        var counter = 0
        while (pos < length) {
            md.reset()
            md.update(salt)
            md.update(
                byteArrayOf(
                    (counter ushr 24).toByte(),
                    (counter ushr 16).toByte(),
                    (counter ushr 8).toByte(),
                    counter.toByte(),
                ),
            )
            val block = md.digest()
            val n = minOf(block.size, length - pos)
            System.arraycopy(block, 0, out, pos, n)
            pos += n
            counter++
        }
        return out
    }

    private fun String.hexToBytesOrNull(): ByteArray? {
        if (isEmpty() || length % 2 != 0) return null
        val out = ByteArray(length / 2)
        for (i in indices step 2) {
            val byte = substring(i, i + 2).toIntOrNull(16) ?: return null
            out[i / 2] = byte.toByte()
        }
        return out
    }
}
