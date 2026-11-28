package space.deepflux.android

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
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
import space.deepflux.android.dlmgr.HttpDownloadRepository
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.iptv.IPTVRepository
import space.deepflux.android.rss.RssRepository
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.chat.ChatSession
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * 1.4.16 — saved tasks. Every conversation is a task on the device: created by the first
 * message, saved as it goes, and reopened with the model's WHOLE context. Before, the chat
 * came back after a restart as text on a screen the model had never seen.
 *
 * Driven through the real session with a scripted model (never the network). The files are
 * read back from disk — what a restart would find, not what the session says it saved.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class ChatTasksTest {

    /** Stands in for the model: answers from a script, records what it was asked. */
    private class Scripted : ChatClient(apiKey = "test") {
        val script = ArrayDeque<ChatClient.Result>()
        val requests = mutableListOf<List<ChatClient.Message>>()
        var failWith: Exception? = null

        /** Runs when a request starts (after it was recorded). */
        var onRequest: (() -> Unit)? = null

        /** While set, every request waits here — a reply still streaming. */
        @Volatile
        var gate: CountDownLatch? = null

        override fun complete(
            history: List<ChatClient.Message>,
            tools: JSONArray?,
            onDelta: (String) -> Unit,
            onReasoning: (String) -> Unit,
        ): ChatClient.Result {
            requests.add(history.toList())
            onRequest?.invoke()
            val result = script.removeFirst()      // decided now, like a model that has answered
            gate?.await(10, TimeUnit.SECONDS)      // …while the answer is still on its way
            failWith?.let { throw it }
            return result.also { result ->
                // As a real client does: the answer arrives in pieces, through onDelta.
                if (result is ChatClient.Result.Content) onDelta(result.text)
            }
        }

        override fun cancel() {}
    }

    private lateinit var context: RecordingContext
    private lateinit var torrents: TorrentRepository
    private lateinit var client: Scripted
    private lateinit var session: ChatSession
    private val sessions = mutableListOf<ChatSession>()

    private val store get() = SessionStore(File(context.filesDir, "agent_sessions"))

    @Before
    fun setUp() {
        context = RecordingContext(ApplicationProvider.getApplicationContext())
        torrents = TorrentRepository(context, FakeTorrentEngine()).also { it.start() }
        // The settings store outlives a test's data folder: start from "no chat of an earlier version".
        torrents.saveChatHistory(org.json.JSONArray().toString())
        await("the old-style chat to be empty") { torrents.loadChatHistory() == "[]" }
        // Whatever ChatLoopTest (or an earlier test here) left in the one settings file of the run.
        // A NEW repository's prefs start as the defaults — which ARE these values — so awaiting
        // them alone proves nothing (ChatLoopTest.setAiSettings says it at length). A value that
        // is nobody's default is written and seen first; a write not seen in two seconds is
        // written again (ChatLoopTest.settingsShown says why).
        settingsShown("the settings store answering", { torrents.setLlmEffort("medium") }) { it.llmEffort == "medium" }
        settingsShown("the out-of-box AI settings", {
            torrents.setLlmCustomSendsEffort(false)
            torrents.setLlmProvider("venice")
            torrents.setCustomApiKey("")
            torrents.setLlmModelAndBaseUrl("", "")
            torrents.setLlmEffort("low")
        }) { p -> p.llmProvider == "venice" && p.customApiKey == "" && p.llmBaseUrl == "" && p.llmModel == "" && !p.llmCustomSendsEffort }
        client = Scripted()
        session = make()
    }

    /** Writes settings and waits until the chat's prefs show them, writing again when a write is not seen (ChatLoopTest.settingsShown). */
    private fun settingsShown(what: String, write: () -> Unit, shown: (space.deepflux.android.data.AppPrefs) -> Boolean) {
        val deadline = System.currentTimeMillis() + 10_000
        var written = 0L
        while (!shown(torrents.prefs.value)) {
            val now = System.currentTimeMillis()
            if (now - written >= 2_000) {
                if (written != 0L && now > deadline) org.junit.Assert.fail("timed out waiting for $what — the prefs now: ${torrents.prefs.value}")
                written = now
                write()
            }
            Thread.sleep(25)
        }
    }

    @After
    fun tearDown() {
        torrents.shutdownForTests()
    }

    /** A session over this test's folder — again makes the one a restart would build. */
    private fun make(): ChatSession {
        val http = HttpDownloadRepository(context, File(context.filesDir, "dl"), gateOk = { true })
        return ChatSession(
            torrents, IPTVRepository(context = context, downloadsDir = torrents.downloadsDir), http,
            RssRepository(context, torrents, http, feedUrlPolicy = { null }), context,
            clientFor = { client },
            builtInKey = { "builtin-test-key" },
            onUiThread = { it() },           // what the app posts to its thread, a test runs now
        ).also { sessions += it }
    }

    private fun await(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 10_000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(25)
        }
    }

    /** Sends a message and waits until the model has answered and the turn is over. */
    private fun turn(text: String) {
        val before = client.requests.size
        session.send(text)
        await("the model to be asked") { client.requests.size > before }
        await("the turn to end") { !session.busy.value }
    }

    /** Opens a task and waits until it is on screen (its file is read on the task thread). */
    private fun open(s: ChatSession, id: String) {
        val asked = s.openTask(id)
        assertTrue("the task is there and nothing runs", asked)
        await("the task on screen") { s.openTask.value == id && s.messages.value.isNotEmpty() }
    }

    private fun answer(text: String) {
        client.script.addLast(ChatClient.Result.Content(text))
    }

    /** One round of a harmless tool (it lists the downloads), then a final answer. */
    private fun toolRoundThen(final: String) {
        client.script.addLast(
            ChatClient.Result.Tools(
                listOf(ChatClient.ToolCall("call_0", "list_downloads", "{}")),
                JSONArray().put(
                    JSONObject().put("id", "call_0").put("type", "function")
                        .put("function", JSONObject().put("name", "list_downloads").put("arguments", "{}")),
                ).toString(),
            ),
        )
        answer(final)
    }

    private fun saved(id: String): SessionStore.Task? = store.load(id)

    private fun awaitSaved(id: String, what: String, cond: (SessionStore.Task) -> Boolean) {
        await(what) { saved(id)?.let(cond) == true }
    }

    private fun roles(task: SessionStore.Task) = task.messages.map { it.role }

    // ------------------------------------------------------------------ saved as it goes

    @Test
    fun theFirstMessageOpensATaskAndTheTurnIsSavedWhole() {
        answer("hello there")
        turn("hi")
        assertNotNull("a task exists from the first message on", session.openTask.value)
        val id = session.openTask.value!!

        awaitSaved(id, "the turn on disk") { roles(it) == listOf("system", "user", "assistant") }
        val task = saved(id)!!
        assertEquals("hi", task.messages[1].content)
        assertEquals("hello there", task.messages[2].content)
        assertEquals(listOf("user" to "hi", "agent" to "hello there"), task.transcript.map { it.kind to it.text })
        assertEquals("named after its first message", "hi", task.name)
        assertEquals("the settings value of earlier versions is no longer written", "[]", torrents.loadChatHistory())
    }

    @Test
    fun theQuestionIsOnDiskBeforeTheAnswerArrives() {
        // A process killed mid-turn leaves the question, not nothing.
        client.gate = CountDownLatch(1)
        answer("late")
        session.send("find a ubuntu iso")
        await("the model is asked") { client.requests.size == 1 }
        val id = session.openTask.value!!
        awaitSaved(id, "the question on disk while the turn runs") { it.transcript.map { k -> k.text } == listOf("find a ubuntu iso") }
        client.gate!!.countDown()
        awaitSaved(id, "the answer on disk") { it.transcript.size == 2 }
    }

    @Test
    fun aToolRoundIsKeptAsTheExchangeAndShownAsANote() {
        toolRoundThen("two downloads")
        turn("what is downloading?")
        val id = session.openTask.value!!
        awaitSaved(id, "the whole turn on disk") { roles(it).last() == "assistant" && roles(it).size == 5 }
        val task = saved(id)!!
        assertEquals(listOf("system", "user", "assistant", "tool", "assistant"), roles(task))
        assertNotNull("the assistant's message carries the tool call", task.messages[2].toolCallsJson)
        assertEquals("call_0", task.messages[3].toolCallId)
        assertEquals("the screen's three lines", listOf("user", "event", "agent"), task.transcript.map { it.kind })
        assertEquals("list_downloads", task.transcript[1].text)
    }

    @Test
    fun anErrorIsOfTheMomentAndNotPartOfTheTask() {
        client.failWith = IllegalStateException("HTTP 500: down")
        session.send("hi")
        await("the failure is shown") { session.messages.value.any { it.role == "error" } }
        await("the turn is over") { !session.busy.value }
        val id = session.openTask.value!!
        awaitSaved(id, "what stands after a failure") { it.messages.size == 1 }
        val task = saved(id)!!
        assertEquals("the unanswered question is dropped, as for a retry", listOf("system"), roles(task))
        assertFalse("no line of the task is an error", task.transcript.any { it.text.contains("HTTP 500") })
    }

    // ------------------------------------------------------------------ reopened with its context

    @Test
    fun aTaskReopensWithItsTextAndTheModelsWholeContext() {
        answer("first answer")
        turn("first question")
        val id = session.openTask.value!!
        awaitSaved(id, "saved") { roles(it).size == 3 }

        session.clear()
        await("the screen is empty") { session.messages.value.isEmpty() }
        assertNull(session.openTask.value)

        open(session, id)
        assertEquals(listOf("user", "assistant"), session.messages.value.map { it.role })
        assertEquals(listOf("first question", "first answer"), session.messages.value.map { it.content })
        assertEquals(listOf("system", "user", "assistant"), session.historyForTests().map { it.role })

        // The model is asked with the EARLIER exchange in front of it — the point of it all.
        answer("second answer")
        turn("and now?")
        assertEquals(listOf("system", "user", "assistant", "user"), client.requests.last().map { it.role })
        assertEquals("first question", client.requests.last()[1].content)
        assertEquals("first answer", client.requests.last()[2].content)
        assertEquals("and now?", client.requests.last()[3].content)
        assertEquals("still the same task", id, session.openTask.value)
    }

    @Test
    fun aTaskReopenedAfterATurnWasCutOffAlternatesRoles() {
        // The question was saved, the process died before any answer — a restart finds the
        // task with the question as its last message.
        client.gate = CountDownLatch(1)
        answer("never arrives in this process")
        session.send("a question left hanging")
        await("the model is asked") { client.requests.size == 1 }
        val id = session.openTask.value!!
        awaitSaved(id, "the question on disk") { it.transcript.isNotEmpty() }

        // "Restart": a new session opens the task while the old turn never gets to finish.
        val restarted = make()
        client.gate = null
        await("its list is read") { restarted.taskList.value.any { it.id == id } }
        open(restarted, id)
        assertEquals("the roles alternate: a note closes the unanswered turn", listOf("system", "user", "assistant"), restarted.historyForTests().map { it.role })
        assertEquals("the screen shows the question, and nothing invented after it", listOf("user"), restarted.messages.value.map { it.role })

        answer("an answer at last")
        restarted.send("go on")
        await("the model is asked") { client.requests.size == 2 }
        await("the turn to end") { !restarted.busy.value }
        // The request carries what was said BEFORE this turn: the hanging question, closed by a note.
        assertEquals(listOf("system", "user", "assistant", "user"), client.requests.last().map { it.role })
        assertEquals("go on", client.requests.last().last().content)
    }

    @Test
    fun aToolNoteIsWordedAgainInTheLanguageOfTheDay() {
        toolRoundThen("done")
        turn("check")
        val id = session.openTask.value!!
        awaitSaved(id, "saved") { it.transcript.size == 3 }
        session.clear()
        open(session, id)
        val note = session.messages.value[1]
        assertEquals(Strings.t.toolNoteFmt.format("list_downloads"), note.content)
    }

    // ------------------------------------------------------------------ many tasks

    @Test
    fun aNewTaskKeepsTheOldOneAndTheListIsNewestFirst() {
        answer("one")
        turn("first task")
        val first = session.openTask.value!!
        awaitSaved(first, "saved") { it.transcript.size == 2 }

        session.clear()
        assertNull("nothing is open until something is said", session.openTask.value)
        answer("two")
        turn("second task")
        val second = session.openTask.value!!
        awaitSaved(second, "saved") { it.transcript.size == 2 }

        await("both tasks, the one used last first") { session.taskList.value.map { it.id } == listOf(second, first) }
        assertEquals(listOf("second task", "first task"), session.taskList.value.map { it.name })
        assertTrue(File(context.filesDir, "agent_sessions/$first.json").isFile)
    }

    @Test
    fun nothingReopensAtAStart() {
        answer("kept")
        turn("remember this")
        val id = session.openTask.value!!
        awaitSaved(id, "saved") { it.transcript.size == 2 }

        val restarted = make()
        await("the list is read") { restarted.taskList.value.any { it.id == id } }
        assertTrue("the screen starts blank — a saved task opens when it is tapped", restarted.messages.value.isEmpty())
        assertNull(restarted.openTask.value)
    }

    @Test
    fun aRenamedTaskKeepsItsNameThroughLaterSaves() {
        answer("ok")
        turn("a long first message that would otherwise be the name")
        val id = session.openTask.value!!
        awaitSaved(id, "saved") { it.transcript.size == 2 }

        session.renameTask(id, "  Ubuntu  ")
        await("the new name in the list") { session.taskList.value.any { it.id == id && it.name == "Ubuntu" } }
        answer("more")
        turn("and more")
        awaitSaved(id, "a later save keeps the name") { it.transcript.size == 4 && it.name == "Ubuntu" }
        assertEquals("created once", saved(id)!!.createdAt, saved(id)!!.createdAt)
    }

    @Test
    fun deletingAnotherTaskLeavesTheScreenAlone() {
        answer("one")
        turn("first")
        val first = session.openTask.value!!
        awaitSaved(first, "saved") { it.transcript.size == 2 }
        session.clear()
        answer("two")
        turn("second")
        await("two tasks") { session.taskList.value.size == 2 }

        session.deleteTask(first)
        await("one task left") { session.taskList.value.size == 1 }
        assertNull(saved(first))
        assertEquals(listOf("second", "two"), session.messages.value.map { it.content })
        assertNotNull(session.openTask.value)
    }

    @Test
    fun deletingTheOpenTaskStartsOverAndNoLateSaveBringsItBack() {
        answer("bye")
        turn("to be deleted")
        val id = session.openTask.value!!
        awaitSaved(id, "saved") { it.transcript.size == 2 }

        session.deleteTask(id)
        await("gone from the list") { session.taskList.value.isEmpty() }
        assertTrue("the screen is blank", session.messages.value.isEmpty())
        assertNull(session.openTask.value)
        // A save queued before the delete runs BEFORE it; nothing is queued after.
        Thread.sleep(400)
        assertNull("the file is not written back", saved(id))
        assertFalse(File(context.filesDir, "agent_sessions/$id.json").exists())
    }

    @Test
    fun leavingATaskMakesItTheMostRecentlyUsedOne() {
        // Two tasks; the OLDER one is opened and left again without a word: it is the one
        // used last now (the desktop saves on a switch too).
        answer("one")
        turn("first task")
        val first = session.openTask.value!!
        awaitSaved(first, "saved") { it.transcript.size == 2 }
        session.clear()
        answer("two")
        turn("second task")
        val second = session.openTask.value!!
        awaitSaved(second, "saved") { it.transcript.size == 2 }
        await("newest first") { session.taskList.value.map { it.id } == listOf(second, first) }

        open(session, first)
        session.clear()
        await("the one just left is the most recently used") { session.taskList.value.map { it.id } == listOf(first, second) }
    }

    @Test
    fun aMessageAfterTheOpenTaskWasDeletedIsANewTask() {
        answer("bye")
        turn("to be deleted")
        val deleted = session.openTask.value!!
        awaitSaved(deleted, "saved") { it.transcript.size == 2 }
        session.deleteTask(deleted)
        await("gone") { session.taskList.value.isEmpty() }

        answer("fresh")
        turn("a fresh start")
        val fresh = session.openTask.value!!
        assertNotEquals("a new conversation is a NEW task — the deleted one is not resurrected", deleted, fresh)
        awaitSaved(fresh, "saved") { it.transcript.map { l -> l.text } == listOf("a fresh start", "fresh") }
        assertNull("the deleted task stays deleted", saved(deleted))
    }

    // ------------------------------------------------------------------ while a reply runs

    @Test
    fun aTaskCannotBeOpenedWhileAReplyIsRunning() {
        answer("first")
        turn("earlier")
        val id = session.openTask.value!!
        awaitSaved(id, "saved") { it.transcript.size == 2 }
        session.clear()

        client.gate = CountDownLatch(1)
        answer("streaming")
        session.send("a slow one")
        // "Running" has two parts a moment apart: the session is busy, and the reply's bubble is on screen.
        await("the turn is running and its bubble is up") { session.busy.value && session.messages.value.size == 2 }
        assertFalse("refused, not queued", session.openTask(id))
        assertEquals("the running turn's screen is untouched: the question, and the bubble still streaming",
            listOf("user", "assistant"), session.messages.value.map { it.role })
        assertTrue(session.messages.value.last().streaming)
        client.gate!!.countDown()
        await("the turn is over") { !session.busy.value }
        open(session, id)
    }

    @Test
    fun aTurnThatIsStillRunningWritesNothingIntoTheTaskOpenedAfterIt() {
        // Stop → new task → open another while the old turn is still inside its "request".
        val gate = CountDownLatch(1)
        client.gate = gate
        answer("late reply")
        session.send("old task's question")
        await("the model is asked") { client.requests.size == 1 }
        val old = session.openTask.value!!
        client.gate = null                 // only the old turn waits; a new one must not

        session.clear()                    // stops the turn; the old task is saved as it stands
        await("the old task on disk") { saved(old)?.transcript?.size == 1 }
        answer("other")
        turn("another task")               // a task to open
        val other = session.openTask.value!!
        awaitSaved(other, "saved") { it.transcript.size == 2 }
        session.clear()
        open(session, other)

        gate.countDown()                   // the old turn's answer arrives now, into a stale turn
        Thread.sleep(700)                  // the worker's way out is silent by design: give it time to take it

        assertEquals("the reopened task shows only its own lines", listOf("user", "assistant"), session.messages.value.map { it.role })
        awaitSaved(other, "and on disk nothing of the old turn landed in it") { it.transcript.map { l -> l.text } == listOf("another task", "other") }
        awaitSaved(old, "the old task has its question only") { it.transcript.map { l -> l.text } == listOf("old task's question") }
    }

    @Test
    fun aTaskReopenedAfterAFailedTurnHandsTheModelTheQuestionThatIsOnScreen() {
        // (Review 2026-11-28.) A failure drops the question from the context — so a retry is
        // clean — but it stays on the screen. Reopened, the question showed and the model had
        // never seen it.
        client.failWith = IllegalStateException("HTTP 500: down")
        session.send("a question that failed")
        await("the failure is shown") { session.messages.value.any { it.role == "error" } }
        await("the turn is over") { !session.busy.value }
        val id = session.openTask.value!!
        awaitSaved(id, "what stands after a failure") { it.messages.size == 1 }
        session.clear()

        open(session, id)
        assertEquals("the screen shows the question", listOf("user"), session.messages.value.map { it.role })
        assertEquals("and the model gets it back, closed by a note", listOf("system", "user", "assistant"), session.historyForTests().map { it.role })
        assertEquals("a question that failed", session.historyForTests()[1].content)
    }

    @Test
    fun openingATaskRightAfterItsTurnEndsShowsTheLastAnswer() {
        // The open is queued behind the saves still waiting: it reads what was said, never
        // the state from before them.
        answer("the last answer")
        turn("a question")
        val id = session.openTask.value!!
        session.clear()
        open(session, id)
        assertEquals(listOf("a question", "the last answer"), session.messages.value.map { it.content })
    }

    @Test
    fun whatTheUserDidWhileATaskWasOpeningWins() {
        answer("one")
        turn("earlier")
        val id = session.openTask.value!!
        awaitSaved(id, "saved") { it.transcript.size == 2 }
        session.clear()

        // A session whose "app thread" is held by the test: the task is read, its opening waits.
        val waiting = java.util.concurrent.CopyOnWriteArrayList<() -> Unit>()
        val slowHttp = HttpDownloadRepository(context, File(context.filesDir, "dl"), gateOk = { true })
        val slow = ChatSession(
            torrents, IPTVRepository(context = context, downloadsDir = torrents.downloadsDir), slowHttp,
            RssRepository(context, torrents, slowHttp, feedUrlPolicy = { null }), context,
            clientFor = { client },
            builtInKey = { "builtin-test-key" },
            onUiThread = { waiting.add(it) },
        ).also { sessions += it }
        await("its list") { slow.taskList.value.any { it.id == id } }
        assertTrue(slow.openTask(id))
        await("the task is read, its opening waits for the app's thread") { waiting.size == 1 }

        answer("typed meanwhile")
        val before = client.requests.size
        slow.send("not the old task")
        await("the user's message went out") { client.requests.size > before }
        await("and was answered") { !slow.busy.value }
        waiting.single()()

        assertNotEquals("the opening was dropped: the user had moved on", id, slow.openTask.value)
        assertEquals(listOf("not the old task", "typed meanwhile"), slow.messages.value.map { it.content })
        assertEquals(listOf("system", "user", "assistant"), slow.historyForTests().map { it.role })
    }

    @Test
    fun aRunningTurnAppendsOnlyToItsOwnCopyOfTheContext() {
        // What the app's thread may read (a save, a switch, a new task) is never the list the
        // turn's worker is appending to: iterating it meanwhile was a crash waiting to happen.
        // The question goes into both when it is sent; the turn's tool round only into its own.
        val gate = CountDownLatch(1)
        client.onRequest = { if (client.requests.size == 2) client.gate = gate }
        toolRoundThen("late")
        val before = session.historyForTests().size
        session.send("while this runs")
        await("the tool round is over and the second request is waiting") { client.requests.size == 2 }
        await("the tool reply is written") { client.requests.size == 2 && session.messages.value.any { it.toolNames != null } }
        assertEquals("the session's own context holds the question, nothing of the turn", before + 1, session.historyForTests().size)
        gate.countDown()
        await("the turn is over") { !session.busy.value }
        assertEquals("and the finished turn is adopted", before + 4, session.historyForTests().size)
    }

    @Test
    fun deletingTheOpenTaskWhileItIsReplyingCrashesNothingAndLeavesNothing() {
        val gate = CountDownLatch(1)
        client.gate = gate
        answer("late")
        session.send("a long one")
        await("the model is asked") { client.requests.size == 1 }
        val id = session.openTask.value!!
        session.deleteTask(id)
        await("gone from the list") { session.taskList.value.isEmpty() }

        gate.countDown()
        Thread.sleep(700)                  // the worker's way out is silent by design
        assertTrue("the screen is blank", session.messages.value.isEmpty())
        assertNull(session.openTask.value)
        assertFalse("and no late save brought the file back", File(context.filesDir, "agent_sessions/$id.json").exists())
    }

    // ------------------------------------------------------------------ the chat of earlier versions

    @Test
    fun theChatOfEarlierVersionsBecomesOneTaskOnce() {
        val old = JSONArray()
            .put(JSONObject().put("role", "user").put("content", "find a ubuntu iso"))
            .put(JSONObject().put("role", "assistant").put("content", "here it is").put("reasoning", "thinking"))
            .put(JSONObject().put("role", "error").put("content", "Chat failed: network"))
            .put(JSONObject().put("role", "assistant").put("content", Strings.t.toolNoteFmt.format("web_search")))
        torrents.saveChatHistory(old.toString())
        await("the old-style chat is readable") { torrents.loadChatHistory().startsWith("[") && torrents.loadChatHistory() != "[]" }

        val upgraded = make()
        await("it is a task now") { upgraded.taskList.value.size == 1 }
        val task = store.load(upgraded.taskList.value.single().id)!!
        assertEquals("find a ubuntu iso", task.name)
        assertEquals(listOf("user" to "find a ubuntu iso", "agent" to "here it is"), task.transcript.map { it.kind to it.text })
        assertEquals("with the thinking", "thinking", task.transcript[1].reasoning)
        assertEquals("the model gets the exchange — not the error, not the screen's tool note", listOf("user", "assistant"), roles(task))
        assertTrue("and the screen still starts blank", upgraded.messages.value.isEmpty())
        // Opened, the adopted exchange is in front of the model — the prompt first, the old question kept.
        open(upgraded, upgraded.taskList.value.single().id)
        answer("sure")
        val before = client.requests.size
        upgraded.send("and now?")
        await("the model is asked") { client.requests.size > before }
        await("the turn is over") { !upgraded.busy.value }
        assertEquals(listOf("system", "user", "assistant", "user"), client.requests.last().map { it.role })
        assertEquals("find a ubuntu iso", client.requests.last()[1].content)
        await("the old value is cleared") { torrents.loadChatHistory() == "[]" }

        val again = make()
        await("the list is read") { again.taskList.value.isNotEmpty() }
        assertEquals("adopted once, not at every start", 1, again.taskList.value.size)
    }

    @Test
    fun aChatOfEarlierVersionsThatCannotBeReadIsClearedNotRetriedForever() {
        torrents.saveChatHistory("{not json")
        await("written") { torrents.loadChatHistory() == "{not json" }
        val upgraded = make()
        await("cleared") { torrents.loadChatHistory() == "[]" }
        assertTrue(upgraded.taskList.value.isEmpty())
    }
}
