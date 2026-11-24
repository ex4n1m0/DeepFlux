package space.deepflux.android.player

import space.deepflux.android.ui.Strings

/**
 * The player's cycling controls — rotation, aspect ratio — and what the
 * audio picker calls a track. No Android types, so the cycles and labels are
 * unit-tested; [PlayerActivity] maps them onto window and view settings.
 */
internal object PlayerModes {

    /**
     * How the picture meets the screen, in the order the aspect button cycles:
     *  - [FIT]: the whole picture, letterboxed if need be (the default);
     *  - [FILL]: edge to edge, the overflow cropped;
     *  - [STRETCH]: edge to edge, the picture distorted to fit.
     */
    enum class Aspect { FIT, FILL, STRETCH }

    fun nextAspect(current: Aspect): Aspect = Aspect.entries[(current.ordinal + 1) % Aspect.entries.size]

    fun aspectLabel(aspect: Aspect): String = when (aspect) {
        Aspect.FIT -> Strings.t.aspectFit
        Aspect.FILL -> Strings.t.aspectFill
        Aspect.STRETCH -> Strings.t.aspectStretch
    }

    /**
     * Screen orientation, in the order the rotation button cycles. A video
     * starts in [LANDSCAPE]; [AUTO] follows how the phone is held.
     */
    enum class Rotation { LANDSCAPE, PORTRAIT, AUTO }

    fun nextRotation(current: Rotation): Rotation = Rotation.entries[(current.ordinal + 1) % Rotation.entries.size]

    fun rotationLabel(rotation: Rotation): String = when (rotation) {
        Rotation.LANDSCAPE -> Strings.t.rotationLandscape
        Rotation.PORTRAIT -> Strings.t.rotationPortrait
        Rotation.AUTO -> Strings.t.rotationAuto
    }

    /**
     * What the audio picker shows for one track: "English · AAC · 5.1".
     * Language and channels only when the stream announces them; an
     * unlabelled track keeps its number so two of them stay tellable apart.
     */
    fun audioLabel(oneBasedIndex: Int, language: String?, sampleMimeType: String?, channelCount: Int): String {
        val parts = ArrayList<String>()
        if (!language.isNullOrBlank()) parts.add(languageName(language.trim()))
        sampleMimeType?.substringAfterLast('/')?.let { if (it.isNotBlank()) parts.add(codecName(it)) }
        channelLayout(channelCount)?.let { parts.add(it) }
        return if (parts.isEmpty()) "#$oneBasedIndex" else parts.joinToString(" · ")
    }

    /**
     * "en", "eng" or "pt-BR" → the language's name in the user's own locale
     * ("German" rather than "deu") — as far as the platform's locale data
     * knows the code; otherwise the code itself.
     */
    private fun languageName(code: String): String {
        val locale = runCatching { java.util.Locale.forLanguageTag(code) }.getOrNull() ?: return code
        val name = locale.displayLanguage
        return if (name.isBlank() || name.equals(code, ignoreCase = true)) code else name
    }

    /** "aac", "mp4a-latm" → "AAC"; "ac-3" → "AC-3"; anything else uppercased. */
    private fun codecName(subtype: String): String = when {
        subtype.startsWith("mp4a", true) -> "AAC"
        subtype.equals("ac-3", true) -> "AC-3"
        subtype.equals("ec-3", true) -> "E-AC-3"
        subtype.startsWith("opus", true) -> "Opus"
        else -> subtype.uppercase()
    }

    /** The usual names for channel counts; null when the stream does not announce them. */
    private fun channelLayout(channels: Int): String? = when (channels) {
        0 -> null
        1 -> "Mono"
        2 -> "Stereo"
        6 -> "5.1"
        8 -> "7.1"
        else -> channels.toString()
    }
}
