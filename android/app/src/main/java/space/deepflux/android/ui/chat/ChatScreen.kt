package space.deepflux.android.ui.chat

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import space.deepflux.android.agent.ChatClient
import space.deepflux.android.data.EmbeddedKey
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.BannerBackground
import space.deepflux.android.ui.theme.Card
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.DimText

/** One visible bubble. */
data class ChatBubble(
    val role: String, // "user" | "assistant" | "error"
    val content: String,
    val reasoning: String = "",
    val streaming: Boolean = false,
)

/**
 * Agent chat surface: a tool-carrying conversation with the model — the
 * Android twin of the desktop Agent tab. Torrent tools since v0.4; IPTV
 * tools (list/play/stop channels) added 2026-11-14 — the owner's test
 * "have the agent run an IPTV channel" is now a first-class flow. History
 * is kept for the session, key = user's own or the built-in shared
 * DeepSeek key (same stance as the desktop app).
 *
 * ALWAYS-YOLO (owner decision 2026-11-16): every tool call executes
 * immediately — there is NO confirmation UI and none may be added (the
 * system prompt carries the desktop's UNATTENDED MODE paragraph so the
 * model never waits for approvals), and the tool loop has NO round cap —
 * the Stop button is the only bound. Torrent search is WEB-ONLY (no
 * Jackett on Android, owner 2026-11-15); tools must never promise seed
 * counts or indexer features.
 */
