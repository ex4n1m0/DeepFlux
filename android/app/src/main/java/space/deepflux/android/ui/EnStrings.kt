package space.deepflux.android.ui

/**
 * English string catalog — the app's DEFAULT language. Key-identical to
 * [ZhStrings] by implementing [AppStrings]; add a key there when adding
 * one here (the interface forces both).
 */
object EnStrings : AppStrings {
    // Tabs / common
    override val downloads = "Downloads"
    override val agent = "Agent"
    override val liveTv = "Live TV"
    override val settings = "Settings"
    override val save = "Save"
    override val retry = "Retry"
    override val errorPrefix = "Error: "

    // Downloads
    override val noDownloads = "No downloads yet"
    override val noDownloadsHint =
        "Open a magnet link, share to DeepFlux, or tap “Add magnet”."
    override val addMagnet = "Add download"
    override val addMagnetTitle = "Add magnet link or file URL"
    override val magnetPlaceholder = "magnet:?xt=urn:btih:… or https://…/file.mp4"
    override val addDownload = "Add download"
    override val addDownloadTitle = "Add magnet link or file URL"
    override val urlOrMagnetPlaceholder = "magnet:?xt=urn:btih:… or https://…/file.mp4"
    override val invalidTarget = "That is not a valid magnet link or http(s) URL."
    override val torrentsSection = "Torrents"
    override val filesSection = "Files"
    override val noFiles = "No file downloads yet"
    override val noFilesHint = "Add an https:// link — the browser and the video grabber also land here."
    override val queued = "Queued"
    override val sequential = "Sequential (stream-first)"
    override val invalidMagnet = "That is not a valid magnet link."
    override val add = "Add"
    override val cancel = "Cancel"
    override val downloading = "Downloading"
    override val fetchingInfo = "Fetching info"
    override val checking = "Checking"
    override val done = "Done"
    override val seeding = "Seeding"
    override val paused = "Paused"
    override val error = "Error"
    override val resume = "Resume"
    override val remove = "Remove"
    override val play = "Play"
    override val seedsPeersFmt = "%1${'$'}d seeds · %2${'$'}d peers"

    // Agent chat
    override val messagePlaceholder = "Message DeepFlux…"
    override val send = "Send"
    override val stop = "Stop"
    override val chatFailedPrefix = "Chat failed: "
    override val noApiKey =
        "No AI key available. Add your own in Settings (the shared built-in key is missing from this build)."

    // Live TV
    override val liveTvSetup = "Live TV Setup"
    override val liveTvHint =
        "Add a playlist (M3U) or log in with Xtream Codes to browse and watch live channels."
    override val m3uPlaylist = "M3U Playlist"
    override val xtreamCodes = "Xtream Codes"
    override val playlistUrl = "Playlist URL (.m3u / .m3u8)"
    override val epgUrl = "EPG URL (optional, XMLTV)"
    override val load = "Load"
    override val serverUrl = "Server URL (e.g. http://provider.com:8080)"
    override val username = "Username"
    override val password = "Password"
    override val login = "Login"
    override val loggingIn = "Logging in…"
    override val tryPrefix = "Try: "
    override val loading = "Loading…"
    override val loadedChannelsFmt = "Loaded %1${'$'}d channels"
    override val loginFailedPrefix = "Login failed: "

    // Settings
    override val apiKey = "API key"
    override val saveLocation = "Save location"
    override val saveLocationCustom = "Custom folder set — files save to your chosen location"
    override val saveLocationDefault =
        "Files save to the app's storage. Choose a visible folder for easier access."
    override val chooseFolder = "Choose folder"
    override val downloadsSection = "Downloads"
    override val wifiOnly = "Wi-Fi only"
    override val wifiOnlyDesc = "Pause on metered networks"
    override val chargingOnly = "Charging only"
    override val chargingOnlyDesc = "Pause on battery"
    override val sequentialDefault = "Sequential by default"
    override val sequentialDesc = "Stream-first piece order"
    override val aiAgentKey = "AI agent key"
    override val builtInKey = "Built-in key: included ✓"
    override val noBuiltInKey = "No built-in key — enter your own below"
    override val customKey = "Custom key (optional — a Venice AI key; overrides the built-in one)"
    override val saveKey = "Save key"
    override val languageTitle = "Language / 语言"
    override val about = "About"
    override val androidTwin = "Android twin of the DeepFlux desktop app"

    // Player
    override val connecting = "Connecting…"
    override val playbackFailed = "Playback failed"
    override val playerInitFailedPrefix = "Failed to initialize player: "
    override val closePlayer = "Close"
    override val audioUnsupported = "No sound: this device cannot decode the audio"
    override val videoUnsupported = "No picture: this device cannot decode the video"

    // Browser
    override val browser = "Browser"
    override val urlHint = "Search or address"
    override val newTab = "New tab"
    override val closeTab = "Close tab"
    override val backDesc = "Back"
    override val forwardDesc = "Forward"
    override val reloadDesc = "Reload"
    override val openExternal = "Open in system browser"
    override val browserStart = "Type an address or search to start browsing"
    override val videosFound = "Videos on this page"
    override val grabVideo = "Download"
    override val streamFlagged = "stream — grabber support coming"
    override val blobFlagged = "in-page blob — not grabbable"

    // Commander
    override val commander = "Commander"
    override val appStorage = "Downloads (app)"
    override val publicDownloads = "Public downloads"
    override val publicVideos = "Videos (public)"
    override val items = "items"
    override val storageUsedFmt = "App storage: %s"
    override val emptyFolder = "Empty"
    override val folder = "Folder"
    override val share = "Share"
    override val rename = "Rename"
    override val details = "Details"
    override val deleteTitle = "Delete?"
    override val confirmDeleteFmt = "Delete %d item(s)? Items deleted from public collections are removed for every app, not just DeepFlux."
    override val selectedFmt = "%d selected"
    override val nameLabel = "Name"
    override val sizeLabel = "Size"
    override val modifiedLabel = "Modified"
    override val typeLabel = "Type"
    override val sourceApp = "In app storage"
    override val sourcePublic = "Public collection"

