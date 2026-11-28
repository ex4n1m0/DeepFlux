package space.deepflux.android.ui.chat

import androidx.compose.foundation.horizontalScroll
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.widget.Toast
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.LinkInteractionListener
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withLink
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.commonmark.ext.gfm.tables.TableBlock
import org.commonmark.ext.gfm.tables.TableCell
import org.commonmark.ext.gfm.tables.TableBody
import org.commonmark.ext.gfm.tables.TableHead
import org.commonmark.ext.gfm.tables.TableRow
import org.commonmark.ext.gfm.tables.TablesExtension
import org.commonmark.ext.autolink.AutolinkExtension
import org.commonmark.node.BlockQuote
import org.commonmark.node.BulletList
import org.commonmark.node.Code
import org.commonmark.node.Emphasis
import org.commonmark.node.FencedCodeBlock
import org.commonmark.node.HardLineBreak
import org.commonmark.node.HtmlInline
import org.commonmark.node.Heading
import org.commonmark.node.HtmlBlock
import org.commonmark.node.Image
import org.commonmark.node.IndentedCodeBlock
import org.commonmark.node.Link
import org.commonmark.node.ListItem
import org.commonmark.node.Node
import org.commonmark.node.OrderedList
import org.commonmark.node.Paragraph
import org.commonmark.node.SoftLineBreak
import org.commonmark.node.StrongEmphasis
import org.commonmark.node.Text
import org.commonmark.node.ThematicBreak
import org.commonmark.parser.Parser
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.DimText

/** What a tapped link in a chat message asks the app to do. */
sealed class LinkOpen {
    /** A magnet link — offered as a torrent (the user confirms the add). */
    data class Magnet(val magnet: String) : LinkOpen()

    /** An http(s) address — opened in the in-app browser. */
    data class Web(val url: String) : LinkOpen()
}

/** CommonMark with GFM tables and bare-URL autolinking. */
fun chatMarkdownParser(): Parser = Parser.builder()
    .extensions(listOf(TablesExtension.create(), AutolinkExtension.create()))
    .build()

private val PARSER: Parser by lazy { chatMarkdownParser() }

private val CODE_BG = Color(0xFF1A2A4A)

private fun mono(): TextStyle = TextStyle(fontFamily = FontFamily.Monospace, fontSize = 13.sp, lineHeight = 18.sp)

private fun linkStyles() = TextLinkStyles(
    style = SpanStyle(color = Accent, textDecoration = TextDecoration.Underline),
)

/**
 * Which links may be tapped: http(s) pages and magnets. Anything else a
 * model (or an injected page) emits — javascript:, data:, file:, intent: —
 * stays inert text.
 */
internal fun linkOpenFor(destination: String): LinkOpen? {
    val d = destination.trim()
    return when {
        d.startsWith("magnet:", ignoreCase = true) -> LinkOpen.Magnet(d)
        d.startsWith("http://", ignoreCase = true) || d.startsWith("https://", ignoreCase = true) -> LinkOpen.Web(d)
        else -> null
    }
}

/**
 * Renders a model reply as markdown (fix batch 2, 2026-11-24). Replies are
 * markdown — bold, lists, code, tables, links — and used to be shown as
 * raw text ("**bold**", "#" and table pipes). Parsing is commonmark-java;
 * everything Compose-side lives here: inline styles, link taps through
 * [onLink], code blocks with a Copy button, lists, quotes and tables.
 *
 * Reparsed whenever [markdown] changes — while a reply streams, that is
 * once per token; a half-written "**bo" simply shows literally until closed.
 */
@Composable
fun MarkdownText(
    markdown: String,
    modifier: Modifier = Modifier,
    onLink: (LinkOpen) -> Unit,
) {
    // Null only when nesting was too deep even for the guarded text: shown as it is, then.
    val root = remember(markdown) { parseSafely(markdown) }
    if (root == null) {
        Text(markdown, style = MaterialTheme.typography.bodyMedium, modifier = modifier)
        return
    }
    Column(modifier = modifier, verticalArrangement = Arrangement.spacedBy(4.dp)) {
        RenderBlocks(root, onLink, depth = 0)
    }
}

/**
 * The document, or null when the parser overflowed on the nesting (1.4.16; review). The
 * PARSER recurses per level of blocks inside blocks — measured on this JVM: fine at 6,400
 * quotes in a row, over the stack well before 20,000 — so the renderer's own depth limit
 * alone did not save the app; a reply like that, saved as a task, would have crashed it at
 * every reopen. Two walls: [parseable] first, and the overflow caught last.
 */
