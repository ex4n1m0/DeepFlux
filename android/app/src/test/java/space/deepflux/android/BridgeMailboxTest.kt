package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.browser.BridgeMailbox
import space.deepflux.android.browser.CrashWindow

/**
 * Review round 2026-11-24 — the page-facing JS bridge, which every frame of
 * a page can call:
 *  - an in-page .torrent fetch used to wait in ONE shared slot that ANY call
 *    emptied, so one junk call from any frame cancelled the torrent the user
 *    had just tapped — and two tabs replaced each other's fetch;
 *  - every call, junk included, was parsed as JSON before being rejected;
 *  - a tab whose page killed its renderer was reloaded into the same crash
 *    forever.
 */
class BridgeMailboxTest {

    private fun token(i: Int) = "%032x".format(i)
    private fun envelope(t: String, payload: String = "{}") = """{"t":"$t","r":$payload}"""
    private fun settle(t: String) = """{"t":"$t"}"""

    /** What the page's own `fetch()` built, as the bridge hands it over. */
    private fun result(t: String, b64: String) = """{"t":"$t","r":{"url":"https://site.example/a.torrent","b64":"$b64"}}"""

    // ---------------------------------------------------------------- in-page .torrent fetches

    @Test
    fun aResultSettlesExactlyTheFetchWithItsToken() {
        val m = BridgeMailbox()
        val got = ArrayList<String?>()
        m.awaitFetch(1, token(1)) { got.add("tab1:$it") }
        m.awaitFetch(2, token(2)) { got.add("tab2:$it") }

        // Two taps in two tabs: neither knows of the other.
        assertTrue(m.isAwaiting(token(1)))
        assertTrue(m.isAwaiting(token(2)))
        m.takeFetch(token(2))!!.onDone("B")
        assertEquals(listOf("tab2:B"), got)
        assertTrue("tab 1's fetch survived tab 2's result", m.isAwaiting(token(1)))
        m.takeFetch(token(1))!!.onDone("A")
        assertEquals(listOf("tab2:B", "tab1:A"), got)
        assertEquals(0, m.awaitingCount)
    }

    @Test
    fun aForeignOrStaleTokenIsRefusedAndConsumesNothing() {
        val m = BridgeMailbox()
        val mine = token(7)
        m.awaitFetch(1, mine) {}
        val junk = listOf(
            null, "", "not json", "{}", """{"r":{"b64":"AAAA"}}""",
            envelope(token(8)), // another fetch's token, or a guess
            """{"t":"${token(7)}x","r":{}}""", // our token, but a different length
            result(mine, "AAAA").substring(1), // shifted by one character
            "x".repeat(500_000),
        )
        for (raw in junk) {
            val t = BridgeMailbox.envelopeToken(raw)
            assertTrue("refused: '$raw'", t == null || t != mine)
            assertNull("nothing is taken for junk", t?.let { m.takeFetch(it) })
        }
        assertTrue("the real fetch is still waiting after all that", m.isAwaiting(mine))
        m.takeFetch(mine)!!.onDone("ok")
        assertFalse("settled once", m.isAwaiting(mine))
    }

    @Test
    fun aFetchIsSettledOnceEvenWhenItsTimeoutFollowsItsResult() {
        val m = BridgeMailbox()
        var calls = 0
        m.awaitFetch(1, token(3)) { calls++ }
        m.takeFetch(token(3))!!.onDone("result")
        assertNull("the result came first: the timeout finds nothing", m.takeFetch(token(3)))
        assertEquals(1, calls)
    }

    @Test
    fun closingATabSettlesItsFetchesAtOnceAndOnlyIts() {
        val m = BridgeMailbox()
        val settled = ArrayList<String>()
        m.awaitFetch(1, token(1)) { settled.add("tab1") }
        m.awaitFetch(1, token(11)) { settled.add("tab1 again") }
        m.awaitFetch(2, token(2)) { settled.add("tab2") }
        m.scanStarted(1, token(100))

        m.abandonTab(1)
        m.forgetTab(1)
        assertEquals(setOf("tab1", "tab1 again"), settled.toSet())
        assertEquals("the other tab's fetch is untouched", 1, m.awaitingCount)
        assertTrue(m.isAwaiting(token(2)))
        assertFalse("and nothing may report for the closed tab", m.claimScan(1, token(100)))
    }

    // ---------------------------------------------------------------- the envelope's token

    @Test
    fun theTokenIsReadFromOurOwnEnvelopeShapeOnly() {
        val real = java.util.UUID.randomUUID().toString().replace("-", "") // as newBridgeToken makes it
        assertEquals(real, BridgeMailbox.envelopeToken("""{"t":"$real","r":[]}"""))
        assertEquals(
            "a token-only envelope — how a timeout is expressed",
            real,
            BridgeMailbox.envelopeToken("""{"t":"$real"}"""),
        )
        for (notOurs in listOf(null, "", "{}", """{"r":{},"t":"$real"}""", """{"t":"$real""", """{"t":"$real" """, """{"t":"$real"x}""", """{"t":"$real","x":{}}""", """{"T":"$real","r":{}}""", """[1,2,3]""", "x")) {
            assertNull("'$notOurs'", BridgeMailbox.envelopeToken(notOurs))
        }
        // Hex only: something of the right length that is not a token is not looked up.
        assertNull(BridgeMailbox.envelopeToken("""{"t":"zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz","r":{}}"""))
        assertNull(BridgeMailbox.envelopeToken("""{"t":"${"0".repeat(31)}","r":{}}"""))
        assertNull(BridgeMailbox.envelopeToken("""{"t":"${"0".repeat(33)}","r":{}}"""))
    }

    // ---------------------------------------------------------------- video reports

    @Test
    fun onlyTheLastInjectedScanMayReportForATab() {
        val m = BridgeMailbox()
        assertFalse("no scan injected yet", m.claimScan(1, token(1)))
        m.scanStarted(1, token(1))
        assertFalse("another tab's token", m.claimScan(2, token(1)))
        m.scanStarted(1, token(2))
        assertFalse("superseded by the next injection", m.claimScan(1, token(1)))
        assertTrue(m.claimScan(1, token(2)))
        m.scanStarted(1, token(3))
        m.forgetTab(1)
        assertFalse("the tab is gone", m.claimScan(1, token(3)))
    }

    @Test
    fun aScanTokenIsGoodForExactlyOneReport() {
        val m = BridgeMailbox()
        m.scanStarted(1, token(1))
        assertTrue("the script's own report", m.claimScan(1, token(1)))
        // A main-frame page can capture the token by replacing `window.deepflux`.
        // Before the claim, every call with it queued its string on the main thread.
        repeat(1000) { assertFalse("a replay with a captured token (call $it)", m.claimScan(1, token(1))) }
    }

    // ---------------------------------------------------------------- crash loop

    @Test
    fun aPageIsReloadedAfterAStrayCrashButNotWhenItKeepsKillingTheRenderer() {
        val w = CrashWindow(limit = 3, windowMs = 30_000)
        assertFalse("first crash: reload", w.crashed(now = 1_000))
        assertFalse("second crash: give the page one more chance", w.crashed(now = 2_000))
        assertTrue("third crash in half a minute: leave it alone", w.crashed(now = 3_000))
    }

    @Test
    fun crashesFarApartAreNotALoop() {
        val w = CrashWindow(limit = 3, windowMs = 30_000)
        assertFalse(w.crashed(now = 0))
        assertFalse(w.crashed(now = 60_000))
        assertFalse("the first one is outside the window by now", w.crashed(now = 120_000))
    }
}
