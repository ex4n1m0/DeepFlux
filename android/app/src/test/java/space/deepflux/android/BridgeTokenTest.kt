package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.browser.BrowserController

/**
 * Fix batch 2026-11-23: scripts injected into a page report back through a
 * JS bridge that EVERY frame of the page can call. Each injection carries a
 * random token; a result without the matching token is dropped.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class BridgeTokenTest {

    @Test
    fun onlyTheMatchingTokenIsAccepted() {
        assertTrue(BrowserController.bridgeTokenMatches("abc123", "abc123"))
        assertFalse("a foreign token", BrowserController.bridgeTokenMatches("abc123", "abc124"))
        assertFalse("no token in the result", BrowserController.bridgeTokenMatches("abc123", null))
        assertFalse("no token in the result (blank)", BrowserController.bridgeTokenMatches("abc123", ""))
    }

    @Test
    fun nothingIsAcceptedBeforeAScriptWasInjected() {
        // No expected token = no scan of ours is outstanding; a frame talking
        // to the bridge unprompted must be ignored.
        assertFalse(BrowserController.bridgeTokenMatches(null, "abc123"))
        assertFalse(BrowserController.bridgeTokenMatches("", "abc123"))
        assertFalse(BrowserController.bridgeTokenMatches(null, null))
    }

    @Test
    fun tokensAreRandomAndJsSafe() {
        val a = BrowserController.newBridgeToken()
        val b = BrowserController.newBridgeToken()
        assertNotEquals(a, b)
        // Hex only: no quote or backslash that could break out of the JS string literal.
        assertTrue(a, Regex("[0-9a-f]{32}").matches(a))
        assertTrue(BrowserController.scanJs(a).contains("{t:'$a',r:"))
        assertFalse(BrowserController.scanJs(a).contains("'$b'"))
    }
}
