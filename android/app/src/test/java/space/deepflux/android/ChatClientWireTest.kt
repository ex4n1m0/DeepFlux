package space.deepflux.android

import android.app.Application
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.agent.ChatClient

/**
 * 1.4.16 — what the chat client really PUTS ON THE WIRE, read from the request a local server
 * receives: the model, the thinking level (only where the endpoint takes one), the key, and
 * how the streamed answer is read back. Until this, the thinking level was never sent and
 * the body was never looked at.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class ChatClientWireTest {

    private lateinit var server: TinyHttpServer

    @Before
    fun setUp() {
        server = TinyHttpServer()
    }

    @After
    fun tearDown() {
        server.close()
    }

    private fun answer(vararg events: String): ByteArray =
        events.joinToString("") { "data: $it\n\n" }.toByteArray() + "data: [DONE]\n\n".toByteArray()

    private fun routeAnswer(vararg events: String) {
        server.route("/v1/chat/completions") {
            TinyHttpServer.Response(body = answer(*events), headers = mapOf("Content-Type" to "text/event-stream"))
        }
    }

    /** One server-sent event whose message delta is [json]: `delta("""{"content":"Hi"}""")`. */
    private fun delta(json: String): String = """{"choices":[{"delta":$json}]}"""

    @Test
    fun theRequestCarriesTheAddressModelKeyAndThinkingLevel() {
        routeAnswer(delta("""{"content":"Hi"}"""))
        val client = ChatClient("http://127.0.0.1:${server.port}/v1", "sk-test", "model-x", reasoningEffort = "high")

        val result = client.complete(listOf(ChatClient.Message("system", "rules"), ChatClient.Message("user", "hello")))

        assertEquals(ChatClient.Result.Content("Hi"), result)
        val request = server.requests.single()
        assertEquals("POST", request.method)
        assertEquals("/v1/chat/completions", request.path)
        assertEquals("Bearer sk-test", request.headers["authorization"])
        assertEquals("text/event-stream", request.headers["accept"])
        val body = JSONObject(request.bodyText)
        assertEquals("model-x", body.getString("model"))
        assertEquals("high", body.getString("reasoning_effort"))
        assertTrue(body.getBoolean("stream"))
        assertFalse("no tools were offered", body.has("tools"))
        val messages = body.getJSONArray("messages")
        assertEquals(listOf("system", "user"), (0 until messages.length()).map { messages.getJSONObject(it).getString("role") })
        assertEquals("hello", messages.getJSONObject(1).getString("content"))
    }

    @Test
    fun whereTheEndpointTakesNoThinkingLevelTheFieldIsNotThere() {
        routeAnswer(delta("""{"content":"Hi"}"""))
        ChatClient(server.url("/v1"), "sk-test", "model-x").complete(listOf(ChatClient.Message("user", "hello")))

        val body = JSONObject(server.requests.single().bodyText)
        assertFalse(body.has("reasoning_effort"))
    }

    @Test
    fun toolSchemasTravelWithTheRequest() {
        routeAnswer(delta("""{"content":"Hi"}"""))
        val tools = JSONArray().put(JSONObject().put("type", "function").put("function", JSONObject().put("name", "web_search")))
        ChatClient(server.url("/v1"), "sk-test", "model-x").complete(listOf(ChatClient.Message("user", "hello")), tools)

        val body = JSONObject(server.requests.single().bodyText)
        assertEquals("web_search", body.getJSONArray("tools").getJSONObject(0).getJSONObject("function").getString("name"))
    }

    @Test
    fun theThinkingIsReadUnderBothFieldNamesAndKeptApartFromTheAnswer() {
        routeAnswer(
            delta("""{"reasoning_content":"let me "}"""),
            delta("""{"reasoning_content":"think"}"""),
            delta("""{"content":"Hel"}"""),
            delta("""{"reasoning":" (openrouter's name for it)"}"""),
            delta("""{"content":"lo"}"""),
            // Not a string in that slot: not thinking to show.
            delta("""{"reasoning":{"effort":"high"}}"""),
        )
        val said = StringBuilder()
        val thought = StringBuilder()

        val result = ChatClient(server.url("/v1"), "sk-test", "model-x")
            .complete(listOf(ChatClient.Message("user", "hello")), onDelta = { said.append(it) }, onReasoning = { thought.append(it) })

        assertEquals(ChatClient.Result.Content("Hello"), result)
        assertEquals("Hello", said.toString())
        assertEquals("let me think (openrouter's name for it)", thought.toString())
    }

    @Test
    fun aToolCallIsStitchedTogetherFromItsFragments() {
        routeAnswer(
            delta("""{"content":""}"""),
            delta("""{"tool_calls":[{"index":0,"id":"call_7","function":{"name":"add_torrent","arguments":""}}]}"""),
            delta("""{"tool_calls":[{"index":0,"function":{"arguments":"{\"magnet\":"}}]}"""),
            delta("""{"tool_calls":[{"index":0,"function":{"arguments":"\"magnet:?xt=x\"}"}}]}"""),
        )

        val result = ChatClient(server.url("/v1"), "sk-test", "model-x").complete(listOf(ChatClient.Message("user", "go"))) as ChatClient.Result.Tools

        assertEquals(1, result.calls.size)
        assertEquals("call_7", result.calls[0].id)
        assertEquals("add_torrent", result.calls[0].name)
        assertEquals("""{"magnet":"magnet:?xt=x"}""", result.calls[0].arguments)
        // What goes back into the next request as the assistant's own message.
        assertEquals("call_7", JSONArray(result.rawJson).getJSONObject(0).getString("id"))
    }

    @Test
    fun aServersErrorTextNeverCarriesWhatCouldBeAKey() {
        // (Review 2026-11-28.) The error body goes on screen and into the log, and an endpoint
        // can echo the request's Authorization header.
        server.route("/v1/chat/completions") {
            TinyHttpServer.Response(status = 401, body = """{"error":"rejected Authorization: Bearer sk-test-0123456789 and token sk-test-0123456789 too"}""".toByteArray())
        }
        try {
            ChatClient(server.url("/v1"), "sk-test-0123456789", "model-x").complete(listOf(ChatClient.Message("user", "hello")))
            fail("a refused request must throw")
        } catch (e: IllegalStateException) {
            val message = e.message ?: ""
            assertFalse(message, message.contains("sk-test-0123456789"))
            assertTrue(message, message.contains("Bearer <redacted>"))
            assertTrue(message, message.contains("<redacted>"))
            assertTrue("still says what happened", message.startsWith("HTTP 401"))
        }
        val client = ChatClient(server.url("/v1"), "sk-test", "model-x")
        assertTrue(client.shownError("x".repeat(5000) + " Bearer abcdefghijklmnop").length <= 300)
    }

    @Test
    fun aBearerTokenThatIsNotThisClientsOwnIsCutOutAsWell() {
        // A server can repeat a token of somebody else's — an upstream provider's, another
        // user's. It is a credential wherever it came from, and neither the chat nor the log
        // is a place for it. (The test above only ever saw THIS client's key come back, so
        // it could not tell the bearer rule from the own-key rule: a mutation showed that.)
        val client = ChatClient(server.url("/v1"), "sk-test", "model-x")
        val shown = client.shownError(
            """{"error":"upstream refused: authorization: Bearer sk-live-SomebodyElsesKey0123456789 — retry later"}""",
        )
        assertFalse(shown, shown.contains("sk-live-SomebodyElsesKey0123456789"))
        assertTrue(shown, shown.contains("Bearer <redacted>"))
        assertTrue("still says what happened", shown.contains("upstream refused"))
        // Whatever its case, the token goes (the cut is written "Bearer <redacted>", in one
        // case); whatever is too short to be a token is left as it is.
        val upper = client.shownError("authorization: BEARER abcdefghijklmnop")
        assertFalse(upper, upper.contains("abcdefghijklmno"))
        assertTrue(upper, upper.contains("Bearer <redacted>"))
        assertTrue(client.shownError("bearer abc").contains("bearer abc"))
    }

    @Test
    fun anErrorAnswerIsReportedWithItsCodeAndWhatTheServerSaid() {
        server.route("/v1/chat/completions") { TinyHttpServer.Response(status = 401, body = """{"error":"invalid api key"}""".toByteArray()) }
        try {
            ChatClient(server.url("/v1"), "sk-wrong", "model-x").complete(listOf(ChatClient.Message("user", "hello")))
            fail("a refused request must throw")
        } catch (e: IllegalStateException) {
            assertTrue(e.message ?: "", e.message!!.startsWith("HTTP 401"))
            assertTrue(e.message ?: "", e.message!!.contains("invalid api key"))
        }
    }
}
