package space.deepflux.android.agent

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.time.Instant
import java.util.UUID

/**
 * Persistent agent conversations ("tasks") — the Android twin of the
 * desktop's `agent/sessions.py` (owner decision 2026-11-22: every chat
 * auto-saves as a local task, ZCode-style). Local-only: no network.
 *
 * ONE JSON file per task under the app's private `agent_sessions/`, with the
 * SAME envelope as the desktop (schema 1: id, name, created_at, updated_at,
 * messages, transcript) — so a task directory is readable on either side.
 * Two payloads because they answer different questions:
 *  - `messages`  — the OpenAI-style LLM history, so a reopened task resumes
 *    with FULL model context (it used to be display-only after a restart);
 *  - `transcript` — the rendered lines {k, t, r} so reopening looks the same.
 *
 * Writes are atomic (tmp + replace); [sanitize] strips a dangling trailing
 * tool-call exchange at save AND load, so a task written mid-crash never
 * restores an API-breaking history.
 */
class SessionStore(private val dir: File) {

    /** One rendered line of a task, as replayed into the chat. */
    data class Line(val kind: String, val text: String, val reasoning: String = "")

    /** List-row summary (no payloads). */
    data class Meta(
        val id: String,
        val name: String,
        val createdAt: String,
        val updatedAt: String,
        val messageCount: Int,
        val preview: String,
    )

    data class Task(
        val id: String,
        val name: String,
        val createdAt: String,
        val updatedAt: String,
        val messages: List<ChatClient.Message>,
        val transcript: List<Line>,
    )

    private val lock = Any()

    private fun path(id: String): File? =
        if (ID.matches(id)) File(dir, "$id.json") else null

    /** What this store last read or wrote of one file: its time, its row, and its `created_at`. */
    private class Cached(val mtime: Long, val meta: Meta, val createdAt: String)

    /**
     * The last read of each file, by file name. A save re-reads the task that changed, not
     * every task on the device — a heavy user has hundreds, and the chat saves after every
     * turn; a write updates its own entry, so the list after a save parses nothing. Guarded
     * by [lock]; an entry is used only while the file's time is still the one it was read with.
     */
    private val listed = HashMap<String, Cached>()

    /**
     * Saved tasks, most recently updated first. Unreadable files are skipped, never listed.
     *
     * `updated_at` has whole seconds (the desktop's format): two tasks saved inside one
     * second would come in no order at all, so those are told apart by the file's own
     * time — the envelope is left exactly as both apps write it.
     */
    fun list(): List<Meta> = synchronized(lock) {
        val files = dir.listFiles { f -> f.name.endsWith(".json") && f.isFile } ?: return@synchronized emptyList()
        listed.keys.retainAll(files.mapTo(HashSet()) { it.name })
        files
            .mapNotNull { file -> cachedOf(file)?.let { it.meta to it.mtime } }
            .sortedWith(compareByDescending<Pair<Meta, Long>> { it.first.updatedAt }.thenByDescending { it.second })
            .map { it.first }
    }

    /** The file's entry in [listed], read now if it changed since it was last read. Null for no such task, or one that cannot be read. */
    private fun cachedOf(file: File): Cached? {
        val mtime = file.lastModified()
        listed[file.name]?.takeIf { it.mtime == mtime }?.let { return it }
        return runCatching { readEnvelope(file) }.getOrNull()?.let { e ->
            Cached(mtime, metaOf(e), e.optString("created_at")).also { listed[file.name] = it }
        }
    }

    fun load(id: String): Task? = synchronized(lock) {
        val file = path(id) ?: return null
        runCatching { readEnvelope(file) }.getOrNull()?.let { taskOf(it) }
    }

