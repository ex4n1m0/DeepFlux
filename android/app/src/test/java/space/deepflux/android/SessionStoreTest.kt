package space.deepflux.android

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.agent.ChatClient
import space.deepflux.android.agent.SessionStore
import java.io.File

/**
 * Tier 2 (2026-11-23): saved agent tasks — the desktop's task-session format
 * (agent/sessions.py), so a reopened task resumes with full LLM context and
 * a task directory is readable on either side.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class SessionStoreTest {

    private lateinit var dir: File
    private lateinit var store: SessionStore

    @Before
    fun setUp() {
        dir = File(androidx.test.core.app.ApplicationProvider.getApplicationContext<android.content.Context>().filesDir, "agent_sessions")
        store = SessionStore(dir)
    }

    private fun user(text: String) = ChatClient.Message("user", text)
    private fun assistant(text: String, vararg callIds: String) = ChatClient.Message(
        "assistant", text,
        toolCallsJson = if (callIds.isEmpty()) null else JSONArray().apply {
            callIds.forEachIndexed { i, id -> put(JSONObject().put("id", id).put("type", "function").put("function", JSONObject().put("name", "web_search").put("arguments", "{}"))) }
        }.toString(),
    )
    private fun tool(id: String, text: String) = ChatClient.Message("tool", text, toolCallId = id)

    private val lines = listOf(
        SessionStore.Line("user", "find a ubuntu iso"),
        SessionStore.Line("assistant", "here it is", "thinking…"),
    )

    @Test
    fun roundTripKeepsNameHistoryAndTranscript() {
        val id = SessionStore.newId()
        val history = listOf(user("find a ubuntu iso"), assistant("", "call_1"), tool("call_1", "results"), assistant("here it is"))
        assertTrue(store.save(id, "Ubuntu", history, lines))

        val task = store.load(id)!!
        assertEquals("Ubuntu", task.name)
        assertEquals(4, task.messages.size)
        assertEquals("call_1", task.messages[1].let { m -> JSONArray(m.toolCallsJson!!).getJSONObject(0).getString("id") })
        assertEquals("call_1", task.messages[2].toolCallId)
        assertEquals("results", task.messages[2].content)
        assertEquals(lines, task.transcript)
    }

    @Test
    fun reopeningResumesWithTheSystemPromptIntact() {
        val id = SessionStore.newId()
        store.save(id, "", listOf(ChatClient.Message("system", "SYSTEM PROMPT"), user("hello"), assistant("hi")), lines)
        val task = store.load(id)!!
        assertEquals(listOf("system", "user", "assistant"), task.messages.map { it.role })
        assertEquals("SYSTEM PROMPT", task.messages[0].content)
    }

    @Test
    fun aBlankNameFallsBackToTheFirstUserLine() {
        val id = SessionStore.newId()
        store.save(id, "   ", listOf(user("x")), listOf(SessionStore.Line("user", "  a   very   long first message ".repeat(3))))
        val name = store.load(id)!!.name
        assertTrue(name.isNotBlank())
        assertTrue("capped: $name", name.length <= SessionStore.MAX_NAME_LEN)
        assertTrue("whitespace collapsed: $name", !name.contains("  "))
    }

    @Test
    fun resavingKeepsCreatedAtAndBumpsUpdatedAt() {
        val id = SessionStore.newId()
        store.save(id, "T", listOf(user("one")), lines)
        val first = store.load(id)!!
        store.save(id, "T", listOf(user("one"), user("two")), lines)
        val second = store.load(id)!!
        assertEquals(first.createdAt, second.createdAt)
        assertTrue(second.updatedAt >= first.updatedAt)
        assertEquals(2, second.messages.size)
    }

    @Test
    fun danglingToolExchangesNeverSurviveASaveOrALoad() {
        val id = SessionStore.newId()
        // Saved mid-crash: the assistant asked for two calls, one reply landed.
        val history = listOf(user("q"), assistant("", "c1", "c2"), tool("c1", "partial"))
        assertTrue(store.save(id, "Crashed", history, lines))
        assertEquals(listOf("user"), store.load(id)!!.messages.map { it.role })

        // An orphan tool reply, and an unanswered assistant round.
        assertEquals(listOf("user", "assistant"), SessionStore.sanitize(listOf(user("q"), assistant("done"), tool("c9", "orphan"))).map { it.role })
        assertEquals(listOf("user"), SessionStore.sanitize(listOf(user("q"), assistant("", "c1"))).map { it.role })
        // A complete exchange is left alone, wherever it sits.
        val complete = listOf(user("q"), assistant("", "c1"), tool("c1", "ok"), assistant("final"))
        assertEquals(complete.map { it.role }, SessionStore.sanitize(complete).map { it.role })
        // Mid-history damage is not touched (saves happen at turn boundaries).
        val midDamaged = listOf(user("q"), assistant("", "c1", "c2"), tool("c1", "x"), user("next"), assistant("answer"))
        assertEquals(5, SessionStore.sanitize(midDamaged).size)
    }

    @Test
    fun idsOutsideTheUuidHexShapeAreRefused() {
        // Path traversal and junk ids never reach the file system.
        for (bad in listOf("../evil", "abc", "Z" + "0".repeat(31), "", "ABCDEF0123456789ABCDEF0123456789".lowercase() + "x")) {
            assertFalse("'$bad'", store.save(bad, "x", listOf(user("q")), lines))
            assertNull("'$bad'", store.load(bad))
            assertFalse(store.rename(bad, "y"))
            assertFalse(store.delete(bad))
        }
        assertTrue("nothing was written", dir.listFiles().orEmpty().isEmpty())
        assertFalse(File(dir.parentFile, "evil.json").exists())
    }

    @Test
    fun listIsNewestFirstWithANamePreviewAndCount() {
        val older = SessionStore.newId()
        val newer = SessionStore.newId()
        assertTrue(store.save(older, "Old", listOf(user("first question")), listOf(SessionStore.Line("user", "first question"))))
        // Distinct timestamps: the envelope has second precision.
        File(dir, "$older.json").writeText(File(dir, "$older.json").readText().replace("2026-", "2025-"))
        assertTrue(store.save(newer, "New", listOf(user("second question"), assistant("answer")), listOf(SessionStore.Line("user", "second question"))))

        val list = store.list()
        assertEquals(listOf(newer, older), list.map { it.id })
        assertEquals("second question", list[0].preview)
        assertEquals(2, list[0].messageCount)
        assertEquals("New", list[0].name)
    }

    @Test
    fun unreadableFilesAreSkippedNotFatal() {
        dir.mkdirs()
        File(dir, "${SessionStore.newId()}.json").writeText("{not json")
        File(dir, "notes.txt").writeText("ignored")
        val good = SessionStore.newId()
        assertTrue(store.save(good, "Good", listOf(user("q")), lines))
        assertEquals(listOf(good), store.list().map { it.id })
    }

    @Test
    fun renameAndDelete() {
        val id = SessionStore.newId()
        store.save(id, "Before", listOf(user("q")), lines)
        assertTrue(store.rename(id, "  After  "))
        assertEquals("After", store.load(id)!!.name)
        assertFalse("blank names are refused", store.rename(id, "   "))
        assertTrue(store.delete(id))
        assertNull(store.load(id))
        assertFalse("already gone", store.delete(id))
        assertTrue("no temp file left behind", dir.listFiles().orEmpty().none { it.name.endsWith(".tmp") })
    }

    @Test
    fun theEnvelopeMatchesTheDesktopFormat() {
        val id = SessionStore.newId()
        store.save(id, "Compat", listOf(user("q"), assistant("a")), lines)
        val e = JSONObject(File(dir, "$id.json").readText())
        assertEquals(1, e.getInt("schema"))
        assertEquals(id, e.getString("id"))
        assertNotNull(e.getString("created_at"))
        val first = e.getJSONArray("transcript").getJSONObject(0)
        assertEquals("user", first.getString("k"))
        assertEquals("find a ubuntu iso", first.getString("t"))
        val m = e.getJSONArray("messages").getJSONObject(0)
        assertEquals("user", m.getString("role"))
        assertEquals("q", m.getString("content"))
        assertNotEquals("", SessionStore.newId().also { other -> assertNotEquals(id, other) })
    }
}
