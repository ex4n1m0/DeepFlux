package space.deepflux.android

import android.app.Application
import android.content.Context
import android.content.Intent
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.android.controller.ActivityController
import space.deepflux.android.player.PlayerActivity
import space.deepflux.android.ui.Strings

/**
 * The player opens with its controls (2026-11-24): the top bar carries the
 * rotation and aspect buttons next to ✕ and CC, the audio-track button is
 * there but hidden until the stream is known to have more than one track,
 * and opening the player on an unreachable stream ends in the error UI, not
 * in a crash. What the controls DO on a device (focus, immersion, the
 * track switch itself) needs a device.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class PlayerActivityTest {

    private fun views(root: View): List<View> {
        val out = ArrayList<View>()
        out.add(root)
        if (root is ViewGroup) for (i in 0 until root.childCount) out += views(root.getChildAt(i))
        return out
    }

    @Test
    fun thePlayerOpensWithItsControls() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        val intent = Intent(ctx, PlayerActivity::class.java)
            .putExtra(PlayerActivity.EXTRA_URL, "http://127.0.0.1:9/none.mp4")
            .putExtra(PlayerActivity.EXTRA_TITLE, "Nothing")
        val controller: ActivityController<PlayerActivity> = Robolectric.buildActivity(PlayerActivity::class.java, intent)
        val activity = controller.create().get()
        try {
            val labels = views(activity.window.decorView).filterIsInstance<TextView>()
                .map { it.contentDescription?.toString().orEmpty() }
            assertTrue("rotation button: $labels", Strings.t.playerRotation in labels)
            assertTrue("aspect button: $labels", Strings.t.playerAspect in labels)
            assertTrue("the way out: $labels", Strings.t.closePlayer in labels)

            val audio = views(activity.window.decorView).filterIsInstance<TextView>()
                .first { it.contentDescription?.toString() == Strings.t.playerAudioTrack }
            assertEquals("no audio picker until the stream has several audio tracks", View.GONE, audio.visibility)
        } finally {
            controller.close()
        }
    }

    @Test
    fun closingThePlayerIsCleanEvenWhenTheStreamNeverLoaded() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        val intent = Intent(ctx, PlayerActivity::class.java)
            .putExtra(PlayerActivity.EXTRA_URL, "http://127.0.0.1:9/none.mp4")
        val controller: ActivityController<PlayerActivity> = Robolectric.buildActivity(PlayerActivity::class.java, intent)
        controller.create().start().resume()
        controller.pause().stop().close()
        assertTrue("closed", !PlayerActivity.isActive)
    }
}
