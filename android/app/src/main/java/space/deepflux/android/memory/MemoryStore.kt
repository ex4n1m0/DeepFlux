package space.deepflux.android.memory

import android.content.Context
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * Persistent local memory for the agent — plain Markdown files (Phase C1),
 * the Android twin of the desktop agent/memory.py. Same layout, same entry
 * format, same budgets, so a copy of this directory IS a desktop memory
 * (~/.deeptorrent/memory) and vice versa:
 *
 *  - USER.md             — stable user preferences, imperative directives
 *  - MEMORY.md           — curated long-term facts, decisions, lessons
 *  - daily/YYYY-MM-DD.md — episodic working notes
 *
 * Entries are lines: `- [id:<12hex>] <content>`; the curated files are
 * injected into the system prompt with a character budget, daily notes are
 * reachable only via search. Secret-like content is rejected on write.
 * All writes are atomic (tmp + rename), all access synchronized.
 */
class MemoryStore(rootDir: File) {

    private val root: File = File(rootDir, "memory")
    private val lock = Any()

    data class Entry(val id: String, val scope: String, val file: String, val content: String)

    // ------------------------------------------------------------------ files

    private val userFile: File get() = File(root, "USER.md")
    private val memoryFile: File get() = File(root, "MEMORY.md")
    private val dailyDir: File get() = File(root, "daily")
    private fun dailyFile(day: String = today()): File = File(dailyDir, "$day.md")

    private fun fileForScope(scope: String): File = when (scope) {
        "user" -> userFile
        "note" -> dailyFile()
        else -> memoryFile
    }

    private fun filesForScope(scope: String = ""): List<Pair<String, File>> {
        val out = ArrayList<Pair<String, File>>()
        if (scope == "" || scope == "user") out.add("user" to userFile)
        if (scope == "" || scope == "fact") out.add("fact" to memoryFile)
        if (scope == "" || scope == "note") {
            val days = runCatching {
                dailyDir.listFiles()?.filter { it.name.endsWith(".md") }
                    ?.sortedByDescending { it.name }.orEmpty()
            }.getOrDefault(emptyList())
            days.forEach { out.add("note" to it) }
        }
        return out
    }

    // ------------------------------------------------------------------ prompt

    /** Budgeted curated contents for the system prompt. */
    fun loadCore(): Pair<String, String> {
        val user = cleanPromptText(read(userFile)).trim()
        val memory = cleanPromptText(read(memoryFile)).trim()
        val userCut = if (user.length > USER_BUDGET) {
            user.take(USER_BUDGET).substringBeforeLast('\n') + "\n… (truncated — see USER.md)"
        } else user
        val memCut = if (memory.length > MEMORY_BUDGET) {
            memory.take(MEMORY_BUDGET).substringBeforeLast('\n') + "\n… (truncated — see MEMORY.md)"
        } else memory
        return userCut to memCut
    }

    /** Rendered markdown block for the system prompt (empty when no memory). */
    fun promptSection(): String {
        val (user, memory) = loadCore()
        if (user.isBlank() && memory.isBlank()) return ""
        val parts = ArrayList<String>()
        parts.add("Long-term memory (persistent, stored on the user's device):")
        if (user.isNotBlank()) parts.add("### About the user (USER.md)\n$user")
        if (memory.isNotBlank()) parts.add("### Remembered facts (MEMORY.md)\n$memory")
        return parts.joinToString("\n\n")
    }

    // ------------------------------------------------------------------ operations

    fun save(contentRaw: String, scopeRaw: String): String {
        val content = contentRaw.trim().split(Regex("\\s+")).joinToString(" ")
        if (content.isEmpty()) return "error: empty content"
        if (containsSecret(content)) return "error: Memory cannot store credentials or secrets"
        val scope = if (scopeRaw in SCOPES) scopeRaw else "fact"
        val day = today()
        val value = if (scope == "note") "$day: $content" else content
        val id = UUID.randomUUID().toString().replace("-", "").take(12)
        synchronized(lock) {
            val file = fileForScope(scope)
            val existing = read(file)
            // Dedupe per line (case-insensitive), same as the desktop.
            for (line in existing.lineSequence()) {
                val t = line.trim()
                if (t.startsWith("- ") && entryContent(t).lowercase() == value.lowercase()) {
                    return "ok: already remembered (duplicate of id ${entryId(scope, t)})"
                }
            }
            val header = if (existing.isBlank()) {
                when (scope) {
                    "user" -> "# USER.md — stable preferences & profile\n\n"
                    "fact" -> "# MEMORY.md — durable facts, decisions, lessons\n\n"
                    else -> "# Notes for $day\n\n"
                }
            } else ""
            val body = if (existing.isBlank() || existing.endsWith("\n")) existing else "$existing\n"
            write(file, body + header + "- [id:$id] $value\n")
        }
        return "ok: remembered [id:$id] in ${scope.uppercase()}"
    }

