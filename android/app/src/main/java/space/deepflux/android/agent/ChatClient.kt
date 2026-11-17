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
 */
class ChatClient(
    private val baseUrl: String = "https://api.venice.ai/api/v1",
    private val apiKey: String,
    private val model: String = "deepseek-v4-1-flash-uncensored",
) {

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
     * Blocking call — run on a background thread.
     */
    fun complete(
        history: List<Message>,
        tools: JSONArray? = null,
        onDelta: (String) -> Unit = {},
        onReasoning: (String) -> Unit = {},
    ): Result {
        val conn = URL("$baseUrl/chat/completions").openConnection() as HttpURLConnection
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
                .toString()
            conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }

            val code = conn.responseCode
            if (code != 200) {
                val err = conn.errorStream?.bufferedReader()?.use { it.readText() }?.take(300) ?: ""
                throw IllegalStateException("HTTP $code: $err")
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
                    delta.optString("reasoning_content").takeIf { it.isNotEmpty() }?.let {
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
            conn.disconnect()
        }
    }

    companion object {
        private const val TAG = "ChatClient"
    }
}