class ChatSession(
    private val repository: TorrentRepository,
    private val iptv: space.deepflux.android.iptv.IPTVRepository,
    private val httpDownloads: space.deepflux.android.dlmgr.HttpDownloadRepository,
    private val rss: space.deepflux.android.rss.RssRepository,
    private val appContext: android.content.Context,
    /** Opens the in-app Browser on a URL (AppRoot switches the nav tab). */
    private val openInBrowser: (String) -> Unit = {},
    /** Site-grabber v1 (§13 #9): scans the ACTIVE browser tab's page links. */
    private val scanActivePage: (() -> String?)? = null,
) {

    private var stopped = false

    /** Phase C1: desktop-format markdown memory (files under filesDir/memory). */
    private val memory = space.deepflux.android.memory.MemoryStore(appContext.filesDir)

    private val _messages = MutableStateFlow<List<ChatBubble>>(emptyList())
    val messages: StateFlow<List<ChatBubble>> = _messages.asStateFlow()

    private val _busy = MutableStateFlow(false)
    val busy: StateFlow<Boolean> = _busy.asStateFlow()

    private var history: MutableList<ChatClient.Message> = mutableListOf(
        ChatClient.Message("system", systemPrompt())
    )

    /**
     * Rebuilt before every send: the memory section must reflect entries
     * saved earlier in THIS conversation, not those at app start, and the
     * date line must stay current across long sessions.
     *
     * Guidelines rework 2026-11-16 (owner): modeled on the desktop agent's
     * SYSTEM_PROMPT (agent/loop.py) — always-YOLO/unattended paragraph
     * (the desktop YOLO lesson: without it models narrate waiting for
     * approvals that never come), search-path choice + DEEP requests,
     * numbered-choice results, no-Jackett honesty (web-only search, no
     * seed counts), language matching, today's date.
     */
    private fun systemPrompt(): String {
        val base = """
            You are DeepFlux, the AI assistant inside the DeepFlux Android app — a private
            torrent download manager with Live TV (IPTV). Be concise.

            UNATTENDED MODE (always on in this app): every tool executes IMMEDIATELY
            when you call it — this app has NO confirmation dialogs. Never ask the
            user for permission to run a tool and never say you are waiting for
            approval; just act and report what you did. When an action removes
            something (remove_torrent, remove_rss_feed, forget_memory), name exactly
            what you removed as you do it.

            Reply in the language the user writes in — a Chinese message gets a
            Chinese reply, an English one gets English. Keep product names (DeepFlux)
            and technical terms (IPTV, RSS, torrent, magnet) as-is.

            When the user greets you or asks what you can do, reply with a short,
            friendly summary in your own words — find & download torrents, direct
            file downloads, watch Live TV, play downloaded videos, browse the web,
            RSS feeds, remember preferences — then ask what they'd like to do.

            Tools: search_indexers (searches the WEB for torrents via DuckDuckGo +
            known torrent sites — keyless, no setup, and NO seed/leecher data),
            add_torrent, list_downloads (torrents AND file downloads),
            pause/resume/remove_torrent, download_url (direct http(s) file
            downloads), web_search, open_url (opens the in-app browser),
            RSS feeds: add_rss_feed, list_rss_feeds, get_rss_feed_items,
            download_from_feed, remove_rss_feed, page scan: scan_page,
            torrent diagnostics: torrent_info, find_alt_trackers,
            playback: list_iptv_channels, play_iptv_channel, stop_iptv_playback,
            play_torrent, file browsing: list_files, open_file,
            persistent memory: save_memory, search_memory, list_memories,
            edit_memory, forget_memory. Batch independent tool calls into a single
            turn instead of one at a time.

            Downloads: if the user describes what they want, search_indexers first;
            a magnet link they paste goes straight to add_torrent; a direct http(s)
            file link goes to download_url. For status questions, call
            list_downloads. This app has NO torrent indexer service (no Jackett):
            search_indexers always searches the web — never mention or suggest
            Jackett, private trackers or search categories, and never invent seed
            or size numbers (write "—" where a desktop app would show seeds).

            Search workflow: for a vague or time-relative request ("that new sci-fi
            movie…", "the latest episode of…"), call web_search FIRST to pin down
            the exact title/year, THEN search_indexers with the refined terms. For
            a specific title or release, go straight to search_indexers. DEEP
            REQUESTS: when the user's message contains the word "deep" ("search
            deep", "deep dive"), keep sweeping with new query variants (year,
            alternate titles, release group) across search_indexers AND web_search
            until sources are genuinely exhausted — a deep request never ends
            after one quick look.

            Present torrent search results as a NUMBERED list: number, full title,
            size when known, and the site it came from — then add a line like
            "Type the number to download it." When the user replies with just a
            number, treat it as their choice from your last list and download it
            with add_torrent right away.

            Live TV: when the user asks to watch TV or names a kind of channel
            ("play the news", "a music channel"), list_iptv_channels first if you
            are unsure of the exact name, then play_iptv_channel — reference
            channels by their #number or exact name.

            Memory: save durable preferences and facts with save_memory when the
            user states them ("remember that…", "I prefer…") — scope 'user' for
            preferences, 'fact' for facts, 'note' for today's events. Check
            search_memory before asking the user something they may have already
            told you. Never save API keys or passwords.

            Today's date: ${java.time.LocalDate.now()}. Use the current year (and
            month where relevant) in "new/latest/recent" searches instead of
            relying on training data.

            When you give EXAMPLE commands or suggestions, use generic categories
            ('news', 'sports', 'music', 'a documentary') — never named commercial
            channels, services or copyrighted titles.
        """.trimIndent()
        val mem = memory.promptSection()
        return if (mem.isBlank()) base else "$base\n\n$mem"
    }

    init {
        // Restore persisted chat on boot
        Thread {
            val saved = repository.loadChatHistory()
            if (saved.isNotBlank()) {
                runCatching {
                    val arr = org.json.JSONArray(saved)
                    val bubbles = mutableListOf<ChatBubble>()
                    for (i in 0 until arr.length()) {
                        val o = arr.getJSONObject(i)
                        bubbles.add(
                            ChatBubble(
                                role = o.optString("role", "assistant"),
                                content = o.optString("content", ""),
                                reasoning = o.optString("reasoning", ""),
                            )
                        )
                    }
                    if (bubbles.isNotEmpty()) _messages.value = bubbles
                }
            }
        }.apply { isDaemon = true }.start()
    }

    private fun persistMessages() {
        val arr = org.json.JSONArray()
        // Persist last 30 bubbles (tool notes excluded)
        val persistable = _messages.value.filter { it.role != "error" }
            .takeLast(30)
        for (b in persistable) {
            arr.put(
                org.json.JSONObject()
                    .put("role", b.role)
                    .put("content", b.content)
                    .put("reasoning", b.reasoning)
            )
        }
        Thread {
            runCatching { repository.saveChatHistory(arr.toString()) }
        }.apply { isDaemon = true }.start()
    }

    fun stop() {
        stopped = true
    }

    /**
     * Clear the conversation (owner request 2026-11-15): stops any running
     * turn (send() resets the flag), wipes the bubbles AND persists an empty
     * history so a restart doesn't resurrect the old chat. The banner idle
     * state returns.
     */
    fun clear() {
        stop()
        _messages.value = emptyList()
        Thread {
            runCatching { repository.saveChatHistory(org.json.JSONArray().toString()) }
        }.apply { isDaemon = true }.start()
    }

    // ---- Tool plumbing (v0.4): OpenAI-style function schemas ----

    private fun toolSchemas(): org.json.JSONArray {
        fun fn(name: String, desc: String, required: List<String> = emptyList(), props: org.json.JSONObject = org.json.JSONObject()): org.json.JSONObject {
            val params = org.json.JSONObject().put("type", "object").put("properties", props)
            if (required.isNotEmpty()) params.put("required", org.json.JSONArray(required))
            return org.json.JSONObject().put("type", "function")
                .put("function", org.json.JSONObject().put("name", name).put("description", desc).put("parameters", params))
        }
        val magnetProp = org.json.JSONObject().put("type", "string").put("description", "The magnet:?xt=urn:btih:... URI")
        val queryProp = org.json.JSONObject().put("type", "string").put("description", "What to search for")
        val limitProp = org.json.JSONObject().put("type", "integer").put("description", "Max results (default 5)")
        return org.json.JSONArray()
            .put(fn("add_torrent", "Add a torrent to the download list from a magnet URI.",
                listOf("magnet"), org.json.JSONObject().put("magnet", magnetProp)))
            .put(fn("search_indexers", "Search the WEB for torrents (DuckDuckGo + known torrent sites; no setup needed). Returns results with magnet URIs.",
                listOf("query"), org.json.JSONObject().put("query", queryProp).put("limit", limitProp)))
            .put(fn("web_search", "Search the web via DuckDuckGo (keyless). Returns titles, URLs and snippets.",
                listOf("query"), org.json.JSONObject().put("query", org.json.JSONObject().put("type", "string")
                    .put("description", "What to search the web for"))))
            .put(fn("list_downloads", "List all torrents AND plain-HTTP file downloads with their state, progress and speed."))
            .put(fn("download_url", "Download a file over http(s) — video, archive, anything. Lands in the Files section of Downloads.",
                listOf("url"),
                org.json.JSONObject()
                    .put("url", org.json.JSONObject().put("type", "string")
                        .put("description", "The full http(s) URL of the file"))
                    .put("file_name", org.json.JSONObject().put("type", "string")
                        .put("description", "Optional override for the saved file name"))))
            .put(fn("pause_torrent", "Pause a torrent by name or info-hash.",
                listOf("name"), org.json.JSONObject().put("name", org.json.JSONObject().put("type", "string")
                    .put("description", "Torrent name (partial match) or info-hash"))))
            .put(fn("resume_torrent", "Resume a paused torrent by name or info-hash.",
                listOf("name"), org.json.JSONObject().put("name", org.json.JSONObject().put("type", "string")
                    .put("description", "Torrent name (partial match) or info-hash"))))
            .put(fn("remove_torrent", "Remove a torrent (keeps downloaded files).",
                listOf("name"), org.json.JSONObject().put("name", org.json.JSONObject().put("type", "string")
                    .put("description", "Torrent name (partial match) or info-hash"))))
            .put(fn("list_iptv_channels", "List the Live TV (IPTV) channels from the user's playlist, with their #numbers.",
                emptyList(), org.json.JSONObject()
                    .put("query", org.json.JSONObject().put("type", "string")
                        .put("description", "Optional case-insensitive filter on channel name or group"))
                    .put("limit", limitProp)))
            .put(fn("play_iptv_channel", "Open an IPTV channel in the video player and start playing it.",
                listOf("target"), org.json.JSONObject().put("target", org.json.JSONObject().put("type", "string")
                    .put("description", "Channel number like '#3' or '#12', or the (partial) channel name, e.g. 'news'"))))
            .put(fn("stop_iptv_playback", "Close the video player and stop IPTV playback."))
            .put(fn("play_torrent", "Play a video inside a downloaded (or downloading) torrent in the player — streams while it downloads.",
                listOf("name"), org.json.JSONObject().put("name", org.json.JSONObject().put("type", "string")
                    .put("description", "Torrent name (partial match) or info-hash"))))
            .put(fn("list_files", "List the files in DeepFlux's download folder (the Commander's app-storage root).",
                emptyList(), org.json.JSONObject().put("path", org.json.JSONObject().put("type", "string")
                    .put("description", "Optional subfolder name"))))
            .put(fn("open_file", "Play a downloaded media file in the player by (partial) name.",
                listOf("name"), org.json.JSONObject().put("name", org.json.JSONObject().put("type", "string")
                    .put("description", "File name (partial match), e.g. 'bunny'"))))
            .put(fn("save_memory", "Save a durable memory entry (markdown files on the user's device). Use scope 'user' for preferences, 'fact' for facts/decisions, 'note' for today's events.",
                listOf("content"), org.json.JSONObject()
                    .put("content", org.json.JSONObject().put("type", "string")
                        .put("description", "The memory, one line, imperative for preferences ('Prefer 720p releases')"))
                    .put("scope", org.json.JSONObject().put("type", "string")
                        .put("description", "user | fact | note (default fact)"))))
            .put(fn("search_memory", "Search all memory entries (user, facts, daily notes).",
                listOf("query"), org.json.JSONObject().put("query", org.json.JSONObject().put("type", "string")
                    .put("description", "Case-insensitive text to find"))))
            .put(fn("list_memories", "List memory entries, newest files first."))
            .put(fn("edit_memory", "Rewrite a memory entry by its id.",
                listOf("id", "content"), org.json.JSONObject()
                    .put("id", org.json.JSONObject().put("type", "string")
                        .put("description", "The id shown by list_memories/search_memory"))
                    .put("content", org.json.JSONObject().put("type", "string")
                        .put("description", "The replacement text"))))
            .put(fn("forget_memory", "Delete a memory entry by its id.",
                listOf("id"), org.json.JSONObject().put("id", org.json.JSONObject().put("type", "string")
                    .put("description", "The id shown by list_memories/search_memory"))))
            .put(fn("open_url", "Open a URL in the in-app browser (switches to the Browser tab).",
                listOf("url"), org.json.JSONObject().put("url", org.json.JSONObject().put("type", "string")
                    .put("description", "Full http(s) URL"))))
            .put(fn("add_rss_feed", "Add an RSS feed (e.g. a torrent site's feed) to watch for new releases. Existing items become the baseline — auto-download applies only to items published AFTER adding.",
                listOf("url"),
                org.json.JSONObject()
                    .put("url", org.json.JSONObject().put("type", "string").put("description", "Feed URL"))
                    .put("keywords", org.json.JSONObject().put("type", "string")
                        .put("description", "Comma-separated title keywords; a matching NEW item is auto-downloaded"))
                    .put("auto_download", org.json.JSONObject().put("type", "boolean")
                        .put("description", "Enable auto-download of keyword matches (default true when keywords given)"))))
            .put(fn("list_rss_feeds", "List the saved RSS feeds with their auto-download settings.", listOf(),
                org.json.JSONObject()))
            .put(fn("get_rss_feed_items", "Fetch and list the current items of a saved RSS feed.",
                listOf("url"),
                org.json.JSONObject()
                    .put("url", org.json.JSONObject().put("type", "string").put("description", "Feed URL as saved"))
                    .put("limit", org.json.JSONObject().put("type", "integer").put("description", "Max items (default 10)"))))
            .put(fn("download_from_feed", "Download one item from a saved RSS feed (by exact or start-of title).",
                listOf("url", "title"),
                org.json.JSONObject()
                    .put("url", org.json.JSONObject().put("type", "string").put("description", "Feed URL as saved"))
                    .put("title", org.json.JSONObject().put("type", "string")
                        .put("description", "The item's title (exact match or from its start)"))))
            .put(fn("remove_rss_feed", "Remove a saved RSS feed by its URL.",
                listOf("url"),
                org.json.JSONObject().put("url", org.json.JSONObject().put("type", "string")
                    .put("description", "Feed URL as saved"))))
            .put(fn("scan_page", "Scan the page currently open in the in-app Browser tab for downloadable links: magnets, .torrent files, direct video links, and page links. Use after the user opens a site, to find downloads on it.",
                listOf(),
                org.json.JSONObject()))
            .put(fn("torrent_info", "Diagnostics for one torrent in the list: state, progress, speed, swarm, size and its trackers.",
                listOf("name"),
                org.json.JSONObject().put("name", org.json.JSONObject().put("type", "string")
                    .put("description", "Torrent name (substring) or info-hash"))))
            .put(fn("find_alt_trackers", "For a torrent that downloads slowly or has no seeds: show its current trackers and search the indexers for the same title on other trackers, excluding ones already in the list.",
                listOf("name"),
                org.json.JSONObject().put("name", org.json.JSONObject().put("type", "string")
                    .put("description", "Torrent name (substring) or info-hash"))))
    }

    /**
     * search_indexers, v1.3 — WEB-ONLY (owner decision 2026-11-15: no Jackett
     * on Android): DuckDuckGo + known torrent sites, keyless, zero setup.
     */
    private fun torrentSearchTool(query: String, limit: Int): String {
        val hits = space.deepflux.android.agent.WebTorrentSearch.search(query, limit * 2)
        if (hits.isEmpty()) {
            return "no torrent results found for: $query\n" +
                "(searched the web via DuckDuckGo). Try a different phrasing or the exact release name."
        }
        val sb = StringBuilder()
        val seen = mutableSetOf<String>()
        var count = 0
        for (h in hits) {
            if (count >= limit * 2) break
            val hash = space.deepflux.android.engine.MagnetParser.infoHashHex(h.magnetUri)?.lowercase()
            if (hash != null && !seen.add(hash)) continue
            val source = if (h.sourceUrl.isNotBlank()) hostLabel(h.sourceUrl) else "web"
            sb.append("- ").append(h.title.ifBlank { "(untitled)" }).append(" [web · ").append(source).append("]\n  magnet: ")
                .append(h.magnetUri).append('\n')
            count++
        }
        if (count == 0) return "no torrent results found for: $query"
        sb.append("(found via DuckDuckGo + torrent sites; no seeds data — pick by title/site reputation)")
        return sb.toString()
    }

    private fun hostLabel(url: String): String =
        runCatching { java.net.URL(url).host }.getOrDefault("web")

    private fun executeTool(call: ChatClient.ToolCall): String {        val args = runCatching { org.json.JSONObject(call.arguments) }.getOrNull() ?: org.json.JSONObject()
        return when (call.name) {
            "add_torrent" -> {
                val magnet = args.optString("magnet", "")
                if (magnet.isBlank()) return "error: magnet is empty"
                repository.addMagnet(magnet)
                "ok: torrent added to the download list"
            }
            "list_downloads" -> {
                val list = repository.torrents.value
                val files = httpDownloads.items.value
                if (list.isEmpty() && files.isEmpty()) return "no downloads in the list"
                val torrentLines = list.joinToString("\n") { t ->
                    "- [torrent] ${t.name} [${t.state}] ${(t.progress * 100).toInt()}% ${t.downloadRate / 1024}KB/s down"
                }
                val fileLines = files.joinToString("\n") { f ->
                    "- [file] ${f.fileName} [${f.state}] " +
                        (if (f.totalBytes > 0) "${(f.progress * 100).toInt()}%" else "${f.doneBytes / 1024}KB") +
                        (if (f.error.isNotBlank()) " ${f.error}" else "")
                }
                listOf(torrentLines, fileLines).filter { it.isNotBlank() }.joinToString("\n")
            }
            "download_url" -> {
                val url = args.optString("url", "").trim()
                if (url.isBlank()) return "error: url is empty"
                val name = args.optString("file_name", "").trim().ifBlank { null }
                val item = httpDownloads.add(url, fileName = name)
                    ?: return "error: '$url' is not an http(s) URL"
                "ok: added '${item.fileName}' to the Files section of Downloads"
            }
            "search_indexers" -> {
                val query = args.optString("query", "")
                val limit = args.optInt("limit", 5)
                if (query.isBlank()) return "error: query is empty"
                torrentSearchTool(query, limit)
            }
            "web_search" -> {
                val query = args.optString("query", "")
                if (query.isBlank()) return "error: query is empty"
                val results = space.deepflux.android.agent.WebSearchClient.search(query, 5)
                if (results.isEmpty()) return "no web results for: " + query
                results.joinToString("\n") { r ->
                    "- " + r.title + "\n  " + r.url + "\n  " + r.snippet.take(150)
                }
            }
            "add_rss_feed" -> {
                val url = args.optString("url", "").trim()
                if (url.isBlank()) return "error: url is empty"
                val keywords = args.optString("keywords", "").split(',').map { it.trim() }.filter { it.isNotBlank() }
                val auto = if (args.has("auto_download")) args.optBoolean("auto_download", false)
                else keywords.isNotEmpty()
                val res = rss.addFeed(url, keywords, auto)
                if (res.isFailure) return "error: ${res.exceptionOrNull()?.message ?: "could not add feed"}"
                val f = res.getOrThrow()
                "ok: watching feed '${f.title}' (${f.url})" +
                    (if (auto && keywords.isNotEmpty()) " — auto-downloading NEW items matching: ${keywords.joinToString(", ")}" else "")
            }
            "list_rss_feeds" -> {
                val feeds = rss.feeds.value
                if (feeds.isEmpty()) return "no RSS feeds saved yet — add one with add_rss_feed"
                feeds.joinToString("\n") { f ->
                    "- ${f.title}\n  ${f.url}" +
                        (if (f.autoDownload && f.keywords.isNotEmpty()) "\n  auto-download: ${f.keywords.joinToString(", ")}" else "") +
                        (if (f.lastError.isNotBlank()) "\n  last error: ${f.lastError}" else "")
                }
            }
            "get_rss_feed_items" -> {
                val url = args.optString("url", "").trim()
                val feed = rss.feeds.value.find { it.url == url || it.id == url }
                    ?: return "error: no saved feed matches: $url"
                val limit = args.optInt("limit", 10).coerceIn(1, 50)
                rss.refresh(feed.id).fold(
                    onSuccess = { },
                    onFailure = { return "error: feed refresh failed: ${it.message}" },
                )
                val items = rss.itemsByFeed.value[feed.id].orEmpty()
                if (items.isEmpty()) return "feed fetched but has no items"
                items.take(limit).joinToString("\n") { i ->
                    "- ${i.title}\n  ${(i.magnet ?: i.link).take(160)}"
                }
            }
            "download_from_feed" -> {
                val url = args.optString("url", "").trim()
                val title = args.optString("title", "").trim()
                if (title.isBlank()) return "error: title is empty"
                val feed = rss.feeds.value.find { it.url == url || it.id == url }
                    ?: return "error: no saved feed matches: $url"
                val items = rss.itemsByFeed.value[feed.id].orEmpty()
                if (items.isEmpty()) return "error: no cached items — call get_rss_feed_items first"
                val wanted = items.find { it.title.equals(title, true) }
                    ?: items.find { it.title.startsWith(title, true) }
                    ?: items.find { it.title.contains(title, true) }
                    ?: return "error: no item matches title: $title"
                if (rss.downloadItem(feed.id, wanted.guid)) "ok: downloading ${wanted.title}"
                else "error: could not hand '${wanted.title}' to an engine (unsupported link type or invalid magnet)"
            }
            "remove_rss_feed" -> {
                val url = args.optString("url", "").trim()
                val feed = rss.feeds.value.find { it.url == url || it.id == url }
                    ?: return "error: no saved feed matches: $url"
                rss.removeFeed(feed.id)
                "ok: removed feed ${feed.title}"
            }
            "scan_page" -> {
                val scanner = scanActivePage ?: return "error: page scanning unavailable"
                scanner() ?: "error: no page is open in the Browser tab — ask the user to open one first"
            }
            "torrent_info" -> {
                val q = args.optString("name", "").lowercase()
                if (q.isBlank()) return "error: name is empty"
                val t = repository.torrents.value.find { it.name.lowercase().contains(q) || it.infoHash.startsWith(q) }
                    ?: return "no torrent found matching: $q"
                val trackers = repository.torrentTrackers(t.infoHash)
                buildString {
                    append("name: ").append(t.name).append('\n')
                    append("state: ").append(t.state).append(" · ").append((t.progress * 100).toInt()).append("%\n")
                    append("swarm: ").append(t.seeds).append(" seeds · ").append(t.peers).append(" peers\n")
                    append("speed: ↓").append(t.downloadRate / 1024).append(" KB/s · ↑").append(t.uploadRate / 1024).append(" KB/s\n")
                    append("size: ").append(t.doneBytes / 1048576).append(" / ").append(t.totalBytes / 1048576).append(" MB\n")
                    append("sequential: ").append(t.sequential).append('\n')
                    if (t.errorMessage.isNotBlank()) append("error: ").append(t.errorMessage.take(120)).append('\n')
                    append("trackers (").append(trackers.size).append("):")
                    append(if (trackers.isEmpty()) " none — a trackerless magnet relies on DHT" else "")
                    append('\n')
                    trackers.take(10).forEach { append("  - ").append(it.take(100)).append('\n') }
                }.trim()
            }
            "find_alt_trackers" -> {
                val q = args.optString("name", "").lowercase()
                if (q.isBlank()) return "error: name is empty"
                val t = repository.torrents.value.find { it.name.lowercase().contains(q) || it.infoHash.startsWith(q) }
                    ?: return "no torrent found matching: " + q
                val currentTrackers = repository.torrentTrackers(t.infoHash)
                val existingHashes = repository.torrents.value.mapNotNull { it.infoHash }.toSet()
                val webAlts = space.deepflux.android.agent.WebTorrentSearch.search(t.name, 8)

                buildString {
                    append("current trackers (").append(currentTrackers.size).append("):\n")
                    currentTrackers.take(10).forEach { append("  - ").append(it.take(100)).append('\n') }
                    if (currentTrackers.isEmpty()) append("  (none — DHT only)\n")
                    append("alternatives from a web search (").append(webAlts.size).append("):\n")
                    val seen = mutableSetOf<String>()
                    webAlts.forEach { r ->
                        val hash = space.deepflux.android.engine.MagnetParser.infoHashHex(r.magnetUri)?.lowercase() ?: ""
                        if (hash.isNotEmpty() && hash !in existingHashes && seen.add(hash)) {
                            // hostLabel (URL-parsed) — a naive substringBefore('/')
                            // stops at the scheme slash and printed "https:".
                            append("- ").append(r.title.take(80)).append(" [web · ")
                                .append(hostLabel(r.sourceUrl).take(30))
                                .append("]\n  ").append(r.magnetUri).append('\n')
                        }
                    }
                }.trim()
            }
"pause_torrent" -> {
                val name = args.optString("name", "").lowercase()
                if (name.isBlank()) return "error: name is empty"
                val t = repository.torrents.value.find { it.name.lowercase().contains(name) || it.infoHash.startsWith(name) }
                    ?: return "no torrent found matching: " + name
                repository.pause(t.infoHash)
                "ok: paused " + t.name
            }
            "resume_torrent" -> {
                val name = args.optString("name", "").lowercase()
                if (name.isBlank()) return "error: name is empty"
                val t = repository.torrents.value.find { it.name.lowercase().contains(name) || it.infoHash.startsWith(name) }
                    ?: return "no torrent found matching: " + name
                repository.resume(t.infoHash)
                "ok: resumed " + t.name
            }
            "remove_torrent" -> {
                val name = args.optString("name", "").lowercase()
                if (name.isBlank()) return "error: name is empty"
                val t = repository.torrents.value.find { it.name.lowercase().contains(name) || it.infoHash.startsWith(name) }
                    ?: return "no torrent found matching: " + name
                repository.remove(t.infoHash)
                "ok: removed " + t.name
            }
            "list_iptv_channels" -> {
                val channels = iptv.state.value.channels
                if (channels.isEmpty()) {
                    return "no IPTV channels loaded. Tell the user to add a playlist URL in the Live TV tab first."
                }
                val query = args.optString("query", "").trim().lowercase()
                val limit = args.optInt("limit", 40).coerceIn(1, 100)
                val filtered = if (query.isEmpty()) channels
                else channels.filter { it.name.lowercase().contains(query) || it.groupId.lowercase().contains(query) }
                if (filtered.isEmpty()) return "no channels match: $query (playlist has ${channels.size} channels)"
                filtered.take(limit).joinToString("\n") { c ->
                    val idx = channels.indexOf(c) + 1
                    "- #$idx ${c.name}" + (if (c.groupId.isNotBlank()) " [${c.groupId}]" else "")
                } + if (filtered.size > limit) "\n(${filtered.size - limit} more — pass a narrower query)" else ""
            }
            "play_iptv_channel" -> {
                val channels = iptv.state.value.channels
                if (channels.isEmpty()) {
                    return "error: no IPTV channels loaded. The user must add a playlist in the Live TV tab first."
                }
                val target = args.optString("target", "").trim()
                if (target.isBlank()) return "error: target is empty"
                // "#3" (1-based index into the full list) wins, then substring match.
                val ch = Regex("#(\\d+)").find(target)?.groupValues?.get(1)?.toIntOrNull()
                    ?.let { channels.getOrNull(it - 1) }
                    ?: channels.find { it.name.lowercase().contains(target.lowercase()) }
                    ?: return "error: no channel matches '$target'. Call list_iptv_channels to see the #numbers."
                val intent = android.content.Intent(appContext, space.deepflux.android.player.PlayerActivity::class.java)
                    .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_URL, ch.url)
                    .putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_TITLE, ch.name)
                    .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
                if (ch.httpUserAgent.isNotBlank()) {
                    intent.putExtra(space.deepflux.android.player.PlayerActivity.EXTRA_USER_AGENT, ch.httpUserAgent)
                }
                // Starting from the app context needs NEW_TASK; the player lands
                // in the app's task (same affinity), on top of the chat screen.
                appContext.startActivity(intent)
                "ok: now playing '${ch.name}' in the player. Tell the user the ✕ button (top-left) closes it, or ask me to stop_iptv_playback."
            }
            "stop_iptv_playback" -> {
                if (space.deepflux.android.player.PlayerActivity.finishActive()) "ok: player closed"
                else "nothing is playing"
            }
            "open_url" -> {
                val url = args.optString("url", "").trim()
                if (!url.startsWith("http://", true) && !url.startsWith("https://", true)) {
                    return "error: open_url needs a full http(s) URL"
                }
                openInBrowser(url)
                "ok: opened $url in the browser"
            }
            "save_memory" -> memory.save(args.optString("content", ""), args.optString("scope", "fact"))
            "search_memory" -> memory.search(args.optString("query", ""))
            "list_memories" -> memory.list()
            "edit_memory" -> memory.edit(args.optString("id", ""), args.optString("content", ""))
            "forget_memory" -> memory.forget(args.optString("id", ""))


            "list_files" -> {
                // Path-traversal guard: canonical path must stay under the root.
                val rel = args.optString("path", "").trim().trimStart('/', '\\')
                val root = repository.downloadsDir.canonicalFile
                val dir = java.io.File(root, rel).canonicalFile
                if (!dir.path.startsWith(root.path)) return "error: path escapes the download folder"
                if (!dir.isDirectory) return "error: no such folder: $rel"
                val list = dir.listFiles()
                    ?.sortedWith(compareByDescending<java.io.File> { it.isDirectory }.thenBy { it.name.lowercase() })
                    ?: return "error: cannot read the folder"
                if (list.isEmpty()) return "the folder is empty"
                list.take(50).joinToString("\n") { f ->
                    if (f.isDirectory) "- [folder] ${f.name}"
                    else "- ${f.name} (${f.length() / 1024} KB)"
                }
            }
            "open_file" -> {
                val name = args.optString("name", "").lowercase()
                if (name.isBlank()) return "error: name is empty"
                val root = repository.downloadsDir
                val match = root.walkTopDown().filter { it.isFile }
                    .firstOrNull { it.name.lowercase().contains(name) }
                    ?: return "no downloaded file matches: $name"
                if (!space.deepflux.android.util.Playables.isPlayable(match.name)) {
                    return "'${match.name}' is not a media file the player supports"
                }
                val ok = repository.playFile(match.absolutePath, match.name, appContext)
                if (ok) "ok: playing '${match.name}' — ✕ closes it." else "error: could not open the file"
            }
            "play_torrent" -> {
                val name = args.optString("name", "").lowercase()
                if (name.isBlank()) return "error: name is empty"
                val t = repository.torrents.value.find { it.name.lowercase().contains(name) || it.infoHash.startsWith(name) }
                    ?: return "no torrent found matching: " + name
                if (t.progress <= 0.0f) {
                    return "error: '${t.name}' has no data yet (${t.state}); playback needs at least some pieces. Try again shortly."
                }
                // playVideo resolves the largest video file (engine lookup if
                // the path is not cached), registers the loopback stream URL
                // and launches the player; its return value is exact.
                val launched = repository.playVideo(t, appContext)
                if (launched) {
                    "ok: streaming '${t.name}' (${(t.progress * 100).toInt()}%) in the player — ✕ closes it."
                } else {
                    "error: could not open '${t.name}' in the player (no playable video file found yet)."
                }
            }
            else -> "error: unknown tool " + call.name
        }
    }

    // ---- Chat loop with tool rounds ----

    fun send(text: String) {
        if (text.isBlank() || _busy.value) return
        val key = EmbeddedKey.effectiveKey(repository.prefs.value.customApiKey)
        if (key.isBlank()) {
            _messages.value = _messages.value + ChatBubble("error", Strings.t.noApiKey)
            return
        }
        // Fresh memory in the system prompt every turn (saved-this-chat entries count).
        history[0] = ChatClient.Message("system", systemPrompt())
        _busy.value = true
        history.add(ChatClient.Message("user", text))
        _messages.value = _messages.value + ChatBubble("user", text)

        val client = ChatClient(apiKey = key)
        Thread {
            try {
                stopped = false
                // No round cap (owner decision 2026-11-16: always-YOLO, no
                // limits — mirrors the desktop client dropping its
                // wall-clock task cap). The loop runs until the model
                // returns final content or the user presses Stop; the Stop
                // button is the only bound. Do NOT re-add a rounds limit.
                var sawTools = false
                while (!stopped) {
                    val assistantIdx = _messages.value.size
                    _messages.value = _messages.value + ChatBubble("assistant", "", streaming = true)

                    var reasoning = StringBuilder()
                    val result = client.complete(
                        history, toolSchemas(),
                        onDelta = { chunk -> appendTo(assistantIdx) { c -> c.copy(content = c.content + chunk) } },
                        onReasoning = { chunk ->
                            reasoning.append(chunk)
                            appendTo(assistantIdx) { c -> c.copy(reasoning = reasoning.toString()) }
                        },
                    )
                    seal(assistantIdx)

                    when (result) {
                        is ChatClient.Result.Content -> {
                            history.add(ChatClient.Message("assistant", result.text))
                            // Some models answer a tool round with an empty
                            // content message — keep it in history (protocol)
                            // but never leave the user with silence after a
                            // ⚡ tool line (P1).
                            if (result.text.isBlank() && sawTools) {
                                val idx = assistantIdx
                                _messages.value = _messages.value.filterIndexed { i, _ -> i != idx }
                                _messages.value = _messages.value + ChatBubble(
                                    "assistant",
                                    Strings.t.emptyReplyFallback,
                                )
                            } else {
                                persistMessages()
                            }
                            return@Thread
                        }
                        is ChatClient.Result.Tools -> {
                            sawTools = true
                            history.add(ChatClient.Message("assistant", toolCallsJson = result.rawJson))
                            // show a tool note bubble
                            val names = result.calls.joinToString(", ") { it.name }
                            _messages.value = _messages.value + ChatBubble(
                                "assistant", "⚡ tool: " + names, streaming = false
                            )
                            for (tc in result.calls) {
                                val output = runCatching { executeTool(tc) }
                                    .getOrElse { "error: " + (it.message?.take(120) ?: "unknown") }
                                history.add(
                                    ChatClient.Message("tool", output, toolCallId = tc.id)
                                )
                            }
                            // loop: model will see tool results next round
                        }
                    }
                }
                // Loop exit without Content = the user pressed Stop mid-turn;
                // nothing to append — the Stop button already said so.
            } catch (e: Exception) {
                _messages.value = _messages.value + ChatBubble(
                    "error", Strings.t.chatFailedPrefix + (e.message?.take(220) ?: "network error")
                )
                // drop trailing unanswered user turn so retry is clean
                while (history.isNotEmpty() && history.last().role == "user") history.removeAt(history.size - 1)
            } finally {
                _busy.value = false
            }
        }.apply { isDaemon = true }.start()
    }

    private fun appendTo(idx: Int, mutate: (ChatBubble) -> ChatBubble) {
        val cur = _messages.value.getOrNull(idx) ?: return
        val updated = _messages.value.toMutableList()
        updated[idx] = mutate(cur)
        _messages.value = updated
    }

    private fun seal(idx: Int) {
        appendTo(idx) { it.copy(streaming = false) }
    }
}

