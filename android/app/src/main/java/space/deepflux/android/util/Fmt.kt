package space.deepflux.android.util

import java.util.Locale

object Fmt {

    fun bytes(v: Long): String {
        if (v < 1024) return "$v B"
        val kb = v / 1024.0
        if (kb < 1024) return String.format(Locale.ROOT, "%.1f KB", kb)
        val mb = kb / 1024.0
        if (mb < 1024) return String.format(Locale.ROOT, "%.1f MB", mb)
        return String.format(Locale.ROOT, "%.2f GB", mb / 1024.0)
    }

    fun speed(bytesPerSecond: Long): String = "${bytes(bytesPerSecond)}/s"

    fun percent(fraction: Float): String =
        String.format(Locale.ROOT, "%.1f%%", (fraction.coerceIn(0f, 1f) * 100))
}
