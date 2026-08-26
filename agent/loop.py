"""ReAct-style agent loop with optional autonomous watchdog mode."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.llm import LLMClient, LLMMessage, create_llm_client
from agent.tools import ToolRegistry
from config import DeeptorrentConfig
from engine import TorrentEngine

logger = logging.getLogger(__name__)


DESTRUCTIVE_TOOLS = {
    "add_magnet",
    "add_torrent_file",
    "add_download",
    "remove_torrent",
    "add_tracker",
    "propose_rename_and_category",
    # IRC actions with real-world side effects (posting under the user's
    # nick / joining channels / changing connection state) — confirm first.
    "irc_send_message",
    "irc_join",
    "irc_part",
    "irc_connect",
    "irc_disconnect",
    "irc_send_action",
    "irc_send_notice",
    "irc_set_nick",
    "irc_send_raw",
    # Filesystem mutations and IPTV playback (changes what's on screen and
    # throttles torrents while playing) — always confirm first.
    "create_folder",
    "copy_path",
    "move_path",
    "rename_path",
    "delete_path",
    "iptv_play",
    # Download cancellation deletes the partial file by default; feed
    # subscriptions mutate the persisted config — always confirm first.
    "cancel_download",
    "add_rss_feed",
    "remove_rss_feed",
    # Live-page interactions can submit forms / trigger downloads under the
    # user's logged-in sessions — always confirm first.
    "browser_click",
    "browser_fill",
}

REQUIRES_CONFIRMATION = DESTRUCTIVE_TOOLS

# Read-only tools are independent and side-effect free — when the model emits
# several in one turn they are executed concurrently instead of sequentially.
READ_ONLY_TOOLS = {
    "list_torrents",
    "get_torrent_status",
    "get_swarm_stats",
    "diagnose_swarm",
    "search_indexers",
    "find_alt_trackers",
    "find_alt_release",
    "refresh_tracker_list",
    "web_search",
    "web_fetch",
    "list_rss_feeds",
    "get_rss_feed_items",
    "search_memory",
    # IRC monitoring: buffer reads are side-effect free.
    "irc_status",
    "irc_list_messages",
    "irc_search_messages",
    # Filesystem browsing and IPTV reads (playlist search/list/EPG/status).
    "list_directory",
    "iptv_search",
    "iptv_list",
    "iptv_epg",
    "iptv_now_playing",
    "iptv_find_subtitles",
    # IRC nick-list reads and browser reads (tab list, live DOM, bookmarks).
    "irc_list_nicks",
    "browser_list_tabs",
    "browser_get_content",
    "browser_list_bookmarks",
    # Download-manager queue reads.
    "list_downloads",
}

# Older tool results in the conversation history are truncated before being
# sent back to the LLM so long search/fetch outputs don't slow down every
# subsequent call. The most recent results stay intact.
TOOL_HISTORY_KEEP_FULL = 4
TOOL_HISTORY_MAX_CHARS = 1500

SYSTEM_PROMPT = """You are DeepFlux, an LLM-assisted BitTorrent download manager.
You control a local libtorrent engine through the tools provided.

Identity (IMPORTANT): your name and the app's name is **DeepFlux** — always call
yourself and the app "DeepFlux", never "DeepTorrent". The legacy data directory
(~/.deeptorrent) still uses the old name; ignore that, it is not the product name.

Rules:
- Analyze the user's request and pick the best tool(s).
- Batch independent tool calls into a single turn whenever possible (e.g. several web_fetch/search calls at once) — you have a limited number of turns.
- If a user asks about a torrent, call list_torrents or get_torrent_status first.
- If the user wants to add a torrent, confirm the save path and category.
- If you need to modify trackers, add a new torrent, remove a torrent with delete_files=True, or rename/move files, you MUST ask the user for explicit confirmation unless auto-heal mode is enabled.
- When a tool returns data, summarize it briefly for the user.
- For stalled torrents, use diagnose_swarm and suggest escalation steps.
- Never hand-roll BitTorrent protocol logic; rely on the engine tools.