@Composable
fun ChatScreen(session: ChatSession) {
    val messages by session.messages.collectAsState()
    val busy by session.busy.collectAsState()
    var input by remember { mutableStateOf("") }
    val listState = rememberLazyListState()

    LaunchedEffect(messages.size, messages.lastOrNull()?.content?.length) {
        if (messages.isNotEmpty()) listState.animateScrollToItem(messages.size - 1)
    }

    Column(modifier = Modifier.fillMaxSize().imePadding()) {
        Box(modifier = Modifier.weight(1f).fillMaxWidth()) {
            if (messages.isEmpty()) {
                // The desktop Agent tab's idle state: the owner's Nov banner
                // (same asset, DeepFluxNovBanner.webp), aspect-preserved,
                // nothing else — owner 2026-11-15: no example text, and the
                // empty state is painted with the banner's OWN background
                // (its edges are rgb(0,0,2)) so no "square" outline shows
                // against the app's #05070C base. Gone once a chat starts.
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .background(BannerBackground),
                    contentAlignment = Alignment.Center,
                ) {
                    Column(
                        horizontalAlignment = Alignment.CenterHorizontally,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Image(
                            painter = painterResource(space.deepflux.android.R.drawable.banner_agent),
                            contentDescription = "DeepFlux",
                            // Fit keeps the whole banner visible at its 2057:764
                            // aspect ratio at every width.
                            contentScale = ContentScale.Fit,
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp)
                                .heightIn(max = 300.dp),
                        )
                        Spacer(Modifier.height(10.dp))
                        // Always-visible build tag (owner 2026-11-15): lets a
                        // tester read the running version off the idle screen.
                        Text(
                            "v${space.deepflux.android.BuildConfig.VERSION_NAME}",
                            style = MaterialTheme.typography.labelSmall,
                            color = DimText.copy(alpha = 0.7f),
                        )
                    }
                }
            } else {
                LazyColumn(
                    state = listState,
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    items(messages) { bubble ->
                        Bubble(bubble)
                    }
                }
            }
            // Clear-chat overlay (owner request 2026-11-15): 🗑 top-right of
            // the message area, only while a conversation exists.
            if (messages.isNotEmpty()) {
                androidx.compose.material3.TextButton(
                    onClick = { session.clear() },
                    modifier = Modifier.align(Alignment.TopEnd),
                ) {
                    Text(
                        "🗑",
                        style = MaterialTheme.typography.labelLarge,
                        color = DimText,
                    )
                }
            }
        }
        Row(
            modifier = Modifier.fillMaxWidth().padding(10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                value = input,
                onValueChange = { input = it },
                placeholder = { Text(Strings.t.messagePlaceholder) },
                modifier = Modifier.weight(1f),
                maxLines = 4,
                enabled = !busy,
            )
            Spacer(Modifier.width(8.dp))
            // Voice input (plan §13 #11): the system speech UI, no permission
            // needed (the recognizer app handles its own mic access).
            val ctx = androidx.compose.ui.platform.LocalContext.current
            val speechAvailable = remember {
                android.speech.SpeechRecognizer.isRecognitionAvailable(ctx)
            }
            if (speechAvailable) {
                val speechLauncher = androidx.activity.compose.rememberLauncherForActivityResult(
                    androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()
                ) { result ->
                    val spoken = result.data
                        ?.getStringArrayListExtra(android.speech.RecognizerIntent.EXTRA_RESULTS)
                        ?.firstOrNull()
                    if (!spoken.isNullOrBlank()) {
                        input = if (input.isBlank()) spoken else "$input $spoken"
                    }
                }
                IconButton(onClick = {
                    val intent = android.content.Intent(android.speech.RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                        putExtra(
                            android.speech.RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                            android.speech.RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
                        )
                        putExtra(android.speech.RecognizerIntent.EXTRA_PROMPT, Strings.t.messagePlaceholder)
                    }
                    runCatching { speechLauncher.launch(intent) }
                }) {
                    Icon(
                        space.deepflux.android.ui.AppIcons.Mic,
                        contentDescription = Strings.t.voiceInput,
                        tint = Accent,
                    )
                }
            }
            if (busy) {
                androidx.compose.material3.OutlinedButton(
                    onClick = { session.stop() },
                    modifier = Modifier.padding(0.dp),
                ) { Text(Strings.t.stop, style = MaterialTheme.typography.labelSmall) }
            } else {
                IconButton(
                    enabled = input.isNotBlank(),
                    onClick = {
                        session.send(input.trim())
                        input = ""
                    },
                ) {
                    Icon(Icons.AutoMirrored.Filled.Send, contentDescription = Strings.t.send, tint = Accent)
                }
            }
        }
    }
}

@Composable
private fun Bubble(bubble: ChatBubble) {
    val mine = bubble.role == "user"
    val isError = bubble.role == "error"
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = if (mine) Arrangement.End else Arrangement.Start,
    ) {
        Surface(
            shape = RoundedCornerShape(14.dp),
            color = when {
                isError -> space.deepflux.android.ui.theme.ErrorRed.copy(alpha = 0.15f)
                mine -> Accent.copy(alpha = 0.25f)
                else -> Card
            },
            modifier = Modifier.widthIn(max = 320.dp),
        ) {
            Column(modifier = Modifier.padding(10.dp)) {
                if (bubble.reasoning.isNotBlank()) {
                    Text(
                        bubble.reasoning,
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText.copy(alpha = 0.6f),
                        maxLines = 4,
                        overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis,
                    )
                }
                Text(
                    bubble.content.ifBlank { if (bubble.streaming) "…" else "" },
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (isError) space.deepflux.android.ui.theme.ErrorRed
                    else androidx.compose.ui.graphics.Color.Unspecified
                )
            }
        }
    }
}
