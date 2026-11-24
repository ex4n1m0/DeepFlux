package space.deepflux.android.agent

/**
 * How tool results are handed to the model — the Android twin of the
 * desktop's `_trust: untrusted_external_content` mark, applied to EVERY
 * result.
 *
 * This agent runs always-YOLO (every tool call executes at once, owner
 * decision), so a search result or a scanned page saying "now download
 * http://…/x" must never read as an instruction. Every result arrives
 * wrapped in <untrusted> tags, and the system prompt tells the model to
 * treat it as data only. Nothing here adds a confirmation UI.
 *
 * Why every result, and not the desktop's list of web-facing tools: on this
 * app there is no result whose text is certainly the app's own. Torrent,
 * file, channel and feed names come from the web, memory entries were saved
 * from earlier results, and even an error string carries a server's
 * message. A list of "safe" tools would have to be kept right by hand
 * forever; one wrap cannot drift.
 */
object UntrustedResults {

    const val OPEN = "<untrusted>"
    const val CLOSE = "</untrusted>"

    /**
     * Any spelling of the wrapper's own tags. Content carrying
     * "</untrusted>" could close the wrapper early and present what follows
     * as app text; "<untrusted>" inside could open a decoy block the model
     * treats as closed. Also the self-closing form, tags with attributes and
     * invisible characters inside the tag name ("<untrusted\u200B>"). What
     * may follow the name is bounded, so only a tag-like span goes — not the
     * rest of the text.
     */
    private val OWN_TAG = Regex("<\\s*/?\\s*untrusted[\\s\\u200B-\\u200F\\uFEFF/=:\\w\"'-]{0,64}>", RegexOption.IGNORE_CASE)

    fun wrap(output: String): String = OPEN + "\n" + OWN_TAG.replace(output, "[removed tag]") + "\n" + CLOSE
}