    /**
     * Upsert one task atomically; `created_at` of an existing task survives
     * re-saves. A blank name falls back to one derived from the first line.
     */
    fun save(id: String, name: String, messages: List<ChatClient.Message>, transcript: List<Line>): Boolean =
        synchronized(lock) {
            val file = path(id) ?: return false
            val now = now()
            // created_at from what is known of the file; it is read again only if it changed since.
            val existingCreatedAt = cachedOf(file)?.createdAt
            val envelope = JSONObject()
                .put("schema", SCHEMA)
                .put("id", id)
                .put(
                    "name",
                    name.trim().ifBlank { autoName(transcript.firstOrNull { it.kind == "user" }?.text ?: "") },
                )
                .put("created_at", existingCreatedAt?.takeIf { it.isNotBlank() } ?: now)
                .put("updated_at", now)
                .put("messages", messagesToJson(sanitize(messages)))
                .put("transcript", transcriptToJson(transcript))
            writeAtomic(file, envelope).also { if (it) remember(file, envelope) }
        }

    fun rename(id: String, name: String): Boolean = synchronized(lock) {
        val file = path(id) ?: return false
        val clean = name.trim()
        if (clean.isEmpty()) return false
        val envelope = runCatching { readEnvelope(file) }.getOrNull() ?: return false
        envelope.put("name", clean.take(MAX_NAME_LEN * 2)) // custom names may run longer
        envelope.put("updated_at", now())
        writeAtomic(file, envelope).also { if (it) remember(file, envelope) }
    }

    fun delete(id: String): Boolean = synchronized(lock) {
        val file = path(id) ?: return false
        listed.remove(file.name)
        file.delete()
    }

    /** After a write of [file]: what it says is known, no reading back. */
    private fun remember(file: File, envelope: JSONObject) {
        listed[file.name] = Cached(file.lastModified(), metaOf(envelope), envelope.optString("created_at"))
    }

    // ------------------------------------------------------------------ format

    private fun readEnvelope(file: File): JSONObject = JSONObject(file.readText())

    private fun metaOf(e: JSONObject): Meta = Meta(
        id = e.optString("id"),
        name = e.optString("name"),
        createdAt = e.optString("created_at"),
        updatedAt = e.optString("updated_at"),
        messageCount = e.optJSONArray("messages")?.length() ?: 0,
        preview = previewOf(e.optJSONArray("transcript")),
    )

    /** The first user line, markup stripped and capped. */
    private fun previewOf(transcript: JSONArray?): String {
        if (transcript == null) return ""
        for (i in 0 until transcript.length()) {
            val line = transcript.optJSONObject(i) ?: continue
            if (line.optString("k") == "user") {
                return line.optString("t").replace(HTML_TAG, "").take(MAX_PREVIEW_LEN)
            }
        }
        return ""
    }

    private fun taskOf(e: JSONObject): Task {
        val messages = e.optJSONArray("messages")?.let { arr ->
            (0 until arr.length()).mapNotNull { i ->
                arr.optJSONObject(i)?.let { o ->
                    ChatClient.Message(
                        role = o.optString("role", "user"),
                        content = o.optString("content"),
                        toolCallsJson = o.optJSONArray("tool_calls")?.toString(),
                        toolCallId = o.optString("tool_call_id").takeIf { it.isNotBlank() },
                    )
                }
            }
        } ?: emptyList()
        val transcript = e.optJSONArray("transcript")?.let { arr ->
            (0 until arr.length()).mapNotNull { i ->
                arr.optJSONObject(i)?.let { o -> Line(o.optString("k", "agent"), o.optString("t"), o.optString("r")) }
            }
        } ?: emptyList()
        return Task(
            id = e.optString("id"), name = e.optString("name"),
            createdAt = e.optString("created_at"), updatedAt = e.optString("updated_at"),
            messages = sanitize(messages), transcript = transcript,
        )
    }

    private fun messagesToJson(messages: List<ChatClient.Message>): JSONArray {
        val arr = JSONArray()
        messages.forEach { arr.put(it.toJson()) }
        return arr
    }

    private fun transcriptToJson(lines: List<Line>): JSONArray {
        val arr = JSONArray()
        lines.forEach { line ->
            val o = JSONObject().put("k", line.kind).put("t", line.text)
            if (line.reasoning.isNotBlank()) o.put("r", line.reasoning)
            arr.put(o)
        }
        return arr
    }