    // Polish pass 1
    override val pause = "Pause"
    override val voiceInput = "Voice input"
    override val voiceUnavailable = "Voice input is not available on this device"
    override val resumeAtFmt = "Resume at %s"
    override val startOver = "Start over"
    override val emptyReplyFallback = "(the model sent an empty reply — say “go on” to continue)"

    // Bookmarks + history
    override val bookmarks = "Bookmarks"
    override val history = "History"
    override val bookmarkThis = "Bookmark this page"
    override val unbookmark = "Remove bookmark"
    override val bookmarksHistory = "Bookmarks & history"
    override val clearHistory = "Clear history"
    override val noBookmarks = "No bookmarks yet — star a page to keep it"
    override val noHistory = "No history yet"

    // RSS feeds
    override val rssSection = "RSS"
    override val noFeeds = "No RSS feeds"
    override val noFeedsHint = "Add a torrent RSS feed and DeepFlux watches it for new releases"
    override val addFeed = "Add feed"
    override val feedUrlLabel = "Feed URL"
    override val keywordsLabel = "Keywords (comma-separated)"
    override val autoDownload = "Auto-download keyword matches"
    override val refresh = "Refresh"
    override val editFeed = "Edit feed"
    override val deleteFeed = "Remove feed"
    override val feedItemsCountFmt = "%d items"

    // Settings backup + subtitles
    override val backupTitle = "Backup"
    override val backupHint = "Encrypted export of settings, feeds, bookmarks and memory — the same .dfc file the desktop app uses"
    override val exportSettings = "Export"
    override val importSettings = "Import"
    override val exportDoneFmt = "Exported %d KB"
    override val passphraseLabel = "Passphrase"
    override val passphraseAgainLabel = "Repeat passphrase"
    override val passphraseMismatch = "Passphrases do not match"
    override val importRestartMsg = "Settings imported. Restart the app to apply them."
    override val restartToApply = "Restart now"
    override val ok = "OK"
    override val subtitlesSection = "Subtitles"
    override val subtitlesHint = "Optional: an opensubtitles.com API key enables subtitle search in the player"
    override val subtitlesSearching = "Searching OpenSubtitles…"
    override val subtitlesNone = "No subtitles found"

    // Page-links scan
    override val linksFound = "Links on this page"
    override val pagesSection = "Pages"
    override val openAction = "Open"

    // Multiview
    override val listView = "List"
    override val gridView = "Grid"

    // Chat room
    override val roomTab = "Room"
    override val roomIntro = "Pick a nickname and a room word. Everyone typing the same word meets in the same end-to-end encrypted room."
    override val roomNick = "Nickname"
    override val roomWord = "Room word"
    override val joinRoom = "Join"
    override val leaveRoom = "Leave"
    override val roomHost = "host"
    override val roomConnecting = "Connecting to the room hub…"
    override val roomMessageHint = "Message the room…"

    // Browser v1.3
    override val pageUnavailable = "Page unavailable"
    override val pageUnavailableHint = "The site could not be loaded. Check the address or your connection and try again."
    override val quickLinks = "Quick links"

    // Settings v1.3
    override val storageSection = "Downloads & storage"
    override val browserSection = "Browser"
    override val servicesSection = "AI & services"
    override val homepageLabel = "Homepage"
    override val homepageDesc = "Opened when the browser starts and on new tabs"

    // Live TV v1.3
    override val playlistLabel = "Playlist"
    override val playlistNameLabel = "Name"
    override val playlistKindLabel = "Type"
    override val playlistTypeM3u = "M3U URL"
    override val playlistTypeXtream = "Xtream Codes"
    override val playlistTypeLocal = "Local folder"
    override val playlistAll = "All"
    override val favorites = "Favorites"
    override val uiModeTitle = "How do you use DeepFlux?"
    override val uiModeTv = "TV (remote control)"
    override val uiModeMobile = "Mobile"
    override val uiModeTablet = "Tablet"

    // In-app updater (§13 #7)
    override val updateAvailableTitle = "Update available"
    override val updateNow = "Update now"
    override val updateLater = "Later"
    override val updateSkip = "Skip this version"
    override val updateCheckNow = "Check for updates"
    override val updateAutoCheck = "Auto-check for updates"
    override val updateDownloading = "Downloading update…"
    override val updateVerifying = "Verifying…"
    override val updateReady = "Update downloaded — confirm the install when Android asks"
    override val upToDate = "DeepFlux is up to date"
    override val updateFailedFmt = "Update failed: %s"
    override val addPlaylist = "Add playlist"
    override val editPlaylist = "Edit playlist"
    override val managePlaylists = "Manage playlists"
    override val deletePlaylistTitle = "Delete playlist?"
    override val confirmDeletePlaylistFmt = "Delete “%s”? Its cached channels are removed; the other playlists stay."
    override val samplePlaylist = "Try the sample (iptv-org Macau, 7 channels)"

    // Commander v1.3
    override val foldersSection = "Chosen folders"
    override val addFolder = "Add folder…"
    override val noFoldersYet = "No folders chosen yet — add any folder on the device to browse it here."
    override val storageAccessNote =
        "Android protects other apps' private storage and system areas. Your downloads, the public collections and folders you choose are the broadest access any app may have."
    override val publicMusic = "Music (public)"
    override val publicPictures = "Pictures (public)"
    override val folderInaccessible = "This folder is no longer accessible — remove it and choose it again."
}
