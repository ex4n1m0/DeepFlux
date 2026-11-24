package space.deepflux.android

import android.app.Application
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows
import space.deepflux.android.filecmd.DownloadOpener
import space.deepflux.android.filecmd.FileIntents
import java.io.File

/**
 * 1.4.14 — "Open" for a finished download the app cannot play itself. On the
 * emulator a PDF, an APK or an archive could not be opened from the Downloads
 * tab at all (2026-11-24); only the Commander offered a way.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class DownloadOpenerTest {

    private lateinit var context: Context
    private lateinit var file: File
    private val uri = Uri.parse("content://space.deepflux.android.fileprovider/downloads/notes.pdf")

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        // A file that is really there — one that is not is not opened at all.
        file = File(context.cacheDir, "downloads/notes.pdf").apply {
            parentFile?.mkdirs()
            writeBytes(byteArrayOf(1, 2, 3))
        }
        with(Shadows.shadowOf(android.webkit.MimeTypeMap.getSingleton())) {
            addExtensionMimeTypeMapping("pdf", "application/pdf")
            addExtensionMimeTypeMapping("apk", "application/vnd.android.package-archive")
        }
    }

    @Test
    fun theFilesOwnTypeIsAskedFor() {
        assertEquals("application/pdf", FileIntents.mimeOf("notes.pdf"))
        assertEquals("the installer listens for this one", "application/vnd.android.package-archive", FileIntents.mimeOf("DeepFlux.apk"))
        assertEquals("*/*", FileIntents.mimeOf("README"))
        assertEquals("*/*", FileIntents.mimeOf("odd.n4me!"))
    }

    @Test
    fun openAsksTheSystemToViewTheFileThroughTheProviderAddress() {
        val started = DownloadOpener(context) { uri }.open(file)
        assertTrue(started)
        val intent = Shadows.shadowOf(context as Application).nextStartedActivity
        assertEquals(Intent.ACTION_VIEW, intent.action)
        assertEquals(uri, intent.data)
        assertEquals("application/pdf", intent.type)
        assertTrue("the other app may read exactly this file", intent.flags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0)
    }

    @Test
    fun nothingInstalledForItIsReportedNotCrashed() {
        // The system now refuses an intent no app can take — as a phone would.
        Shadows.shadowOf(context as Application).checkActivities(true)
        assertFalse(DownloadOpener(context) { uri }.open(file))
    }

    @Test
    fun anAppForTheTypeMakesItStart() {
        Shadows.shadowOf(context as Application).checkActivities(true)
        val viewer = ComponentName("other.app", "other.app.Viewer")
        Shadows.shadowOf(context.packageManager).addActivityIfNotPresent(viewer)
        Shadows.shadowOf(context.packageManager).addIntentFilterForActivity(
            viewer,
            // what a real viewer declares: the action, the type, the default
            // category and the addresses it reads
            IntentFilter().apply {
                addAction(Intent.ACTION_VIEW)
                addCategory(Intent.CATEGORY_DEFAULT)
                addDataType("application/pdf")
                addDataScheme("content")
            },
        )
        // With `checkActivities` on, the system refuses an intent no app takes —
        // so this passing means the viewer really was resolved.
        assertTrue(DownloadOpener(context) { uri }.open(file))
        val started = Shadows.shadowOf(context as Application).nextStartedActivity
        assertEquals("an implicit intent — the system picks the app", null, started.component)
        val takers = context.packageManager.queryIntentActivities(started, 0)
            .map { it.activityInfo?.let { a -> ComponentName(a.packageName, a.name) } }
        assertEquals(listOf(viewer), takers)
    }

    @Test
    fun aFileThatIsNotThereIsNotOpened() {
        // Deleted meanwhile (Commander, another app): a viewer would have been handed a dead address.
        assertFalse(DownloadOpener(context) { uri }.open(File("downloads", "missing.pdf")))
        assertNull("nothing was started", Shadows.shadowOf(context as Application).peekNextStartedActivity())
    }

    @Test
    fun aFileThisAppMayNotHandOutIsNotOpened() {
        val opener = DownloadOpener(context) { throw IllegalArgumentException("Failed to find configured root") }
        val outside = File(context.cacheDir, "elsewhere/secret.txt").apply {
            parentFile?.mkdirs()
            writeText("x")
        }
        assertFalse(opener.open(outside))
        assertNull("nothing was started", Shadows.shadowOf(context as Application).peekNextStartedActivity())
    }
}