    fun search(query: String, scope: String = "", maxResults: Int = 20): String {
        val q = query.trim().lowercase()
        if (q.isEmpty()) return "error: empty query"
        val results = ArrayList<Entry>()
        synchronized(lock) {
            for ((s, file) in filesForScope(scope)) {
                for (line in read(file).lineSequence()) {
                    val t = line.trim()
                    if (!t.startsWith("- ")) continue
                    val c = entryContent(t)
                    if (c.lowercase().contains(q)) {
                        results.add(Entry(entryId(s, t), s, file.name, c))
                        if (results.size >= maxResults) break
                    }
                }
                if (results.size >= maxResults) break
            }
        }
        if (results.isEmpty()) return "no memories match '$query'"
        return results.joinToString("\n") { "- [${it.id}] (${it.file}) ${it.content}" } +
            if (results.size >= maxResults) "\n(more may exist — narrow the query)" else ""
    }

    fun list(scope: String = "", maxResults: Int = 60): String {
        val entries = ArrayList<Entry>()
        synchronized(lock) {
            for ((s, file) in filesForScope(scope)) {
                for (line in read(file).lineSequence()) {
                    val t = line.trim()
                    if (!t.startsWith("- ")) continue
                    entries.add(Entry(entryId(s, t), s, file.name, entryContent(t)))
                    if (entries.size >= maxResults) break
                }
            }
        }
        if (entries.isEmpty()) return "memory is empty"
        return entries.joinToString("\n") { "- [${it.id}] (${it.file}) ${it.content.take(120)}" }
    }

    fun edit(memoryId: String, contentRaw: String): String {
        val content = contentRaw.trim().split(Regex("\\s+")).joinToString(" ")
        if (content.isEmpty()) return "error: empty content"
        if (containsSecret(content)) return "error: Memory cannot store credentials or secrets"
        synchronized(lock) {
            for ((s, file) in filesForScope()) {
                val lines = read(file).lineSequence().toMutableList()
                for (i in lines.indices) {
                    val t = lines[i].trim()
                    if (!t.startsWith("- ") || entryId(s, t) != memoryId) continue
                    lines[i] = "- [id:$memoryId] $content"
                    write(file, lines.joinToString("\n") + "\n")
                    return "ok: updated [id:$memoryId] in ${file.name}"
                }
            }
        }
        return "error: no memory with id $memoryId"
    }

    fun forget(memoryId: String): String {
        synchronized(lock) {
            for ((_s, file) in filesForScope()) {
                val lines = read(file).lineSequence().toList()
                val idx = lines.indexOfFirst { it.trim().startsWith("- ") && entryId("", it.trim()) == memoryId }
                if (idx >= 0) {
                    write(file, (lines - lines[idx]).joinToString("\n") + "\n")
                    return "ok: forgot [id:$memoryId] from ${file.name}"
                }
            }
        }
        return "error: no memory with id $memoryId"
    }

    // ------------------------------------------------------------------ helpers

    private fun read(file: File): String =
        runCatching { if (file.exists()) file.readText() else "" }.getOrElse {
            Log.w(TAG, "read failed: ${it.message}"); ""
        }

    /** Atomic tmp+rename — a torn memory file would lose every entry. */
    private fun write(file: File, text: String) {
        runCatching {
            file.parentFile?.mkdirs()
            val tmp = File(file.parentFile, file.name + ".tmp")
            tmp.writeText(text)
            if (!tmp.renameTo(file)) {
                file.delete()
                tmp.renameTo(file)
            }
        }.onFailure { Log.w(TAG, "write failed: ${it.message}") }
    }

    private fun entryId(scope: String, line: String): String {
        Regex("^- \\[id:([a-z0-9-]+)\\] ", RegexOption.IGNORE_CASE).find(line)?.let {
            return it.groupValues[1]
        }
        // Legacy lines without an id get a stable content-derived one.
        val digest = java.security.MessageDigest.getInstance("SHA-256")
            .digest("$scope:$line".toByteArray())
            .joinToString("") { "%02x".format(it) }.take(12)
        return "legacy-$digest"
    }

    private fun entryContent(line: String): String =
        line.replaceFirst(Regex("^- \\[id:[a-z0-9-]+\\] ", RegexOption.IGNORE_CASE), "").trim()

    /** Strip ids from the injected copy; keep list shape. */
    private fun cleanPromptText(text: String): String =
        text.lineSequence().joinToString("\n") { line ->
            if (line.trim().startsWith("- ")) "- ${entryContent(line.trim())}" else line
        }

    private fun today(): String = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())

    companion object {
        private const val TAG = "MemoryStore"
        private val SCOPES = setOf("user", "fact", "note")
        private const val USER_BUDGET = 2000
        private const val MEMORY_BUDGET = 3000

        /** Same patterns as the desktop — memory must never hold credentials. */
        private val SECRET_PATTERNS = listOf(
            Regex("(?i)(api[_ -]?key|password|passphrase|secret|access[_ -]?token)\\s*[:=]\\s*\\S+"),
            Regex("(?i)\\b(?:sk|pplx|ghp|github_pat|xox[baprs])[-_][a-z0-9_-]{12,}\\b"),
            Regex("\\beyJ[a-zA-Z0-9_-]{10,}\\.[a-zA-Z0-9_-]{10,}\\.[a-zA-Z0-9_-]{10,}\\b"),
            Regex("-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        )

        fun containsSecret(content: String): Boolean = SECRET_PATTERNS.any { it.containsMatchIn(content) }
    }
}
