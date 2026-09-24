"""Simplified Chinese catalog for gui/i18n.py — phase 1 (app shell).

Keys are the exact English source strings (``&&`` mnemonics included — Qt
consumes them before display, and the Chinese side drops mnemonics
entirely). Brand/product names stay Latin: DeepFlux, Jackett, FFmpeg,
OpenSubtitles, RSS, IPTV, API, PDF, TMDb, OMDb, TPDB, StashDB, Brave,
Perplexity. Proper nouns never get translated.

Scope so far: window title, tab rail, File/Settings/Help menus, settings
page titles (shared by the menu, the Settings Hub and the Play gear),
onboarding cards, Settings Hub entries, status-bar chips. Phase 2 covers
per-tab toolbars/dialogs; phase 3 the in-app user guide.
"""
from __future__ import annotations

ZH_CN: dict[str, str] = {
    # --- window title suffix ---
    "AI Deep Search": "AI 深度搜索",

    # --- activity rail / tab titles ---
    "Agent": "智能体",
    "Browse": "浏览",
    "Download": "下载",
    "Play": "播放",
    "Command": "文件管理",
    "Room": "聊天室",

    # --- menu bar ---
    "File": "文件",
    "Settings": "设置",
    "Help": "帮助",
    "Bookmarks": "书签",

    # --- File menu ---
    "Add Magnet Link...": "添加磁力链接...",
    "Add .torrent File...": "添加 .torrent 文件...",
    "Save Page as PDF...": "将页面保存为 PDF...",
    "Back Up All Settings...": "备份全部设置...",
    "Restore Settings from Backup...": "从备份恢复设置...",
    "Set as Default App for Magnets && Media...": "设为磁力链接和媒体的默认程序...",
    "Exit": "退出",

    # --- Settings menu ---
    "Settings Hub… (search)": "设置中心…（搜索）",
    "Language": "语言",
    "AI & API Keys": "AI 与 API 密钥",
    "Downloads && Sources": "下载与源",
    # single-& form: the Settings Hub shows the literal string (not a QMenu,
    # so no Qt mnemonic unescaping happens there)
    "Downloads & Sources": "下载与源",
    "Browser": "浏览器",
    "Jackett Indexer Settings...": "Jackett 索引器设置...",
    "Torrent Search Sources...": "种子搜索源...",
    "RSS Feed Subscriptions...": "RSS 订阅...",
    "Browser History...": "浏览器历史...",
    "Browser Developer Tools": "浏览器开发者工具",
    "The new language applies after DeepFlux restarts.": "新语言将在 DeepFlux 重启后生效。",

    # --- settings page titles (menu + Settings Hub + Play gear share these) ---
    "Torrent Download Settings…": "种子下载设置…",
    "Torrent Queue Settings…": "种子队列设置…",
    "Download Manager Settings…": "下载管理器设置…",
    "Browser General Settings…": "浏览器常规设置…",
    "Browser Privacy && Data…": "浏览器隐私与数据…",
    "AI Agent Key…": "AI 智能体密钥…",
    "Search Keys (Jackett, Brave, Perplexity)…": "搜索密钥（Jackett、Brave、Perplexity）…",
    "Movie && TV Metadata Keys (TMDb, OMDb)…": "影视元数据密钥（TMDb、OMDb）…",
    "Adult Metadata Keys (TPDB, StashDB)…": "成人元数据密钥（TPDB、StashDB）…",
    "OpenSubtitles Key…": "OpenSubtitles 密钥…",
    "IPTV Playlist Sources…": "IPTV 播放列表源…",
    "Artwork, Metadata && Cache…": "海报、元数据与缓存…",
    "Subtitles && Languages…": "字幕与语言…",
    "Playback && Video Settings…": "播放与视频设置…",

    # --- Help menu ---
    "User Guide": "用户指南",
    "Check for Updates…": "检查更新…",
    "About": "关于",

    # --- onboarding cards ---
    "Add a download": "添加下载",
    "Paste a magnet link or add a .torrent file": "粘贴磁力链接或添加 .torrent 文件",
    "Add an AI key": "添加 AI 密钥",
    "The built-in shared key works out of the box; add your own to be independent":
        "内置共享密钥开箱即用；添加自己的密钥可完全独立",
    "Connect Jackett": "连接 Jackett",
    "Link Jackett to search dozens of torrent indexers from the agent":
        "连接 Jackett，让智能体能搜索数十个种子索引站",
    "Add a playlist": "添加播放列表",
    "Add an IPTV playlist (M3U or Xtream) for live TV, movies and series":
        "添加 IPTV 播放列表（M3U 或 Xtream），观看直播、电影和剧集",

    # --- Settings Hub entries (categories, descriptions; titles reuse the
    #     rstrip("…") forms of the page labels above) ---
    "Open the key manager": "打开密钥管理器",
    "Jackett Indexer Settings": "Jackett 索引器设置",
    "Service URL, key, auto-start, sync": "服务地址、密钥、自动启动、同步",
    "Torrent / queue / manager preferences": "种子 / 队列 / 下载管理器偏好",
    "Torrent Search Sources": "种子搜索源",
    "Which indexers the agent searches": "智能体搜索哪些索引站",
    "RSS Feed Subscriptions": "RSS 订阅",
    "Feeds and auto-download rules": "订阅源与自动下载规则",
    "Browser preferences": "浏览器偏好",
    "Browser History": "浏览器历史",
    "Recently visited pages": "最近访问的页面",
    "Back Up All Settings": "备份全部设置",
    "Export everything as an encrypted .dfc": "将全部设置导出为加密的 .dfc 文件",
    "IPTV / player preferences": "IPTV / 播放器偏好",

    # --- status bar chips ---
    "⬇ idle": "⬇ 空闲",
    "Active downloads — click to open the Download page": "活动下载 —— 点击打开下载页",
    "🧠 agent": "🧠 智能体",
    "Agent state — click to open the quick-ask panel (Ctrl+K)": "智能体状态 —— 点击打开快速提问面板 (Ctrl+K)",
    "Recent events — completions, syncs, updates": "最近事件 —— 完成、同步、更新",
    "Ready": "就绪",
}
