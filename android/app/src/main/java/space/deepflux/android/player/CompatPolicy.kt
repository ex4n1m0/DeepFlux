package space.deepflux.android.player

import androidx.media3.common.PlaybackException

/**
 * When the built-in player (ExoPlayer) hands a stream to the compatibility player
 * (VLC — [CompatPlayerActivity]). 1.4.15.
 *
 * ExoPlayer decodes with the phone's own decoders plus the FFmpeg AUDIO fallback
 * (`DefaultRenderersFactory.EXTENSION_RENDERER_MODE_ON`). It has no software VIDEO
 * decoder, so a phone without the hardware for a format can neither show those films
 * nor tell why before it tries: 10-bit H.265 is the common case (47 % of the films in
 * the owner's TV catalogue; on a device with 8-bit-only H.265 decoders every one of
 * them stopped with "Playback failed", emulator 2026-11-27). VLC decodes in software
 * when the hardware cannot.
 *
 * The compatibility player is therefore used EXACTLY when the built-in one provably
 * cannot decode the stream — never by default, never for a network or server failure
 * (VLC would fail the same way), and never for DRM (VLC cannot either).
 *
 * Pure functions on purpose: nothing here needs a player, so this is unit-tested on
 * the JVM.
 */
object CompatPolicy {

    /** What the built-in player can decode of the stream's tracks. */
    data class Coverage(
        val videoTracks: Int,
        val videoDecodable: Int,
        val audioTracks: Int,
        val audioDecodable: Int,
    ) {
        /** There is video, and not one track of it can be decoded here: no picture at all. */
        val noDecodableVideo: Boolean get() = videoTracks > 0 && videoDecodable == 0

        /** There is audio, and not one track of it can be decoded here: the film would be silent. */
        val noDecodableAudio: Boolean get() = audioTracks > 0 && audioDecodable == 0
    }

    /**
     * The stream has a whole kind of track this device cannot decode: hand it over as
     * soon as the tracks are known — before a decoder is started and fails.
     */
    fun cannotDecode(c: Coverage): Boolean = c.noDecodableVideo || c.noDecodableAudio

    /**
     * A playback error that means "this device cannot decode it", as opposed to "the
     * stream or the network is broken". Covers what the track check cannot see: a decoder
     * that is listed for the format and still refuses or dies on it.
     */
    fun isDecodeError(errorCode: Int): Boolean = errorCode in DECODE_ERRORS

    private val DECODE_ERRORS = setOf(
        PlaybackException.ERROR_CODE_DECODER_INIT_FAILED,
        PlaybackException.ERROR_CODE_DECODER_QUERY_FAILED,
        PlaybackException.ERROR_CODE_DECODING_FAILED,
        PlaybackException.ERROR_CODE_DECODING_FORMAT_EXCEEDS_CAPABILITIES,
        PlaybackException.ERROR_CODE_DECODING_FORMAT_UNSUPPORTED,
        PlaybackException.ERROR_CODE_DECODING_RESOURCES_RECLAIMED,
        PlaybackException.ERROR_CODE_AUDIO_TRACK_INIT_FAILED,
        PlaybackException.ERROR_CODE_AUDIO_TRACK_WRITE_FAILED,
        PlaybackException.ERROR_CODE_AUDIO_TRACK_OFFLOAD_WRITE_FAILED,
        PlaybackException.ERROR_CODE_AUDIO_TRACK_OFFLOAD_INIT_FAILED,
        PlaybackException.ERROR_CODE_VIDEO_FRAME_PROCESSOR_INIT_FAILED,
        PlaybackException.ERROR_CODE_VIDEO_FRAME_PROCESSING_FAILED,
    )

    /** "12:34" or "1:05:09" from milliseconds; "" when there is nothing to show yet. */
    fun clock(ms: Long): String {
        if (ms <= 0) return ""
        val totalSeconds = ms / 1000
        val h = totalSeconds / 3600
        val m = (totalSeconds % 3600) / 60
        val s = totalSeconds % 60
        return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%02d:%02d".format(m, s)
    }
}
