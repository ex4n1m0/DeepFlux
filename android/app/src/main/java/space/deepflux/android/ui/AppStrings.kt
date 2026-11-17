package space.deepflux.android.ui

/**
 * The string-catalog contract — implemented by [EnStrings] and [ZhStrings]
 * so the compiler keeps both catalogs key-identical. Screens consume it via
 * `Strings.t.xxx`, which resolves to the active language at read time.
 */
interface AppStrings {
    // Tabs / common
    val downloads: String
    val agent: String
    val liveTv: String
    val settings: String
    val save: String
    val retry: String
    val errorPrefix: String

    // Downloads
    val noDownloads: String
    val noDownloadsHint: String
    val addMagnet: String
    val addMagnetTitle: String
    val magnetPlaceholder: String
    // Plain-HTTP downloads lane (Phase A1) + the combined Downloads surface
    val addDownload: String
    val addDownloadTitle: String
    val urlOrMagnetPlaceholder: String
    val invalidTarget: String
    val torrentsSection: String
    val filesSection: String
    val noFiles: String
    val noFilesHint: String
    val queued: String
    val sequential: String
    val invalidMagnet: String
    val add: String
    val cancel: String
    val downloading: String
    val fetchingInfo: String
    val checking: String
    val done: String
    val seeding: String
    val paused: String
    val error: String
    val resume: String
    val remove: String
    val play: String
    val seedsPeersFmt: String

    // Agent chat
    val messagePlaceholder: String
    val send: String
    val stop: String
    val chatFailedPrefix: String
    val noApiKey: String

    // Live TV
    val liveTvSetup: String
    val liveTvHint: String
    val m3uPlaylist: String
    val xtreamCodes: String
    val playlistUrl: String
    val epgUrl: String
    val load: String
    val serverUrl: String
    val username: String
    val password: String
    val login: String
    val loggingIn: String
    val tryPrefix: String
    val loading: String
    val loadedChannelsFmt: String
    val loginFailedPrefix: String

    // Settings
    val apiKey: String
    val saveLocation: String
    val saveLocationCustom: String
    val saveLocationDefault: String
    val chooseFolder: String
    val downloadsSection: String
    val wifiOnly: String
    val wifiOnlyDesc: String
    val chargingOnly: String
    val chargingOnlyDesc: String
    val sequentialDefault: String
    val sequentialDesc: String
    val aiAgentKey: String
    val builtInKey: String
    val noBuiltInKey: String
    val customKey: String
    val saveKey: String
    val languageTitle: String
    val about: String
    val androidTwin: String

    // Player
    val connecting: String
    val playbackFailed: String
    val playerInitFailedPrefix: String
    val closePlayer: String
    val audioUnsupported: String
    val videoUnsupported: String

    // Browser (Phase A2)
    val browser: String
    val urlHint: String
    val newTab: String
    val closeTab: String
    val backDesc: String
    val forwardDesc: String
    val reloadDesc: String
    val openExternal: String
    val browserStart: String
    val videosFound: String
    val grabVideo: String
    val streamFlagged: String
    val blobFlagged: String

    // Commander (Phase B)
    val commander: String
    val appStorage: String
    val publicDownloads: String
    val publicVideos: String
    val items: String
    val storageUsedFmt: String
    val emptyFolder: String
    val folder: String
    val share: String
    val rename: String
    val details: String
    val deleteTitle: String
    val confirmDeleteFmt: String
    val selectedFmt: String
    val nameLabel: String
    val sizeLabel: String
    val modifiedLabel: String
    val typeLabel: String
    val sourceApp: String
    val sourcePublic: String

    // Polish pass 1 (plan §13)
    val pause: String
    val voiceInput: String
    val voiceUnavailable: String
    val resumeAtFmt: String
    val startOver: String
    val emptyReplyFallback: String

    // Bookmarks + history (§13 #3)
    val bookmarks: String
    val history: String
    val bookmarkThis: String
    val unbookmark: String
    val bookmarksHistory: String
    val clearHistory: String
    val noBookmarks: String
    val noHistory: String

    // RSS feeds (§13 #4)
    val rssSection: String
    val noFeeds: String
    val noFeedsHint: String
    val addFeed: String
    val feedUrlLabel: String
    val keywordsLabel: String
    val autoDownload: String
    val refresh: String
    val editFeed: String
    val deleteFeed: String
    val feedItemsCountFmt: String

    // Settings backup (§13 #6) + subtitles (§13 #8)
    val backupTitle: String
    val backupHint: String
    val exportSettings: String
    val importSettings: String
    val exportDoneFmt: String
    val passphraseLabel: String
    val passphraseAgainLabel: String
    val passphraseMismatch: String
    val importRestartMsg: String
    val restartToApply: String
    val ok: String
    val subtitlesSection: String
    val subtitlesHint: String
    val subtitlesSearching: String
    val subtitlesNone: String

    // Page-links scan (§13 #9)
    val linksFound: String
    val pagesSection: String
    val openAction: String

    // Multiview (§13 #15)
    val listView: String
    val gridView: String

    // Chat room (§13 #17)
    val roomTab: String
    val roomIntro: String
    val roomNick: String
    val roomWord: String
    val joinRoom: String
    val leaveRoom: String
    val roomHost: String
    val roomConnecting: String
    val roomMessageHint: String

    // Browser v1.3: error page + homepage + quick links
    val pageUnavailable: String
    val pageUnavailableHint: String
    val quickLinks: String

    // Settings v1.3: categories + homepage
    val storageSection: String
    val browserSection: String
    val servicesSection: String
    val homepageLabel: String
    val homepageDesc: String

    // Live TV v1.3: persistent playlists
    val playlistLabel: String
    val playlistNameLabel: String
    val playlistKindLabel: String
    val playlistTypeM3u: String
    val playlistTypeXtream: String
    val playlistTypeLocal: String
    val playlistAll: String
    val favorites: String
    val uiModeTitle: String
    val uiModeTv: String
    val uiModeMobile: String
    val uiModeTablet: String

    // In-app updater (§13 #7)
    val updateAvailableTitle: String
    val updateNow: String
    val updateLater: String
    val updateSkip: String
    val updateCheckNow: String
    val updateAutoCheck: String
    val updateDownloading: String
    val updateVerifying: String
    val updateReady: String
    val upToDate: String
    val updateFailedFmt: String
    val addPlaylist: String
    val editPlaylist: String
    val managePlaylists: String
    val deletePlaylistTitle: String
    val confirmDeletePlaylistFmt: String
    val samplePlaylist: String

    // Commander v1.3: SAF folders + more public collections
    val foldersSection: String
    val addFolder: String
    val noFoldersYet: String
    val storageAccessNote: String
    val publicMusic: String
    val publicPictures: String
    val folderInaccessible: String
}