internal fun parseSafely(text: String): Node? = try {
    PARSER.parse(parseable(text))
} catch (e: StackOverflowError) {
    null
}

/** Quote markers one line may open onto the parser; a longer run is cut to this. */
private const val QUOTE_RUN_LIMIT = 24

/**
 * The text with absurd nesting taken out before the parser sees it: a line opening with
 * more than [QUOTE_RUN_LIMIT] quote markers keeps that many, then an ellipsis — the surplus
 * markers are dropped (they carry nothing). ">>>>…" is one block per character to the
 * parser, and each level is a frame on the app's thread.
 *
 * Counted in a loop, NOT with a regex of a repeated group: Java's regex engine recurses per
 * repetition and overflowed on the very run this guards against (measured 2026-11-28).
 */
internal fun parseable(text: String): String {
    if ('>' !in text) return text
    val out = ArrayList<String>()
    var changed = false
    for (line in text.lineSequence()) {
        var i = 0
        var markers = 0
        while (i < line.length) {
            val c = line[i]
            if (c == '>') markers++ else if (c != ' ' && c != '\t') break
            i++
        }
        if (markers > QUOTE_RUN_LIMIT) {
            out += "> ".repeat(QUOTE_RUN_LIMIT) + "… " + line.substring(i).trimStart()
            changed = true
        } else {
            out += line
        }
    }
    return if (changed) out.joinToString("\n") else text
}

@Composable
private fun RenderBlocks(parent: Node, onLink: (LinkOpen) -> Unit, depth: Int) {
    var node = parent.firstChild
    while (node != null) {
        RenderBlock(node, onLink, depth)
        node = node.next
    }
}

