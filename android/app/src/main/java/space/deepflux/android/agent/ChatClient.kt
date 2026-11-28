package space.deepflux.android.agent

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL

/**
 * Minimal OpenAI-compatible chat client over HttpURLConnection (no extra
 * dependency). Streams server-sent events and calls [onDelta] per content
 * chunk. Mirrors the desktop agent/llm.py contract (content deltas;
 * reasoning_content is captured separately so the UI can dim it).
 *
 * v0.4: supports OpenAI-style TOOL CALLING. A completion either returns
 * plain content or a list of tool calls the caller must execute and feed
 * back via [Message] entries with role "tool".
 *
 * 1.4.12 (owner 2026-11-17): the agent moved to Venice AI's UNCENSORED
 * DeepSeek V4.1 Flash — same OpenAI wire format, function calling and
 * reasoning verified live against api.venice.ai.
 *
 * 1.4.16: WHERE a request goes is the user's choice (Settings → AI &
 * services): the constructor's defaults are the out-of-box Venice endpoint,
 * and [LlmPresets.resolve] supplies provider, model and thinking level.
 *
 * Open for tests: the chat session's loop is exercised against a scripted
 * subclass ([cancel] and [complete] are the whole contract).
 */
open class ChatClient(
    private val baseUrl: String = "https://api.venice.ai/api/v1",
    private val apiKey: String,
    private val model: String = "deepseek-v4-1-flash-uncensored",
    /**
     * The `reasoning_effort` the request carries (1.4.16), or null when the endpoint takes
     * none — exactly what [LlmPresets.resolve] worked out for the user's provider.
     */
    private val reasoningEffort: String? = null,
) {

    /** The in-flight request's connection — [cancel] tears it down. */
    @Volatile
    private var active: HttpURLConnection? = null

    /**
     * Abort the in-flight request: the blocked read fails at once instead
     * of streaming the rest of the reply. Without this, Stop only took
     * effect after the whole round had arrived — and its tool calls ran.
     */
    open fun cancel() {
        runCatching { active?.disconnect() }
    }

    /** One turn of the conversation, JSON-serializable. */
    data class Message(
        val role: String,
        val content: String = "",
        /** For assistant turns that requested tools: raw tool_calls JSON array. */
        val toolCallsJson: String? = null,
        /** For role="tool" results: the id of the call this answers. */
        val toolCallId: String? = null,
    ) {
        fun toJson(): JSONObject {
            val o = JSONObject()
            o.put("role", role)
            if (content.isNotEmpty()) o.put("content", content)
            if (toolCallsJson != null) o.put("tool_calls", JSONArray(toolCallsJson))
            if (toolCallId != null) o.put("tool_call_id", toolCallId)
            return o
        }
    }

    data class ToolCall(val id: String, val name: String, val arguments: String)

    /** A completion result: streamed content, or tool calls to execute. */
    sealed class Result {
        data class Content(val text: String) : Result()
        data class Tools(val calls: List<ToolCall>, val rawJson: String) : Result()
    }

    /**
     * Blocking call — run on a background thread. Throws when [cancel]
     * tore the request down (the caller treats that as "stopped").
     */
    open fun complete(
        history: List<Message>,
        tools: JSONArray? = null,
        onDelta: (String) -> Unit = {},
        onReasoning: (String) -> Unit = {},
    ): Result {
        val conn = URL("$baseUrl/chat/completions").openConnection() as HttpURLConnection
        active = conn
        return try {
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.connectTimeout = 15_000
            conn.readTimeout = 120_000
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setRequestProperty("Authorization", "Bearer $apiKey")
            conn.setRequestProperty("Accept", "text/event-stream")

            val messages = JSONArray()
            for (m in history) messages.put(m.toJson())
            val body = JSONObject()
                .put("model", model)
                .put("messages", messages)
                .put("stream", true)
                .apply { if (tools != null && tools.length() > 0) put("tools", tools) }
                // Sent only where the preset says the endpoint understands it (desktop rule).
                .apply { if (reasoningEffort != null) put("reasoning_effort", reasoningEffort) }
                .toString()
            conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }

            val code = conn.responseCode
            if (code != 200) {
                // What the server said goes on screen and into the log: an endpoint can echo
                // the request's Authorization header, so nothing that can be a key is kept.
                throw IllegalStateException("HTTP $code: " + shownError(conn.errorStream?.bufferedReader()?.use { it.readText() } ?: ""))
            }

            val full = StringBuilder()
            val reason = StringBuilder()
            // tool call fragments arrive as indexed deltas — stitch them.
            val tcIds = mutableMapOf<Int, String>()
            val tcNames = mutableMapOf<Int, String>()
            val tcArgs = mutableMapOf<Int, StringBuilder>()

            BufferedReader(InputStreamReader(conn.inputStream, Charsets.UTF_8)).useLines { lines ->
                for (raw in lines) {
                    val line = raw.trim()
                    if (!line.startsWith("data:")) continue
                    val payload = line.removePrefix("data:").trim()
                    if (payload == "[DONE]") break
                    val obj = runCatching { JSONObject(payload) }.getOrNull() ?: continue
                    val delta = obj.optJSONArray("choices")?.optJSONObject(0)?.optJSONObject("delta") ?: continue
                    delta.optString("content").takeIf { it.isNotEmpty() }?.let {
                        full.append(it)
                        onDelta(it)
                    }
                    // DeepSeek's and Venice's streams call the thinking `reasoning_content`;
                    // OpenRouter's call it `reasoning` (the desktop reads both). Only a STRING
                    // counts: anything else in that slot is not thinking to show.
                    val thought = delta.optString("reasoning_content").ifEmpty { delta.opt("reasoning") as? String ?: "" }
                    thought.takeIf { it.isNotEmpty() }?.let {
                        reason.append(it)
                        onReasoning(it)
                    }
                    val tcs = delta.optJSONArray("tool_calls") ?: continue
                    for (i in 0 until tcs.length()) {
                        val tc = tcs.optJSONObject(i) ?: continue
                        val idx = tc.optInt("index", 0)
                        tc.optJSONObject("function")?.optString("name")?.takeIf { it.isNotEmpty() }?.let {
                            tcNames[idx] = it
                        }
                        tc.optString("id").takeIf { it.isNotEmpty() }?.let { tcIds[idx] = it }
                        tc.optJSONObject("function")?.optString("arguments")?.takeIf { it.isNotEmpty() }?.let {
                            tcArgs.getOrPut(idx) { StringBuilder() }.append(it)
                        }
                    }
                }
            }

            if (tcNames.isNotEmpty()) {
                val arr = JSONArray()
                val calls = mutableListOf<ToolCall>()
                for ((idx, name) in tcNames.toSortedMap()) {
                    val id = tcIds[idx] ?: "call_$idx"
                    val args = tcArgs[idx]?.toString() ?: "{}"
                    calls.add(ToolCall(id, name, args))
                    arr.put(
                        JSONObject()
                            .put("id", id)
                            .put("type", "function")
                            .put("function", JSONObject().put("name", name).put("arguments", args))
                    )
                }
                Result.Tools(calls, arr.toString())
            } else {
                Result.Content(full.toString())
            }
        } catch (e: Exception) {
            Log.w(TAG, "chat failed: ${e.message}")
            throw e
        } finally {
            active = null
            conn.disconnect()
        }
    }

    companion object {
        private const val TAG = "ChatClient"

        /** "Bearer <something long>" anywhere in a server's text. */
        private val BEARER = Regex("(?i)bearer\\s+[A-Za-z0-9._~+/=-]{8,}")
    }

    /**
     * A server's error text as it may be shown and logged: any bearer token is cut out, and
     * so is this client's own key wherever the server repeated it; then it is capped.
     */
    internal fun shownError(body: String): String {
        var text = body.take(4000).replace(BEARER, "Bearer <redacted>")
        if (apiKey.length >= 8) text = text.replace(apiKey, "<redacted>")
        return text.trim().take(300)
    }
}
