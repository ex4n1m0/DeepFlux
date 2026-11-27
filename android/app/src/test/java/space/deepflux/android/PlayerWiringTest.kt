package space.deepflux.android

import androidx.media3.common.PlaybackException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.player.CompatPolicy
import space.deepflux.android.ui.EnStrings
import space.deepflux.android.ui.ZhStrings
import java.io.File
import java.security.MessageDigest
import javax.xml.parsers.DocumentBuilderFactory

/**
 * 1.4.15: the built-in player (ExoPlayer + the FFmpeg audio fallback) and the compatibility
 * player (VLC) — when one hands over to the other, and how they are wired.
 *
 * The rule itself is pure and tested outright. The wiring is read from the sources: a JVM
 * test cannot load VLC's native libraries, and the things that go wrong here go wrong at
 * RUNTIME on a device — a class native code looks up by name after minification, a screen
 * exported by accident, a decoder extension preferred over working hardware.
 */
class PlayerWiringTest {

    // ------------------------------------------------------------------ the rule

    private fun cov(video: Int, videoOk: Int, audio: Int, audioOk: Int) =
        CompatPolicy.Coverage(videoTracks = video, videoDecodable = videoOk, audioTracks = audio, audioDecodable = audioOk)

    @Test
    fun aStreamWhoseVideoThisDeviceCannotDecodeIsHandedOver() {
        // The case this player exists for: 10-bit H.265 on a device with 8-bit-only decoders.
        val tenBitHevc = cov(video = 1, videoOk = 0, audio = 1, audioOk = 1)
        assertTrue(tenBitHevc.noDecodableVideo)
        assertTrue("handed over as soon as the tracks are known", CompatPolicy.cannotDecode(tenBitHevc))
    }

    @Test
    fun aStreamWhoseAudioThisDeviceCannotDecodeIsHandedOverToo() {
        val silent = cov(video = 1, videoOk = 1, audio = 2, audioOk = 0)
        assertTrue(silent.noDecodableAudio)
        assertTrue("a film that would play silently goes to the player that can decode it", CompatPolicy.cannotDecode(silent))
    }

    @Test
    fun aStreamThisDeviceCanDecodeStaysOnTheBuiltInPlayer() {
        assertFalse(CompatPolicy.cannotDecode(cov(video = 1, videoOk = 1, audio = 1, audioOk = 1)))
        // One decodable variant among several is enough: the others are simply not selected.
        assertFalse(CompatPolicy.cannotDecode(cov(video = 3, videoOk = 1, audio = 2, audioOk = 1)))
        // Audio only.
        assertFalse(CompatPolicy.cannotDecode(cov(video = 0, videoOk = 0, audio = 1, audioOk = 1)))
    }

    @Test
    fun aStreamWithNoTracksYetIsNotAReasonToHandOver() {
        // Before the tracks are known (or a still picture): nothing is proved undecodable.
        assertFalse(CompatPolicy.cannotDecode(cov(video = 0, videoOk = 0, audio = 0, audioOk = 0)))
    }