@Composable
private fun RenderBlock(node: Node, onLink: (LinkOpen) -> Unit, depth: Int) {
    // Past the limit a block is shown as its plain text, taken iteratively: a pathological
    // reply must not recurse without end — ">>>>…" is one quote per character, "- - - -" a
    // list per dash. (Until 1.4.16 a quote inside a quote did not count as a level at all;
    // and since replies are saved as tasks, such a one would have crashed the app at every
    // reopen.) Text and paragraphs are shown as they are at any depth.
    if (depth >= MAX_DEPTH && !isTextual(node)) {
        Text(flatTextOf(node), style = MaterialTheme.typography.bodyMedium)
        return
    }
    when (node) {
        is Paragraph -> Text(inlineMarkdown(node, onLink), style = MaterialTheme.typography.bodyMedium)
        is Heading -> Text(
            inlineMarkdown(node, onLink),
            style = when (node.level) {
                1 -> MaterialTheme.typography.titleLarge
                2 -> MaterialTheme.typography.titleMedium
                3 -> MaterialTheme.typography.titleSmall
                else -> MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold)
            },
            modifier = Modifier.padding(top = 4.dp),
        )
        is BulletList -> RenderList(node, onLink, depth, marker = { "•" })
        is OrderedList -> {
            // The number written in the marker ("3." starts at 3); 1 when absent.
            var number = node.markerStartNumber ?: 1
            RenderList(node, onLink, depth, marker = { "${number++}." })
        }
        is BlockQuote -> Surface(
            color = Color(0x14FFFFFF),
            shape = RoundedCornerShape(6.dp),
            modifier = Modifier.padding(vertical = 2.dp),
        ) {
            Column(modifier = Modifier.padding(start = 10.dp, top = 4.dp, end = 10.dp, bottom = 4.dp)) {
                // A quote's paragraphs inside carry their own text style. One level deeper:
                // a quote in a quote is a block in a block like any other.
                RenderBlocks(node, onLink, nestedDepth(depth))
            }
        }
        is ThematicBreak -> HorizontalDivider(
            color = MaterialTheme.colorScheme.outline,
            modifier = Modifier.padding(vertical = 6.dp),
        )
        is FencedCodeBlock -> CodeBlock(node.literal)
        is IndentedCodeBlock -> CodeBlock(node.literal)
        is HtmlBlock -> Text(node.literal.trim(), style = mono(), color = DimText)
        is TableBlock -> RenderTable(node, onLink)
        // Inline nodes reached directly (never inside a paragraph) still render.
        is Text, is Code, is SoftLineBreak, is HardLineBreak, is Emphasis, is StrongEmphasis, is Link, is Image ->
            Text(inlineMarkdown(node, onLink), style = MaterialTheme.typography.bodyMedium)
        else -> literalOf(node)?.takeIf { it.isNotBlank() }?.let {
            Text(it, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

/**
 * A bullet / numbered list. Nested lists indent by one step per level; past
 * [MAX_DEPTH] the structure is flattened (a pathological reply must not
 * recurse without end).
 */
@Composable
private fun RenderList(list: Node, onLink: (LinkOpen) -> Unit, depth: Int, marker: () -> String) {
    val nested = nestedDepth(depth)
    var item = list.firstChild
    while (item is ListItem) {
        Row(modifier = Modifier.fillMaxWidth().padding(start = (depth * 16).dp)) {
            Text(
                marker(),
                style = MaterialTheme.typography.bodyMedium,
                color = Accent,
                modifier = Modifier.width(24.dp),
            )
            Column(modifier = Modifier.weight(1f)) {
                var child = item.firstChild
                while (child != null) {
                    // An item's own paragraph is its text line; anything
                    // else (a nested list, a quote, code) is a sub-block.
                    if (child is Paragraph) {
                        Text(inlineMarkdown(child, onLink), style = MaterialTheme.typography.bodyMedium)
                    } else {
                        RenderBlock(child, onLink, nested)
                    }
                    child = child.next
                }
            }
        }
        item = item.next
    }
}

/** Blocks inside blocks stop here: past it a block is shown as its plain text. */
private const val MAX_DEPTH = 12

/** How deep the INSIDE of a block goes: one more than the block, up to the limit. */
internal fun nestedDepth(depth: Int): Int = if (depth < MAX_DEPTH) depth + 1 else depth

/** The nodes shown as they are at any depth: a paragraph and what can sit inside one. */
private fun isTextual(node: Node): Boolean =
    node is Paragraph || node is Text || node is Code || node is SoftLineBreak || node is HardLineBreak ||
        node is Emphasis || node is StrongEmphasis || node is Link || node is Image

/**
 * The plain text of everything inside a block, taken WITHOUT recursing (its own stack):
 * it is used exactly when the structure is too deep to recurse into.
 */
internal fun flatTextOf(root: Node): String {
    val sb = StringBuilder()
    val stack = ArrayDeque<Node>()
    var n = root.firstChild
    while (true) {
        if (n == null) {
            if (stack.isEmpty()) break
            n = stack.removeLast().next
            continue
        }
        when (n) {
            is Text -> sb.append(n.literal)
            is Code -> sb.append(n.literal)
            is SoftLineBreak, is HardLineBreak -> sb.append(' ')
            else -> {
                stack.addLast(n)
                n = n.firstChild
                continue
            }
        }
        n = n.next
    }
    return sb.toString()
}

@Composable
private fun CodeBlock(code: String) {
    val text = code.trimEnd('\n')
    val ctx = LocalContext.current
    fun copy() {
        val clipboard = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("code", text))
        Toast.makeText(ctx, Strings.t.copied, Toast.LENGTH_SHORT).show()
    }
    Surface(
        color = CODE_BG.copy(alpha = 0.55f),
        shape = RoundedCornerShape(8.dp),
        modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
    ) {
        Row(modifier = Modifier.padding(end = 4.dp), verticalAlignment = Alignment.Top) {
            Text(
                text,
                style = mono(),
                modifier = Modifier
                    .weight(1f)
                    .horizontalScroll(rememberScrollState())
                    .padding(8.dp),
            )
            // A labeled button, not a mystery glyph.
            TextButton(onClick = { copy() }) {
                Text(Strings.t.copy, style = MaterialTheme.typography.labelSmall, color = Accent)
            }
        }
    }
}

/** A GFM table: one line per row, cells separated by a visible divider (wide tables scroll sideways). */
@Composable
private fun RenderTable(table: TableBlock, onLink: (LinkOpen) -> Unit) {
    // Rows live inside the table's sections (TableHead / TableBody), not
    // directly under the TableBlock.
    val rows = remember(table) {
        children(table)
            .filter { it is TableHead || it is TableBody }
            .flatMap { section -> children(section).filterIsInstance<TableRow>() }
            .map { row -> children(row).filterIsInstance<TableCell>() }
    }
    if (rows.isEmpty()) return
    Surface(
        color = CODE_BG.copy(alpha = 0.35f),
        shape = RoundedCornerShape(8.dp),
        modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
    ) {
        Column(
            modifier = Modifier
                .horizontalScroll(rememberScrollState())
                .padding(8.dp),
        ) {
            rows.forEachIndexed { index, cells ->
                if (index == 1 && rows.size > 1) {
                    HorizontalDivider(color = MaterialTheme.colorScheme.outline, modifier = Modifier.padding(vertical = 4.dp))
                }
                Text(
                    cells.joinToString("  │  ") { inlineMarkdown(it, onLink).text },
                    style = mono().let { if (index == 0) it.copy(fontWeight = FontWeight.Bold) else it },
                    color = if (index == 0) Accent else Color.Unspecified,
                )
            }
        }
    }
}

// ------------------------------------------------------------------ inline styling

/** Raw text of a leaf node (commonmark keeps `literal` on the leaf classes, not on Node). */
private fun literalOf(node: Node): String? = when (node) {
    is Text -> node.literal
    is Code -> node.literal
    is HtmlInline -> node.literal
    else -> null
}

private fun children(node: Node): List<Node> {
    val out = mutableListOf<Node>()
    var child = node.firstChild
    while (child != null) {
        out.add(child)
        child = child.next
    }
    return out
}

/** The inline content of a node as a styled string: bold, italics, code, links. */
internal fun inlineMarkdown(node: Node, onLink: (LinkOpen) -> Unit): AnnotatedString =
    buildAnnotatedString { appendInline(node, onLink) }

/** Plain text of a node's inline content (table cells, image alt text). */
internal fun plainTextOf(node: Node): String {
    val sb = StringBuilder()
    fun walk(n: Node) {
        var c = n.firstChild
        while (c != null) {
            when (c) {
                is Text -> sb.append(c.literal)
                is Code -> sb.append(c.literal)
                is SoftLineBreak, is HardLineBreak -> sb.append(' ')
                else -> walk(c)
            }
            c = c.next
        }
    }
    walk(node)
    return sb.toString()
}

/**
 * A bare `magnet:?…` in running text becomes a tappable link. The autolink
 * extension only knows web and mail schemes, and a reply's magnet is
 * usually written bare — until now nothing in a reply could be acted on.
 * A magnet ends at whitespace or a closing bracket / quote.
 */
private val MAGNET_IN_TEXT = Regex("""magnet:\?[^\s<>"'\]\)]+""", RegexOption.IGNORE_CASE)

private fun AnnotatedString.Builder.appendWithMagnets(text: String, onLink: (LinkOpen) -> Unit) {
    var from = 0
    for (m in MAGNET_IN_TEXT.findAll(text)) {
        val start = m.range.first
        if (start > from) append(text, from, start)
        val magnet = m.value
        withLink(clickable(magnet, LinkOpen.Magnet(magnet), onLink)) { append(magnet) }
        from = m.range.last + 1
    }
    if (from < text.length) append(text, from, text.length)
}

private fun AnnotatedString.Builder.appendInline(node: Node, onLink: (LinkOpen) -> Unit) {
    var child = node.firstChild
    while (child != null) {
        when (child) {
            is Text -> appendWithMagnets(child.literal, onLink)
            is SoftLineBreak -> append(' ')
            is HardLineBreak -> append('\n')
            is Code -> withStyle(SpanStyle(fontFamily = FontFamily.Monospace, background = CODE_BG)) { append(child.literal) }
            is Emphasis -> withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { appendInline(child, onLink) }
            is StrongEmphasis -> withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { appendInline(child, onLink) }
            is Link -> appendLink(child.destination, child, onLink)
            is Image -> {
                val alt = plainTextOf(child).trim()
                append(if (alt.isBlank()) "[image]" else "[$alt]")
                linkOpenFor(child.destination)?.let { open ->
                    withLink(clickable(child.destination, open, onLink)) { append(" ↗") }
                }
            }
            else -> literalOf(child)?.let { append(it) }
        }
        child = child.next
    }
}

private fun AnnotatedString.Builder.appendLink(destination: String, content: Node, onLink: (LinkOpen) -> Unit) {
    val open = linkOpenFor(destination)
    if (open == null) {
        // Not tappable: show the link's own text plus the target, so nothing is hidden.
        append(plainTextOf(content))
        append(" ($destination)")
        return
    }
    withLink(clickable(destination, open, onLink)) { appendInline(content, onLink) }
}

private fun clickable(destination: String, open: LinkOpen, onLink: (LinkOpen) -> Unit): LinkAnnotation.Clickable =
    LinkAnnotation.Clickable(
        tag = destination,
        styles = linkStyles(),
        linkInteractionListener = LinkInteractionListener { onLink(open) },
    )
