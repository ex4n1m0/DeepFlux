package space.deepflux.android

import android.app.Application
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.player.PlayerModes
import space.deepflux.android.ui.Strings

/** The player's cycling controls (rotation, aspect) and the audio picker's labels. */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class PlayerModesTest {

    @Test
    fun aspectCyclesThroughAllThreeAndBack() {
        assertEquals(PlayerModes.Aspect.FILL, PlayerModes.nextAspect(PlayerModes.Aspect.FIT))
        assertEquals(PlayerModes.Aspect.STRETCH, PlayerModes.nextAspect(PlayerModes.Aspect.FILL))
        assertEquals("…and back to the default", PlayerModes.Aspect.FIT, PlayerModes.nextAspect(PlayerModes.Aspect.STRETCH))
        assertEquals(Strings.t.aspectFit, PlayerModes.aspectLabel(PlayerModes.Aspect.FIT))
        assertEquals(Strings.t.aspectFill, PlayerModes.aspectLabel(PlayerModes.Aspect.FILL))
        assertEquals(Strings.t.aspectStretch, PlayerModes.aspectLabel(PlayerModes.Aspect.STRETCH))
    }

    @Test
    fun rotationCyclesLandscapePortraitAuto() {
        assertEquals(PlayerModes.Rotation.PORTRAIT, PlayerModes.nextRotation(PlayerModes.Rotation.LANDSCAPE))
        assertEquals(PlayerModes.Rotation.AUTO, PlayerModes.nextRotation(PlayerModes.Rotation.PORTRAIT))
        assertEquals(PlayerModes.Rotation.LANDSCAPE, PlayerModes.nextRotation(PlayerModes.Rotation.AUTO))
        assertEquals(Strings.t.rotationLandscape, PlayerModes.rotationLabel(PlayerModes.Rotation.LANDSCAPE))
        assertEquals(Strings.t.rotationPortrait, PlayerModes.rotationLabel(PlayerModes.Rotation.PORTRAIT))
        assertEquals(Strings.t.rotationAuto, PlayerModes.rotationLabel(PlayerModes.Rotation.AUTO))
    }

    @Test
    fun anAudioTrackIsNamedByWhatTheStreamAnnounces() {
        assertEquals("English · AAC · 5.1", PlayerModes.audioLabel(1, "English", "audio/mp4a-latm", 6))
        // An ISO code becomes the language's name when the platform's locale
        // data knows it (a device does; Robolectric has none and keeps the code).
        val german = PlayerModes.audioLabel(2, "deu", "audio/ac-3", 0)
        org.junit.Assert.assertTrue("got '$german'", german == "German · AC-3" || german == "deu · AC-3")
        assertEquals("AAC", PlayerModes.audioLabel(1, "", "audio/mp4a-latm", 0))
        assertEquals("Opus · Stereo", PlayerModes.audioLabel(1, null, "audio/opus", 2))
        assertEquals("an exotic layout shows as its channel count", "12", PlayerModes.audioLabel(1, null, null, 12))
        // Unlabelled tracks keep their number, so two of them stay tellable apart.
        assertEquals("#1", PlayerModes.audioLabel(1, null, "", 0))
        assertEquals("#2", PlayerModes.audioLabel(2, "  ", null, 0))
    }
}