    private fun writeAtomic(file: File, envelope: JSONObject): Boolean = runCatching {
        dir.mkdirs()
        val tmp = File(dir, file.name + ".tmp")
        // Written through a stream that is synced before the move: after a power loss the
        // file is the old one or the new one — never the empty one a rename of unsynced data
        // leaves behind (which would be no task at all: unreadable, and never listed).
        java.io.FileOutputStream(tmp).use { out ->
            out.write(envelope.toString().toByteArray(Charsets.UTF_8))
            out.fd.sync()
        }
        try {
            Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        } catch (e: java.nio.file.AtomicMoveNotSupportedException) {
            Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }
        true
    }.getOrElse {
        File(dir, file.name + ".tmp").delete()
        false
    }

    /** UTC, second precision — the desktop's `isoformat(timespec="seconds")`. */
    private fun now(): String = Instant.now().toString().take(19) + "Z"

    companion object {
        const val SCHEMA = 1
        const val MAX_NAME_LEN = 48
        const val MAX_PREVIEW_LEN = 60

        /** Session ids are uuid4().hex — pinning the shape makes every entry point path-traversal-proof. */
        private val ID = Regex("^[0-9a-f]{32}$")
        private val HTML_TAG = Regex("<[^>]+>")

        fun newId(): String = UUID.randomUUID().toString().replace("-", "")

        /** Task name from the first user message: whitespace collapsed, capped with an ellipsis. */
        fun autoName(text: String): String {
            val flat = text.trim().replace(Regex("\\s+"), " ")
            return when {
                flat.isEmpty() -> ""
                flat.length <= MAX_NAME_LEN -> flat
                else -> flat.take(MAX_NAME_LEN - 1).trimEnd() + "…"
            }
        }

        /** The ids an assistant message's tool_calls ask for. */
        private fun callsOf(message: ChatClient.Message): List<String> {
            val raw = message.toolCallsJson ?: return emptyList()
            return runCatching {
                val arr = JSONArray(raw)
                (0 until arr.length()).mapNotNull { arr.optJSONObject(it)?.optString("id") }
            }.getOrDefault(emptyList())
        }

        /**
         * A copy of an LLM history that is safe to restore (desktop
         * `sanitize_messages`): any trailing incomplete tool-call exchange is
         * stripped from the END — an assistant `tool_calls` without every
         * reply (a PARTIAL reply set too), or an orphan trailing `tool`
         * message. Mid-history damage is left alone: saves only happen at
         * turn boundaries, so incompleteness can only be at the tail.
         */
        fun sanitize(messages: List<ChatClient.Message>): List<ChatClient.Message> {
            val msgs = messages.toMutableList()
            var changed = true
            while (changed && msgs.isNotEmpty()) {
                changed = false
                val last = msgs.last()
                if (last.role == "tool") {
                    val cid = last.toolCallId ?: ""
                    // The owning assistant is the LAST one requesting this id.
                    val ownerIdx = msgs.indexOfLast { it.role == "assistant" && cid in callsOf(it) }
                    if (ownerIdx < 0) {
                        msgs.removeAt(msgs.size - 1) // orphan: nobody ever asked for this reply
                        changed = true
                        continue
                    }
                    val needed = callsOf(msgs[ownerIdx]).toSet()
                    val answered = msgs.drop(ownerIdx + 1).filter { it.role == "tool" }.mapNotNull { it.toolCallId }.toSet()
                    if (!needed.all { it in answered }) {
                        // Partial reply set: drop this reply; the loop re-examines the assistant.
                        msgs.removeAt(msgs.size - 1)
                        changed = true
                        continue
                    }
                }
                if (last.role == "assistant") {
                    val calls = callsOf(last)
                    if (calls.isNotEmpty()) {
                        val answered = msgs.filter { it.role == "tool" }.mapNotNull { it.toolCallId }.toSet()
                        if (calls.any { it !in answered }) {
                            msgs.removeAt(msgs.size - 1)
                            changed = true
                            continue
                        }
                    }
                }
            }
            return msgs
        }
    }
}
