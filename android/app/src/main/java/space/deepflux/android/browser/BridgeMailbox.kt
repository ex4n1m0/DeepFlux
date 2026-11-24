package space.deepflux.android.browser

import java.util.concurrent.ConcurrentHashMap

/**
 * The bookkeeping behind the page-facing JS bridge: which injected script
 * may report for a tab, and which in-page .torrent fetch is waiting for its
 * result. No Android types, so it is tested without a WebView.
 *
 * `window.deepflux` is callable from EVERY frame of a page, so whatever
 * reaches it is untrusted input that may also arrive in floods (review round
 * 2026-11-24):
 *  - an envelope is read by its token first — string work only, an unknown
 *    token is refused before any JSON is parsed ([envelopeToken]);
 *  - a result settles exactly the fetch with ITS token. There used to be one
 *    shared slot, emptied by any call at all: one junk call from any frame
 *    cancelled the torrent the user had just tapped;
 *  - fetches are held per token, so two tabs (or two taps) never replace
 *    each other.
 */
internal class BridgeMailbox {

    /**
     * One in-page .torrent fetch. [onDone] runs exactly once, with the
     * fetch's parsed result (the controller's own type) or null for "no result".
     */
    class PendingFetch(val tabId: Int, val onDone: (Any?) -> Unit)

    /** Latest scan token per tab: only the script injected last may report for it. Blank = nothing may. */
    private val scanTokens = ConcurrentHashMap<Int, String>()

    /** .torrent fetches waiting for their result, by token. */
    private val pendingFetches = ConcurrentHashMap<String, PendingFetch>()

    fun scanStarted(tabId: Int, token: String) {
        scanTokens[tabId] = token
    }

    /**
     * Bridge thread: CLAIMS the right of the script with [token] to report
     * for [tabId]. True once — the token is consumed — and only while it is
     * the one injected last. A main-frame page can capture a token by
     * replacing `window.deepflux`; a token that stayed valid let every later
     * call with it queue work on the main thread (second security review).
     */
    fun claimScan(tabId: Int, token: String?): Boolean =
        !token.isNullOrBlank() && scanTokens.remove(tabId, token)

    fun awaitFetch(tabId: Int, token: String, onDone: (Any?) -> Unit) {
        pendingFetches[token] = PendingFetch(tabId, onDone)
    }

    /** Bridge thread: is a fetch waiting for [token]? */
    fun isAwaiting(token: String?): Boolean = !token.isNullOrBlank() && pendingFetches.containsKey(token)

    /**
     * The fetch waiting for [token], removed — or null when that fetch was
     * already settled (its result or its timeout came first) or never
     * existed. A foreign token touches nothing else.
     */
    fun takeFetch(token: String): PendingFetch? = pendingFetches.remove(token)

    /** How many fetches are waiting right now (tests). */
    val awaitingCount: Int get() = pendingFetches.size

    /**
     * Settles every fetch of the tab with "no result" — it was closed, or
     * its renderer died. Each falls back to the plain HTTP lane at once
     * instead of waiting out its timeout.
     */
    fun abandonTab(tabId: Int) {
        for ((token, fetch) in pendingFetches) {
            if (fetch.tabId == tabId && pendingFetches.remove(token, fetch)) fetch.onDone(null)
        }
    }

    /** The tab is gone: nothing may report for it any more. */
    fun forgetTab(tabId: Int) {
        scanTokens.remove(tabId)
    }

    companion object {
        /** Length of a token as [newBridgeToken] makes it: a UUID without dashes. */
        private const val TOKEN_LENGTH = 32

        /**
         * The token of a `{t, r}` envelope as our own scripts build it —
         * `{"t":"<token>","r":…}`, or `{"t":"<token>"}` alone ("no result") —
         * by string positions only, so junk is cheap to refuse. Null for
         * anything that is not that shape. The payload itself is not looked
         * at here.
         */
        fun envelopeToken(raw: String?): String? {
            if (raw == null) return null
            val start = 6 // right after `{"t":"`
            val end = start + TOKEN_LENGTH
            if (raw.length <= end + 1 || !raw.startsWith("{\"t\":\"") || raw[end] != '"') return null
            val token = raw.substring(start, end)
            // Hex only, like the tokens themselves: nothing else is worth a lookup.
            if (!token.all { it in '0'..'9' || it in 'a'..'f' }) return null
            val after = raw[end + 1]
            // What may follow the token's closing quote: the end of the
            // envelope, or its payload.
            if (after != '}' && !(after == ',' && raw.startsWith(",\"r\":", end + 1))) return null
            return token
        }
    }
}

/**
 * How many times a tab's renderer may die in a row before its page is left
 * alone. A page that kills the renderer deterministically (a memory bomb)
 * otherwise gets an endless crash → recreate → reload round, each round with
 * a notice. Pure: the caller supplies the clock.
 */
internal class CrashWindow(
    private val limit: Int = LIMIT,
    private val windowMs: Long = WINDOW_MS,
) {
    private val times = ArrayList<Long>()

    /** Records a crash at [now]; true when the limit is reached and the page must NOT be reloaded. */
    fun crashed(now: Long): Boolean {
        times.removeAll { now - it > windowMs }
        times.add(now)
        return times.size >= limit
    }

    fun reset() = times.clear()

    companion object {
        const val LIMIT = 3
        const val WINDOW_MS = 30_000L
    }
}
