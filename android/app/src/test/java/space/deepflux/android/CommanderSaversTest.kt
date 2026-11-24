package space.deepflux.android

import androidx.compose.runtime.saveable.Saver
import androidx.compose.runtime.saveable.SaverScope
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import space.deepflux.android.ui.commander.SafStackSaver
import space.deepflux.android.ui.commander.SelectionSaver

/**
 * The Commander pane's navigation state as rememberSaveable hands it to the
 * saved-state bundle (fix batch 2026-11-23: both panes used to come back at
 * the storage root on every app-tab switch). These are the screen's own savers.
 */
class CommanderSaversTest {

    /** Only Strings survive a Bundle here — exactly what the savers may emit. */
    private val scope = object : SaverScope {
        override fun canBeSaved(value: Any): Boolean = value is String
    }

    /**
     * What the pane sees back after a save/restore cycle. rememberSaveable's
     * rule: "nothing saved" (an empty value) restores as the initial value —
     * which is the same empty list/set the pane starts from.
     */
    private fun <T, S : Any> roundTrip(saver: Saver<T, S>, value: T): T {
        val saved = with(saver) { scope.save(value) } ?: return value
        return saver.restore(saved) ?: value
    }

    @Test
    fun safNavigationStackSurvivesASaveRestoreRoundTrip() {
        val stack = listOf(
            "content://com.android.externalstorage.documents/tree/primary%3AMovies/document/primary%3AMovies" to "Movies",
            "content://x/document/y" to "Season 1",
        )
        assertEquals(stack, roundTrip(SafStackSaver, stack))
    }

    @Test
    fun emptyStackRestoresEmpty() {
        assertEquals(emptyList<Pair<String, String>>(), roundTrip(SafStackSaver, emptyList()))
    }

    @Test
    fun namesWithSlashesAndEqualsSurvive() {
        val stack = listOf("content://a/b?x=1" to "A/B = C")
        assertEquals(stack, roundTrip(SafStackSaver, stack))
    }

    @Test
    fun selectionRoundTripsAndKeepsNothingExtra() {
        assertEquals(setOf("/a/b.bin", "/a/c.bin"), roundTrip(SelectionSaver, setOf("/a/b.bin", "/a/c.bin")))
        assertEquals(emptySet<String>(), roundTrip(SelectionSaver, emptySet()))
    }
}
