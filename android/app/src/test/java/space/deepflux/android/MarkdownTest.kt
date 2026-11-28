package space.deepflux.android

import android.app.Application
import org.commonmark.node.FencedCodeBlock
import org.commonmark.ext.gfm.tables.TableBlock
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.ui.chat.LinkOpen
import space.deepflux.android.ui.chat.chatMarkdownParser
import space.deepflux.android.ui.chat.inlineMarkdown
import space.deepflux.android.ui.chat.linkOpenFor
import space.deepflux.android.ui.chat.flatTextOf
import space.deepflux.android.ui.chat.nestedDepth
import space.deepflux.android.ui.chat.parseSafely
import space.deepflux.android.ui.chat.parseable
import space.deepflux.android.ui.chat.plainTextOf
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import org.commonmark.ext.gfm.tables.TableRow
import org.commonmark.ext.gfm.tables.TableCell

/** Fix batch 2 (2026-11-24): model replies are markdown — rendered, not shown raw. */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class MarkdownTest {

    private val parser = chatMarkdownParser()

    private fun firstParagraph(markdown: String): org.commonmark.node.Node = parser.parse(markdown).firstChild!!

    private fun inline(markdown: String, opened: MutableList<LinkOpen> = mutableListOf()) =
        inlineMarkdown(firstParagraph(markdown)) { opened.add(it) }

    private fun spans(a: androidx.compose.ui.text.AnnotatedString): List<SpanStyle> =
        a.spanStyles.map { it.item }

    private fun tap(link: LinkAnnotation.Clickable, opened: MutableList<LinkOpen>) {
        link.linkInteractionListener!!.onClick(link)
    }

    @Test
    fun nestingEndsAtTheLimitAndIsTakenApartWithoutRecursing() {
        // (Review 2026-11-28.) A quote in a quote did not count as a level: ">>>>…" is one
        // quote per character — a frame on the app's thread in the PARSER already — and since
        // replies are saved, such a task would have crashed the app at every reopen.
        assertEquals(1, nestedDepth(0))
        assertEquals(12, nestedDepth(11))
        assertTrue("never below the limit once it is reached", nestedDepth(5000) >= nestedDepth(11))

        // The parser never sees an absurd run.
        val longRun = ">".repeat(20_000) + " deep inside"
        assertEquals("> ".repeat(24) + "… deep inside", parseable(longRun))
        assertEquals("a normal quote is left alone", "> quoted", parseable("> quoted"))
        assertEquals("text without quotes is left alone", "a > b", parseable("a > b"))
        val quotes = parseSafely(longRun)
        assertNotNull("20,000 quotes do not overflow the parser", quotes)
        assertTrue(flatTextOf(quotes!!).trim(), flatTextOf(quotes!!).trim().endsWith("deep inside"))

        // Whatever still overflows the parser is answered with null, not with a crash.
        val list = StringBuilder()
        repeat(3000) { i -> list.append("  ".repeat(i)).append("- ").append(10.toChar()) }
        list.append("  ".repeat(3000)).append("- leaf")
        val deep = parseSafely(list.toString())
        if (deep != null) assertEquals("leaf", flatTextOf(deep).trim())
    }

    @Test
    fun markersBecomeStylesNotVisibleCharacters() {
        val a = inline("**bold** then *italic* and `code`")
        assertEquals("bold then italic and code", a.text)
        assertTrue("bold", spans(a).any { it.fontWeight == FontWeight.Bold })
        assertTrue("italic", spans(a).any { it.fontStyle == FontStyle.Italic })
        assertTrue("code is monospaced", spans(a).any { it.fontFamily == androidx.compose.ui.text.font.FontFamily.Monospace })
    }

    @Test
    fun headingsListsAndQuotesLoseTheirMarkup() {
        assertEquals("Title", inlineMarkdown(parser.parse("# Title").firstChild!!) {}.text)
        // A list item's paragraph.
        val list = parser.parse("- one\n- two").firstChild!!
        val item1 = list.firstChild!!
        assertEquals("one", inlineMarkdown(item1.firstChild!!) {}.text)
    }

    @Test
    fun linksAreTappableAndReportedByKind() {
        val opened = mutableListOf<LinkOpen>()
        val a = inline("[site](https://example.com/a?b=1) and [get it](magnet:?xt=urn:btih:0000000000000000000000000000000000000001)", opened)
        assertEquals("site and get it", a.text)
        val links = a.getLinks()
        assertEquals(2, links.size)
        tap(links[0], opened)
        tap(links[1], opened)
        assertEquals(
            listOf(LinkOpen.Web("https://example.com/a?b=1"), LinkOpen.Magnet("magnet:?xt=urn:btih:0000000000000000000000000000000000000001")),
            opened,
        )
    }

    @Test
    fun bareUrlsAutolink() {
        val opened = mutableListOf<LinkOpen>()
        val a = inline("see https://example.com/page now", opened)
        assertEquals("see https://example.com/page now", a.text)
        tap(a.getLinks().single(), opened)
        assertEquals(listOf(LinkOpen.Web("https://example.com/page")), opened)
    }

    @Test
    fun unsafeSchemesStayInertText() {
        val a = inline("[click](javascript:alert(1)) and [x](data:text/html,hi)")
        assertEquals("no tappable link", 0, a.getLinks().size)
        // Nothing is hidden: the target is spelled out as plain text.
        assertEquals("click (javascript:alert(1)) and x (data:text/html,hi)", a.text)
        assertEquals(null, linkOpenFor("javascript:alert(1)"))
        assertEquals(null, linkOpenFor("file:///etc/passwd"))
        assertEquals(null, linkOpenFor("intent://x"))
        assertEquals(null, linkOpenFor("  "))
    }

    @Test
    fun aMagnetOnItsOwnLineIsTappable() {
        val opened = mutableListOf<LinkOpen>()
        val magnet = "magnet:?xt=urn:btih:0000000000000000000000000000000000000002&dn=Ubuntu"
        val a = inline(magnet, opened)
        tap(a.getLinks().single(), opened)
        assertEquals(listOf(LinkOpen.Magnet(magnet)), opened)
    }

    @Test
    fun fencedCodeKeepsItsTextVerbatimInsideABlock() {
        val doc = parser.parse("before\n\n```\nval x = \"<b>**not markup**</b>\"\n```\n\nafter")
        val types = mutableListOf<String>()
        var n = doc.firstChild
        while (n != null) { types.add(n.javaClass.simpleName); n = n.next }
        assertEquals(listOf("Paragraph", "FencedCodeBlock", "Paragraph"), types)
        val code = doc.firstChild!!.next as FencedCodeBlock
        assertEquals("val x = \"<b>**not markup**</b>\"\n", code.literal)
    }

    @Test
    fun tablesParseIntoRowsAndCells() {
        val doc = parser.parse("| Name | Size |\n| --- | --- |\n| A | 1 GB |\n| **B** | 2 GB |")
        val table = doc.firstChild as TableBlock
        val sections = table.childNodes()
        val rows = sections.flatMap { it.childNodes() }.filterIsInstance<TableRow>()
        val cells = rows.map { r -> r.childNodes().filterIsInstance<TableCell>().map { plainTextOf(it) } }
        assertEquals(listOf(listOf("Name", "Size"), listOf("A", "1 GB"), listOf("B", "2 GB")), cells)
    }

    private fun org.commonmark.node.Node.childNodes(): List<org.commonmark.node.Node> {
        val out = mutableListOf<org.commonmark.node.Node>()
        var c = firstChild
        while (c != null) { out.add(c); c = c.next }
        return out
    }

    private fun androidx.compose.ui.text.AnnotatedString.getLinks(): List<LinkAnnotation.Clickable> =
        getLinkAnnotations(0, length).map { it.item }.filterIsInstance<LinkAnnotation.Clickable>()

}