    @Test
    fun decodingErrorsGoToTheCompatibilityPlayer() {
        val decodeErrors = listOf(
            PlaybackException.ERROR_CODE_DECODER_INIT_FAILED,
            PlaybackException.ERROR_CODE_DECODER_QUERY_FAILED,
            PlaybackException.ERROR_CODE_DECODING_FAILED,             // what the emulator's H.265 decoders raised
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
        decodeErrors.forEach { assertTrue("code $it", CompatPolicy.isDecodeError(it)) }
    }

    @Test
    fun everythingThatIsNotADecodingProblemStaysWhereItIs() {
        // VLC would fail the same way — handing these over would only hide the real cause.
        val notDecodeErrors = listOf(
            PlaybackException.ERROR_CODE_UNSPECIFIED,
            PlaybackException.ERROR_CODE_REMOTE_ERROR,
            PlaybackException.ERROR_CODE_TIMEOUT,
            PlaybackException.ERROR_CODE_FAILED_RUNTIME_CHECK,
            PlaybackException.ERROR_CODE_IO_UNSPECIFIED,
            PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED,
            PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_TIMEOUT,
            PlaybackException.ERROR_CODE_IO_INVALID_HTTP_CONTENT_TYPE,
            PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS,
            PlaybackException.ERROR_CODE_IO_FILE_NOT_FOUND,
            PlaybackException.ERROR_CODE_IO_NO_PERMISSION,
            PlaybackException.ERROR_CODE_PARSING_CONTAINER_MALFORMED,
            PlaybackException.ERROR_CODE_PARSING_MANIFEST_MALFORMED,
            PlaybackException.ERROR_CODE_PARSING_CONTAINER_UNSUPPORTED,
            PlaybackException.ERROR_CODE_PARSING_MANIFEST_UNSUPPORTED,
            PlaybackException.ERROR_CODE_DRM_UNSPECIFIED,
            PlaybackException.ERROR_CODE_DRM_SCHEME_UNSUPPORTED,
            PlaybackException.ERROR_CODE_DRM_CONTENT_ERROR,
        )
        notDecodeErrors.forEach { assertFalse("code $it", CompatPolicy.isDecodeError(it)) }
    }

    @Test
    fun theClockReadsLikeTheBuiltInPlayers() {
        assertEquals("", CompatPolicy.clock(0))
        assertEquals("", CompatPolicy.clock(-5))
        assertEquals("00:05", CompatPolicy.clock(5_400))
        assertEquals("12:34", CompatPolicy.clock((12 * 60 + 34) * 1000L))
        assertEquals("1:05:09", CompatPolicy.clock((3600 + 5 * 60 + 9) * 1000L))
    }

    // ------------------------------------------------------------------ the wiring

    /** A source file of the app module, found from wherever the test runner starts. */
    private fun source(relative: String): String {
        var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
        val marker = "src/main/java/space/deepflux/android/player/PlayerActivity.kt"
        while (dir != null && !File(dir, marker).isFile) dir = dir.parentFile
        return dir?.let { File(it, relative).takeIf { f -> f.isFile }?.readText() }
            ?: error("not found from ${System.getProperty("user.dir")}: $relative")
    }

    private val player by lazy { source("src/main/java/space/deepflux/android/player/PlayerActivity.kt") }

    @Test
    fun theFfmpegAudioRendererIsAFallbackNeverTheFirstChoice() {
        assertTrue(
            "the FFmpeg audio renderer is not switched on",
            player.contains("setExtensionRendererMode(DefaultRenderersFactory.EXTENSION_RENDERER_MODE_ON)"),
        )
        assertFalse(
            "PREFER would displace a phone's working hardware decoder",
            player.contains("EXTENSION_RENDERER_MODE_PREFER"),
        )
    }

    @Test
    fun bothWaysIntoTheCompatibilityPlayerAreWired() {
        // Pinned as the WHOLE guard, not as "the call appears somewhere": a first version of
        // this test only looked for the call, and stayed green when the mutation switched the
        // hand-over off with `if (false && …)` (teeth check, 2026-11-27).
        assertTrue(
            "no hand-over when the tracks are known to be undecodable",
            player.contains("if (CompatPolicy.cannotDecode(CompatPolicy.Coverage("),
        )
        assertTrue(
            "no hand-over when a decoder fails at runtime",
            player.contains("if (CompatPolicy.isDecodeError(error.errorCode) && handToCompatPlayer(error.errorCodeName)) return"),
        )
        // And it never hands back: the compatibility player must not open the built-in one.
        // (Matched with a word boundary: "CompatPlayerActivity::class.java" contains the same letters.)
        val compat = source("src/main/java/space/deepflux/android/player/CompatPlayerActivity.kt")
        assertFalse(Regex("(?<![A-Za-z])PlayerActivity::class\\.java").containsMatchIn(compat))
    }

    @Test
    fun closingThePlayerClosesTheCompatibilityPlayerToo() {
        // The agent's stop tool goes through PlayerActivity.finishActive(). The compatibility
        // player is on TOP of that screen: closing only the one beneath leaves the film running.
        assertTrue(player.contains("CompatPlayerActivity.finishActive()"))
        val compat = source("src/main/java/space/deepflux/android/player/CompatPlayerActivity.kt")
        assertTrue(compat.contains("fun finishActive(): Boolean = active?.let { it.finish(); true } ?: false"))
        assertTrue(compat.contains("if (active === this) active = null"))
    }

    @Test
    fun theCompatibilityPlayerIsNotExported() {
        val manifest = source("src/main/AndroidManifest.xml")
        val doc = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
            .newDocumentBuilder().parse(manifest.byteInputStream())
        val androidNs = "http://schemas.android.com/apk/res/android"
        val activities = doc.getElementsByTagName("activity")
        var found = 0
        for (i in 0 until activities.length) {
            val el = activities.item(i) as org.w3c.dom.Element
            val name = el.getAttributeNS(androidNs, "name")
            if (name.endsWith(".player.CompatPlayerActivity")) {
                found++
                assertEquals("another app could start it with any URL", "false", el.getAttributeNS(androidNs, "exported"))
            }
        }
        assertEquals("the compatibility player is not declared", 1, found)
    }

    @Test
    fun theNativeLibrariesKeepTheirNamesUnderMinification() {
        val rules = source("proguard-rules.pro")
        assertTrue(rules.contains("-keep class org.videolan.libvlc.** { *; }"))
        // ExoPlayer finds this renderer BY NAME, and its JNI methods are looked up by name.
        assertTrue(rules.contains("class androidx.media3.decoder.ffmpeg.**"))
        assertTrue(rules.contains("native <methods>;"))
    }

    @Test
    fun theAudioExtensionIsTheOneBuiltByTheWorkflow() {
        // .github/workflows/android-ffmpeg-audio.yml builds this file; a different one must be
        // a conscious decision (rebuild, replace, change the hash here and in the commit).
        val aar = File(appModuleDir(), "libs/decoder-ffmpeg-1.11.1.aar")
        assertTrue("android/app/libs/decoder-ffmpeg-1.11.1.aar is missing", aar.isFile)
        val sha = MessageDigest.getInstance("SHA-256").digest(aar.readBytes()).joinToString("") { "%02x".format(it) }
        assertEquals("61ce9303d6823f3e2664fcb916063da2d978541f93232a39108c34e49d4fe6c7", sha)
        assertTrue(source("build.gradle.kts").contains("libs/decoder-ffmpeg-1.11.1.aar"))
    }

    private fun appModuleDir(): File {
        var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
        while (dir != null && !File(dir, "src/main/AndroidManifest.xml").isFile) dir = dir.parentFile
        return dir ?: error("app module directory not found")
    }

    @Test
    fun theNewStringsExistInBothLanguages() {
        val pairs = listOf(
            EnStrings.openingCompatPlayer to ZhStrings.openingCompatPlayer,
            EnStrings.compatPlayer to ZhStrings.compatPlayer,
            EnStrings.compatPlayerFailed to ZhStrings.compatPlayerFailed,
            EnStrings.compatPlayerUnavailable to ZhStrings.compatPlayerUnavailable,
            EnStrings.playerTracksOff to ZhStrings.playerTracksOff,
            EnStrings.openSourceLicences to ZhStrings.openSourceLicences,
        )
        pairs.forEach { (en, zh) ->
            assertTrue(en.isNotBlank())
            assertTrue(zh.isNotBlank())
            assertNotEquals("the Chinese text was forgotten", en, zh)
        }
    }

    @Test
    fun theLicenceNoticesShipWithTheLibraries() {
        // FFmpeg and VLC are LGPL: shipping them means shipping their licence. The file is an
        // asset the About card shows; it must name both libraries and carry the whole licence.
        val notices = source("src/main/assets/licenses.txt")
        listOf("FFmpeg", "https://git.ffmpeg.org/ffmpeg", "VLC", "https://code.videolan.org/videolan/vlc").forEach {
            assertTrue("the notices do not mention $it", notices.contains(it))
        }
        assertTrue(notices.contains("GNU LESSER GENERAL PUBLIC LICENSE"))
        assertTrue(notices.contains("Version 2.1, February 1999"))
        assertTrue("the licence text is cut short", notices.contains("END OF TERMS AND CONDITIONS"))
        // And the About card is what opens it.
        assertTrue(source("src/main/java/space/deepflux/android/ui/settings/SettingsScreen.kt").contains("licenses.txt"))
    }
}
