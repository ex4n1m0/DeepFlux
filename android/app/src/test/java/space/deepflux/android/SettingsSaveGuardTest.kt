package space.deepflux.android

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.ui.Strings
import java.io.File
import java.io.IOException
import java.util.concurrent.CopyOnWriteArrayList

/**
 * 1.4.14 — a settings save that fails is told, not fatal. Every save used
 * to be a plain background coroutine: the error escaped it — and on a phone
 * an error escaping a coroutine ends the process (found 2026-11-25, when a
 * save that failed on the build machine's disk took the test run down).
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class SettingsSaveGuardTest {

    private lateinit var repo: TorrentRepository
    private val seen = CopyOnWriteArrayList<String>()
    private var collector: Job? = null
    private var before: Thread.UncaughtExceptionHandler? = null
    private val uncaught = CopyOnWriteArrayList<Throwable>()

    @Before
    fun setUp() {
        repo = TorrentRepository(ApplicationProvider.getApplicationContext(), FakeTorrentEngine())
        collector = CoroutineScope(Dispatchers.Unconfined).launch { repo.notices.collect { seen.add(it) } }
        // What a crash is: an error no coroutine caught reaches the thread's handler.
        before = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { _, e -> uncaught.add(e) }
    }

    @After
    fun tearDown() {
        Thread.setDefaultUncaughtExceptionHandler(before)
        collector?.cancel()
        repo.shutdownForTests()
    }

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 10_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(25)
        }
    }

    @Test
    fun aSaveThatFailsIsToldAndIsNotFatal() {
        repo.saveSetting { throw IOException("No space left on device") }

        await("the notice") { Strings.t.noticeSettingNotSaved in seen }
        // The coroutine is long done by now; give a stray report a moment to arrive.
        Thread.sleep(300)
        assertTrue(
            "the error reached the thread's uncaught handler — on a phone that is a crash",
            uncaught.isEmpty(),
        )
        assertTrue("said once", seen.count { it == Strings.t.noticeSettingNotSaved } == 1)
    }

    @Test
    fun theOthersAreNotStoppedByIt() {
        repo.saveSetting { throw IOException("No space left on device") }
        repo.setUpdatesAutoCheck(false)

        await("the later save") { runBlocking { repo.appSettings.prefsSnapshot().updatesAutoCheck } == false }
        Thread.sleep(300)
        assertTrue(uncaught.isEmpty())
    }

    @Test
    fun noSettingIsSavedWithoutTheGuard() {
        // Every setter goes through saveSetting — this reads the source, since a
        // bare launch and a guarded one behave the same until the day the disk fails.
        var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
        while (dir != null && !File(dir, "src/main/java/space/deepflux/android/engine/TorrentRepository.kt").isFile) {
            dir = dir.parentFile
        }
        val source = dir?.let { File(it, "src/main/java/space/deepflux/android/engine/TorrentRepository.kt").readText() }
        assertTrue("could not find the repository's source from the test's working folder", source != null)
        val bare = Regex("""scope\.launch\s*\{\s*settings\.""").findAll(source!!).map { it.value }.toList()
        assertTrue("saved without the guard: $bare", bare.isEmpty())
    }
}