Web access:
- You have a `web_search` tool that queries every configured provider in parallel (DuckDuckGo always — keyless — plus Brave and Perplexity when their keys are set) and merges the results, deduped by URL. When Perplexity contributes, the result carries an `answer` field — a synthesized summary you can use directly — and a `providers` field listing which backends answered.
- Choosing the search path (IMPORTANT — don't waste time or API calls):
  - If the query names a specific, unambiguous title/release (e.g. "ubuntu 24.04", "Dune Part Two 2024 2160p"), go STRAIGHT to `search_indexers`.
  - If the query is vague, descriptive, or time-relative ("that new sci-fi movie with the bald guy", "the latest episode of ...", "this week's ..."), call `web_search` FIRST to pin down the exact title/year/version, THEN call `search_indexers` with the refined terms. Tell the user what you identified in one short line ("Identified as Dune: Part Two (2024) — searching…"). These two calls are dependent — never batch them into one turn.
  - If the user asks a question rather than requesting content ("is there a 4K remaster of X?", reviews, comparisons, "what is ..."), `web_search` alone may be the whole answer — answer it, then offer to find the torrent.
- You have a `web_fetch` tool to read the full text content of any web page URL. When you need to read several pages, pass them together via the `urls` array in a single call — it's much faster. web_fetch results may include `magnets` and `torrent_urls` fields extracted directly from the page HTML — when present, use them with add_magnet right away instead of searching the page text.
- Direct downloads (complement to torrents): `web_fetch` results may also include a `download_links` field — direct file/stream URLs found on the page (archives, videos, installers, .m3u8/.mpd streams). When the user asks for a direct download, provides a file/stream URL, or torrents come up empty but a page offers the file directly (official mirrors, archive.org, release pages), use `add_download` to start it in the built-in download manager (segmented + resumable; HLS/DASH streams are remuxed to MP4). add_download requires the same explicit user confirmation as add_magnet — name the file and link before calling it.
- `search_indexers` accepts an optional `category` — you may pass an app category name ("Movies", "TV", "Software") and it will be mapped automatically. Its results are deduplicated and ranked by relevance and seeders. A `magnets` field may also be present at the top level of search results.
- Use these to find torrent releases, tracker lists, reviews, subtitles, release info, or anything else.
- `search_indexers` prefers private trackers: it searches them first and only falls back to public sources when private ones yield nothing usable. When results include a `source_type` field, prefer "private" results (better quality/speed, and they usually require seeding back — mention this to the user when relevant).
- If `search_indexers` returns `source: "web_search"`, it means no Jackett indexer is configured (or it returned no results) — use the web results to find magnets or .torrent download links, then use web_fetch to read the page and extract them.
- If `search_indexers` returns `source: "source_web_search"`, it means Jackett is not configured but the user has enabled source websites — the results are site-specific searches from those sources. Use web_fetch to read the pages and extract magnets or .torrent links.
- The user can manage their source list via Config → Sources... in the GUI. Enabled sources are searched even without Jackett.
- Search workflow: `search_indexers` runs a QUICK pass on the top source(s) by default — fast, and usually enough. Present those results first. If they look thin or the user asks for more options, ASK whether to search deeper ("Want me to sweep all sources?") before calling `search_indexers` again with `deep=true` — a deep sweep is slower. If the quick pass found nothing, a deep sweep already ran automatically; say so instead of offering one. When results include `deep_available: true`, mention that a deeper sweep is available.
- ALWAYS mention where results came from: include a **Source** column in result tables (the tracker/indexer name from each result's `indexer` or `source_name` field), and note the tier when relevant (e.g. "from your private trackers" or "from public sites"). Users want to know which source delivered.
- When `search_indexers` returns an `about` field, open your reply with a short **About** section BEFORE the results table: what it is, year, rating/genres (TMDb) or description (Wikipedia), and a 1–2 sentence overview — this confirms to the user that you found the right thing. Link the title when `about.url` is present. Omit the section silently when `about` is absent.

RSS feeds:
- The user may have configured RSS feed subscriptions (managed via Config → RSS Feeds... in the GUI).
- Use `list_rss_feeds` to see all configured feeds and their modes.
- Use `get_rss_feed_items` to fetch new items from a feed. Items may contain magnet_uri or torrent_url fields.
- Use `download_from_feed` to download specific items by their index (0-based) from a feed.
- Feeds in "monitor" mode are loaded but NOT auto-downloaded — the user must ask you to download items.
- Feeds in "auto_download" mode are automatically downloaded by the background monitor; you don't need to handle those.
- When the user asks "what's new in my feeds" or "show me my RSS feeds", call list_rss_feeds then get_rss_feed_items for each.
- `add_rss_feed` / `remove_rss_feed` manage the subscriptions themselves (persisted to config; require confirmation).

Download manager (the "Downloads" panel):
- `add_download` starts downloads; the queue is fully manageable: `list_downloads` (job ids, status, progress), `pause_download` / `resume_download` / `retry_download`, `remove_download` (clears completed/errored entries, keeps files), and `cancel_download` (also deletes the partial file by default — requires confirmation; name the file first).
- Torrent sessions can be tuned live: `set_torrent_rate_limits` (global KB/s, 0 = unlimited), `set_sequential_download` (stream a video while it downloads), `force_recheck` (after files changed on disk) and `force_reannounce` (stalled swarms).

Memory:
- You have persistent memory stored as markdown files on the user's drive — it survives restarts. Everything already saved is shown below under "Long-term memory" (empty on first run).
- Use `save_memory` when the user states a stable preference (scope "user" — e.g. "Prefers 1080p", "Never seed above 1:1"), when you learn a durable fact or decision (scope "fact"), or to log what was done today (scope "note"). One concise line per entry. Don't save transient task state, and NEVER save API keys, passwords or passkeys.
- Use `search_memory` to recall past preferences and facts — check before asking the user something they may have already told you.

IRC:
- The app has an embedded IRC client (the "IRC" tab). Every channel the user has open is continuously buffered — you can monitor them without joining anything new.
- Use `irc_status` to see connected networks and open channels, `irc_list_messages` to read a channel's recent buffer (optionally only the last N minutes), and `irc_search_messages` to find keywords across all open channels ("did anyone mention X?").
- `irc_join` / `irc_part` / `irc_send_message` / `irc_send_action` / `irc_send_notice` / `irc_set_nick` / `irc_send_raw` / `irc_connect` / `irc_disconnect` have real-world side effects and REQUIRE explicit user confirmation — always show the exact message text before sending. Never mass-message or advertise; IRC networks ban flooding quickly.
- `irc_list_channels` fetches the network's channel list (server LIST, top channels by user count; pass refresh=false to reuse the last list) and `irc_list_nicks` shows who's in a joined channel with its topic.
- If nothing is connected yet, use `irc_connect` when the user asks (a configured network must exist), or tell the user to add one from the IRC tab (Networks… button).

IPTV (the "Play" tab):
- The app has an IPTV player with the user's playlist sources. Use `iptv_search` / `iptv_list` to find live channels, movies and series, `iptv_epg` for now/next guide data, and `iptv_now_playing` for the player state.
- `iptv_play` starts playback — by item id, by name query (best match plays), by direct stream URL, or by local file path. It requires user confirmation and only works in the GUI. Series can't be played directly; name a specific episode instead. `iptv_pause` / `iptv_stop` / `iptv_set_volume` control the running player.
- If searches come back empty, the playlist likely isn't loaded — tell the user to pick a source in the Play tab or add one in Settings → IPTV.

Filesystem (the "Command" tab's domain):
- You can browse and manage local files: `list_directory` to browse, and `create_folder` / `copy_path` / `move_path` / `rename_path` / `delete_path` to make changes.
- All mutations REQUIRE explicit user confirmation — state the exact paths before calling. `delete_path` is permanent (no recycle bin); never use `recursive=true` without the user explicitly asking to remove a whole folder tree.

Embedded browser (the "Browse" tab):
- You can fully drive the app's embedded browser: `browser_navigate` (URLs or plain search terms), `browser_list_tabs` / `browser_switch_tab` / `browser_close_tab` for tab management, `browser_go` for back/forward/reload/stop/home, and `browser_scroll`.
- `browser_get_content` reads the LIVE rendered page (title, text, links) — unlike web_fetch it sees JavaScript-rendered content and the user's logged-in sessions. Prefer web_fetch for quick static reads; use the browser when a page needs JS or login.
- `browser_click` (CSS selector or visible text) and `browser_fill` (selector + value, optional form submit) interact with the page. Both REQUIRE explicit user confirmation — a click can submit forms, start downloads, or act under the user's accounts. Describe exactly what you'll click/submit before calling.
- `browser_add_bookmark` / `browser_remove_bookmark` / `browser_list_bookmarks` manage bookmarks.
- A typical flow: browser_navigate → browser_get_content → (confirm) browser_click/browser_fill → browser_get_content again to verify the result.

Output formatting (IMPORTANT — your output is rendered as rich text in a GUI):
- Use **Markdown** for all responses. The GUI converts it to formatted HTML.
- Use `## Heading` for section titles, `**bold**` for emphasis, `- bullet` for lists.
- When presenting search results or multiple options to the user, ALWAYS use a numbered Markdown table with clear columns. When the tool data includes seeders/leechers/size, ALWAYS show them — use the FULL release name (never truncate filenames). Example:

| # | Name | Size | Seeds | Leeches | Source |
|---|------|------|-------|---------|--------|
| 1 | Ubuntu 24.04 Desktop amd64 | 6.2 GB | 120 | 14 | IPTorrents |
| 2 | Ubuntu 24.04 Server amd64 | 3.2 GB | 85 | 3 | Nyaa.si |

- If a result's seeder/leecher counts are unknown (e.g. plain web pages), write "—" in those columns instead of omitting them.

- After the table, add a clear instruction line like:
  **Type the number of your choice (e.g., "1") to download, or ask me for more details.**
- Keep responses concise. Don't repeat the raw tool output — summarize it.
- When the user types just a number (e.g. "1"), treat it as selecting option #1 from your last table.
- Use `code` formatting for hashes, paths, and technical values.
- Use `---` to separate sections when needed.

Current default save path: {default_save_path}
Categories: {categories}
Auto-heal enabled: {auto_heal}
Indexer configured: {indexer_configured}
{memory}
Today's date: {today}
- Use this date for anything time-sensitive: when the user asks for "new", "latest", "recent", or "this week/month" content, include the current year (and month where relevant) in search queries instead of relying on training data.
"""


@dataclass
class PendingAction:
    tool_name: str
    arguments: Dict[str, Any]
    reasoning: str
    tool_call_id: Optional[str] = None


class AgentLoop:
    """ReAct agent loop that bridges user messages, LLM tool calls and engine state."""

    def __init__(
        self,
        engine: TorrentEngine,
        config: DeeptorrentConfig,
        tools: Optional[ToolRegistry] = None,
        llm: Optional[LLMClient] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.engine = engine
        self.config = config
        self.tools = tools or ToolRegistry(engine, config)
        self.llm = llm or create_llm_client(config.llm)
        self.history: List[Dict[str, Any]] = []
        self.pending: List[PendingAction] = []
        self._watchdog: Optional[threading.Thread] = None
        self._stop_watchdog = threading.Event()
        self._last_progress: Dict[str, Tuple[float, float]] = {}
        self._on_event = on_event
        # Surface fine-grained tool sub-steps (per-indexer search progress etc.).
        if hasattr(self.tools, "on_progress"):
            self.tools.on_progress = lambda msg: self._emit("tool_progress", message=msg)

    def _emit(self, event_type: str, **data: Any) -> None:
        """Emit a progress event to the callback (if set)."""
        if self._on_event:
            try:
                self._on_event({"type": event_type, **data})
            except Exception:
                logger.debug("event callback failed", exc_info=True)

    def chat(self, message: str, auto_confirm: bool = False) -> Dict[str, Any]:
        """Run one full ReAct turn for a user message and return the final response."""
        if self.pending and (message.strip().lower() in ("yes", "y", "confirm", "ok", "proceed")):
            self.history.append({"role": "user", "content": message})
            return self._execute_pending()

        self.history.append({"role": "user", "content": message})
        return self._react(auto_confirm=auto_confirm)

    def _pruned_history(self) -> List[Dict[str, Any]]:
        """Copy of the history with older tool results truncated and the whole
        thing capped at config.llm.history_budget messages.

        Full results are kept for the most recent tool messages; older ones
        are capped at TOOL_HISTORY_MAX_CHARS. self.history is left untouched
        so the full data remains available for display. When the budget forces
        a cut, the window is advanced to a safe boundary so no tool message is
        left without its assistant tool_calls message (the API rejects that)."""
        tool_idx = [i for i, m in enumerate(self.history) if m.get("role") == "tool"]
        keep_full = set(tool_idx[-TOOL_HISTORY_KEEP_FULL:])
        pruned: List[Dict[str, Any]] = []
        for i, m in enumerate(self.history):
            content = m.get("content")
            if (
                m.get("role") == "tool"
                and i not in keep_full
                and isinstance(content, str)
                and len(content) > TOOL_HISTORY_MAX_CHARS
            ):
                m = dict(m)
                m["content"] = content[:TOOL_HISTORY_MAX_CHARS] + "... [truncated]"
            pruned.append(m)
        return self._trim_to_budget(pruned)

    def _trim_to_budget(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep the newest `history_budget` messages, cut at a safe boundary."""
        budget = self.config.llm.history_budget
        if not budget or len(messages) <= budget:
            return messages
        window = messages[-budget:]
        for i, m in enumerate(window):
            role = m.get("role")
            if role == "user":
                return window[i:]
            if role == "assistant":
                tool_calls = m.get("tool_calls") or []
                if not tool_calls:
                    return window[i:]
                # Safe only if every tool response follows inside the window.
                ids = {tc.get("id") for tc in tool_calls}
                following = window[i + 1 : i + 1 + len(ids)]
                if (
                    len(following) == len(ids)
                    and all(f.get("role") == "tool" for f in following)
                    and {f.get("tool_call_id") for f in following} >= ids
                ):
                    return window[i:]
            # tool messages fall through — orphaned here, unsafe to keep.
        return []

    def _make_delta_callback(self, streamed: Dict[str, bool]) -> Optional[Callable[[str, str], None]]:
        """Token-streaming callback for the LLM client, or None when disabled.

        `streamed` is mutated to record which kinds ("content"/"reasoning")
        were streamed live, so complete values aren't re-emitted afterwards."""
        if not (self._on_event and self.config.llm.stream):
            return None

        def _on_delta(kind: str, text: str) -> None:
            streamed[kind] = True
            self._emit("stream_delta", kind=kind, text=text)

        return _on_delta

    def _react(self, auto_confirm: bool = False, max_turns: Optional[int] = None) -> Dict[str, Any]:
        max_turns = max_turns or self.config.llm.max_turns
        messages = [self._system_message()] + self._pruned_history()

        for turn in range(max_turns):
            self._emit("thinking", turn=turn + 1, max_turns=max_turns)
            streamed: Dict[str, bool] = {}
            try:
                reply = self.llm.chat(
                    messages,
                    tools=self.tools.list_tools(),
                    on_delta=self._make_delta_callback(streamed),
                )
            except Exception as exc:
                logger.exception("LLM call failed")
                self._emit("error", message=f"LLM request failed: {exc}")
                return {
                    "role": "assistant",
                    "content": f"LLM request failed: {exc}",
                    "tool_calls": [],
                }

            # Surface the model's thinking when it wasn't streamed live.
            if reply.reasoning and not streamed.get("reasoning"):
                self._emit("model_reasoning", content=reply.reasoning)

            if not reply.tool_calls:
                self.history.append({"role": "assistant", "content": reply.content})
                self._emit("done", content=reply.content)
                return {"role": "assistant", "content": reply.content, "tool_calls": []}

            # Check for pending confirmations.
            confirmations = [
                tc for tc in reply.tool_calls if tc["function"]["name"] in REQUIRES_CONFIRMATION
            ]
            if confirmations and not (auto_confirm or self.config.watchdog.auto_heal):
                self.pending = []
                for tc in confirmations:
                    self.pending.append(
                        PendingAction(
                            tool_name=tc["function"]["name"],
                            arguments=json.loads(tc["function"]["arguments"]),
                            reasoning=reply.content or "",
                            tool_call_id=tc.get("id"),
                        )
                    )
                names = [p.tool_name for p in self.pending]
                ask = (
                    f"I need your confirmation before executing: {', '.join(names)}. "
                    f"Reasoning: {reply.content or 'No additional reasoning.'} "
                    "Reply 'yes' to proceed."
                )
                self.history.append({"role": "assistant", "content": ask})
                self._emit("pending_confirmation", tools=names, reasoning=reply.content or "")
                return {"role": "assistant", "content": ask, "tool_calls": reply.tool_calls, "pending_confirmation": True}

            # Record the assistant message with tool_calls before the tool results.
            assistant_msg = {
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": self._openai_format_tool_calls(reply.tool_calls),
            }
            self.history.append(assistant_msg)
            messages.append(assistant_msg)

            # Narration accompanying tool calls — skip when it already streamed live.
            if reply.content and not streamed.get("content"):
                self._emit("reasoning", content=reply.content)

            # Execute all tool calls and feed back observations.
            for tc in reply.tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}
                self._emit("tool_start", tool=name, args=args)
            tool_messages = self._execute_tool_calls(reply.tool_calls, auto_confirm=auto_confirm)
            for tm in tool_messages:
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tm["tool_call_id"],
                    "content": json.dumps(tm["content"]),
                }
                self.history.append(tool_msg)
                messages.append(tool_msg)
                self._emit("tool_end", tool=tm.get("tool_name", ""), summary=tm.get("summary", ""), result_keys=list(tm["content"].keys()) if isinstance(tm["content"], dict) else [], result=tm["content"])

        final = "I need more turns to complete this request. Please ask me to continue, or break it into smaller steps."
        self.history.append({"role": "assistant", "content": final})
        self._emit("done", content=final)
        return {"role": "assistant", "content": final, "tool_calls": []}

    def _execute_tool_calls(self, tool_calls: List[Dict[str, Any]], auto_confirm: bool = False) -> List[Dict[str, Any]]:
        def _run(tc: Dict[str, Any]) -> Dict[str, Any]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            try:
                result = self.tools.call(name, args)
            except Exception as exc:
                result = {"success": False, "error": str(exc)}
            return {
                "tool_call_id": tc.get("id", "call_0"),
                "tool_name": name,
                "content": result,
                "summary": self._summarize_tool_result(result),
            }

        # When every call in the batch is read-only, run them concurrently —
        # pool.map preserves the original order of results. Mixed batches with
        # state-changing tools stay sequential to preserve execution order.
        if len(tool_calls) > 1 and all(tc["function"]["name"] in READ_ONLY_TOOLS for tc in tool_calls):
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, len(tool_calls))) as pool:
                return list(pool.map(_run, tool_calls))
        return [_run(tc) for tc in tool_calls]

    def _execute_pending(self) -> Dict[str, Any]:
        if not self.pending:
            return {"role": "assistant", "content": "No pending actions.", "tool_calls": []}

        # Build the conversation for the LLM summary.
        messages = [self._system_message()] + self._pruned_history()

        # Add the assistant message with the confirmed tool calls.
        tool_calls = self._openai_format_tool_calls(
            [
                {
                    "id": p.tool_call_id or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": p.tool_name,
                        "arguments": json.dumps(p.arguments),
                    },
                }
                for i, p in enumerate(self.pending)
            ]
        )
        assistant_msg = {"role": "assistant", "content": "Executing confirmed action(s).", "tool_calls": tool_calls}
        self.history.append(assistant_msg)
        messages.append(assistant_msg)

        # Execute the tools and add results to the conversation.
        tool_messages = []
        for p in self.pending:
            self._emit("tool_start", tool=p.tool_name, args=p.arguments)
            try:
                result = self.tools.call(p.tool_name, p.arguments)
            except Exception as exc:
                result = {"success": False, "error": str(exc)}
            self._emit("tool_end", tool=p.tool_name, summary=self._summarize_tool_result(result), result=result)
            tool_call_id = p.tool_call_id or "call_0"
            tool_msg = {"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps(result)}
            tool_messages.append(tool_msg)
            self.history.append(tool_msg)
            messages.append(tool_msg)
        self.pending = []

        # Ask the LLM to summarize the executed actions.
        execution_summary = "\n".join(
            [f"- Executed {tm['tool_call_id']}: {json.dumps(tm['content'])[:200]}" for tm in tool_messages]
        )
        try:
            # Summarizing executed actions doesn't need deep reasoning — use
            # the fast model (flash) with low effort when configured.
            reply = self.llm.chat(
                messages,
                tools=self.tools.list_tools(),
                effort="low",
                model=self.config.llm.fast_model or None,
            )
            if reply.reasoning:
                self._emit("model_reasoning", content=reply.reasoning)
            if reply.tool_calls:
                # If the LLM decides to make more tool calls, run a full ReAct step.
                return self._react_from_messages(messages)
            llm_summary = reply.content or "Done."
            summary = f"{execution_summary}\n{llm_summary}"
        except Exception as exc:
            logger.exception("LLM summary call failed")
            summary = f"{execution_summary}\nActions executed, but the LLM summary failed: {exc}"

        self.history.append({"role": "assistant", "content": summary})
        return {"role": "assistant", "content": summary, "tool_calls": []}

    def _react_from_messages(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Continue a ReAct loop from an existing message list."""
        for turn in range(max(5, self.config.llm.max_turns // 2)):
            streamed: Dict[str, bool] = {}
            try:
                reply = self.llm.chat(
                    messages,
                    tools=self.tools.list_tools(),
                    on_delta=self._make_delta_callback(streamed),
                )
            except Exception as exc:
                logger.exception("LLM call failed")
                return {"role": "assistant", "content": f"LLM request failed: {exc}", "tool_calls": []}
            if reply.reasoning and not streamed.get("reasoning"):
                self._emit("model_reasoning", content=reply.reasoning)
            if not reply.tool_calls:
                self.history.append({"role": "assistant", "content": reply.content})
                return {"role": "assistant", "content": reply.content, "tool_calls": []}

            # Execute all tool calls and feed back observations.
            assistant_msg = {
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": self._openai_format_tool_calls(reply.tool_calls),
            }
            self.history.append(assistant_msg)
            messages.append(assistant_msg)

            for tm in self._execute_tool_calls(reply.tool_calls):
                tool_msg = {"role": "tool", "tool_call_id": tm["tool_call_id"], "content": json.dumps(tm["content"])}
                self.history.append(tool_msg)
                messages.append(tool_msg)

        final = "I need more turns to complete this request. Please ask me to continue, or break it into smaller steps."
        self.history.append({"role": "assistant", "content": final})
        return {"role": "assistant", "content": final, "tool_calls": []}

    def _system_message(self) -> Dict[str, str]:
        return {
            "role": "system",
            "content": self._render_system_prompt(),
        }

    def _render_system_prompt(self) -> str:
        """Build the system prompt with live values (today's date included)."""
        import datetime
        memory_section = ""
        store = getattr(self.tools, "memory", None)
        if store is not None:
            try:
                memory_section = store.prompt_section()
            except Exception:
                logger.debug("memory prompt section failed", exc_info=True)
        return SYSTEM_PROMPT.format(
            default_save_path=self.config.default_save_path,
            categories=", ".join(self.config.categories),
            auto_heal=self.config.watchdog.auto_heal,
            indexer_configured=bool(self.config.indexer.api_key),
            memory=memory_section,
            today=datetime.date.today().strftime("%A, %Y-%m-%d"),
        )

    @staticmethod
    def _summarize_tool_result(result: Any) -> str:
        """One-line human summary of a tool result for the UI."""
        if not isinstance(result, dict):
            return ""
        if result.get("success") is False:
            return f"failed — {str(result.get('error', 'unknown'))[:100]}"
        if result.get("count") is not None:
            rows = [r for r in result.get("results", []) if isinstance(r, dict)]
            seeds = [r.get("seeders", 0) or 0 for r in rows]
            best = f", best {max(seeds)} seeds" if seeds else ""
            leeches = [r.get("leechers", 0) or 0 for r in rows]
            if leeches and any(leeches):
                best += f" / {max(leeches)} leeches"
            return f"{result['count']} result(s){best}"
        if result.get("health"):
            return str(result.get("summary", result["health"]))
        if result.get("magnets"):
            return f"{len(result['magnets'])} magnet(s) found"
        if result.get("new_items") is not None:
            return f"{result['new_items']} new item(s)"
        if result.get("total_downloaded") is not None:
            return f"{result['total_downloaded']} downloaded, {result.get('total_failed', 0)} failed"
        if result.get("info_hash"):
            return "added to engine"
        if result.get("trackers") is not None:
            return f"{result.get('count', len(result['trackers']))} trackers"
        return ""

    @staticmethod
    def _openai_format_tool_calls(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "id": tc.get("id", "call_0"),
                "type": tc.get("type", "function"),
                "function": tc["function"],
            }
            for tc in tool_calls
        ]

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def start_watchdog(self) -> None:
        if self._watchdog and self._watchdog.is_alive():
            return
        self._stop_watchdog.clear()
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="agent-watchdog", daemon=True)
        self._watchdog.start()

    def stop_watchdog(self) -> None:
        self._stop_watchdog.set()
        if self._watchdog and self._watchdog.is_alive():
            self._watchdog.join(timeout=5)

    def _watchdog_loop(self) -> None:
        interval = max(10, self.config.watchdog.stall_threshold_seconds // 2)
        while not self._stop_watchdog.is_set():
            if self.config.watchdog.enabled:
                try:
                    self._watchdog_check()
                except Exception:
                    logger.exception("Watchdog check failed")
            self._stop_watchdog.wait(interval)

    def _watchdog_check(self) -> None:
        for t in self.engine.list_torrents():
            info_hash = t["info_hash"]
            progress = t["progress"]
            now = time.time()
            last_progress, last_time = self._last_progress.get(info_hash, (0.0, 0.0))
            if progress == last_progress:
                stalled = now - last_time > self.config.watchdog.stall_threshold_seconds
            else:
                self._last_progress[info_hash] = (progress, now)
                stalled = False

            if stalled and progress < 1.0:
                logger.info("WATCHDOG: stalled torrent %s (progress %.4f for %.0fs)", info_hash, progress, now - last_time)
                self._escalate(t)

    def _watchdog_emit(self, message: str) -> None:
        """Surface watchdog activity in the chat when debug mode is on."""
        if getattr(self.config, "ui_agent_debug", True):
            self._emit("watchdog", message=message)

    def _escalate(self, status: Dict[str, Any]) -> None:
        info_hash = status["info_hash"]
        diagnosis = self.tools.call("diagnose_swarm", {"info_hash": info_hash})
        if diagnosis["health"] in ("healthy", "completed"):
            return

        logger.info("WATCHDOG_ESCALATE info_hash=%s health=%s cause=%s", info_hash, diagnosis["health"], diagnosis["cause"])
        torrent_label = status.get("name") or info_hash[:12]
        self._watchdog_emit(f"Stalled torrent detected: {torrent_label} — {diagnosis['health']}, {diagnosis.get('cause', 'unknown')}. Investigating…")

        prompt = self._escalation_prompt(diagnosis)
        # Run a short ReAct loop without user in the loop.
        messages = [
            {"role": "system", "content": self._render_system_prompt()},
            {"role": "user", "content": prompt},
        ]

        for turn in range(5):
            try:
                reply = self.llm.chat(messages, tools=self.tools.list_tools())
            except Exception as exc:
                logger.exception("Watchdog LLM call failed")
                break

            if not reply.tool_calls:
                logger.info("WATCHDOG_REASONING info_hash=%s summary=%s", info_hash, reply.content)
                if reply.content:
                    self._watchdog_emit(f"Watchdog conclusion for {torrent_label}: {reply.content}")
                break

            for tc in reply.tool_calls:
                name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"])
                # Destructive actions only execute when auto-heal is on.
                if name in REQUIRES_CONFIRMATION and not self.config.watchdog.auto_heal:
                    logger.info("WATCHDOG_CONFIRMATION_REQUIRED info_hash=%s tool=%s args=%s reasoning=%s",
                                info_hash, name, json.dumps(args), reply.content)
                    self._watchdog_emit(f"Watchdog wants to run {name} but needs auto-heal — skipped.")
                    continue
                try:
                    result = self.tools.call(name, args)
                    logger.info("WATCHDOG_ACTION info_hash=%s tool=%s result=%s", info_hash, name, json.dumps(result)[:500])
                    self._watchdog_emit(f"Watchdog action: {name} — {self._summarize_tool_result(result) or 'done'}")
                except Exception as exc:
                    result = {"success": False, "error": str(exc)}
                    logger.error("WATCHDOG_ACTION_FAILED info_hash=%s tool=%s error=%s", info_hash, name, exc)
                    self._watchdog_emit(f"Watchdog action failed: {name} — {exc}")
                messages.append({"role": "tool", "content": json.dumps(result), "tool_call_id": tc.get("id", "call_0")})

            messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": self._openai_format_tool_calls(reply.tool_calls)})

    def _escalation_prompt(self, diagnosis: Dict[str, Any]) -> str:
        return (
            f"Torrent {diagnosis['info_hash']} is {diagnosis['health']}. "
            f"Cause: {diagnosis.get('cause', 'unknown')}. "
            "Try to recover this swarm autonomously. Follow this escalation order: "
            "1) refresh_tracker_list, 2) find_alt_trackers, 3) search_indexers for a better-seeded duplicate, "
            "4) find_alt_release. "
            "If you find a better source, return its magnet or download URL. "
            "You may call add_tracker with discovered tracker URLs and add_magnet only if auto-heal is enabled."
        )
