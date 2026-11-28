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
    val saveLocationDefault: String
    /** %s = the chosen folder's name. */
    val saveLocationCustomFmt: String
    /** The chosen folder can no longer be opened. */
    val saveLocationLost: String
    val chooseFolder: String
    val changeFolder: String
    val useDefaultFolder: String
    val downloadsSection: String
    val wifiOnly: String
    val wifiOnlyDesc: String
    val chargingOnly: String
    val chargingOnlyDesc: String
    val sequentialDefault: String
    val sequentialDesc: String
    val aiAgentTitle: String
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
    // Player controls: buffering state, rotation, aspect ratio, audio track
    val playerBuffering: String
    val playerRotation: String
    val rotationLandscape: String
    val rotationPortrait: String
    val rotationAuto: String
    val playerAspect: String
    val aspectFit: String
    val aspectFill: String
    val aspectStretch: String
    val playerAudioTrack: String
    val audioUnsupported: String
    val videoUnsupported: String

    // Compatibility player (1.4.15): VLC's engine, used only when this device cannot decode the stream.
    /** Shown by the built-in player while it hands the stream over. */
    val openingCompatPlayer: String
    /** What the second player is called on screen. */
    val compatPlayer: String
    /** VLC could not play the stream either. */
    val compatPlayerFailed: String
    /** VLC itself could not start on this device. */
    val compatPlayerUnavailable: String
    /** A track picker's "none of these" entry. */
    val playerTracksOff: String
    /** Settings → About: the licences of the libraries the app ships (FFmpeg, VLC). */
    val openSourceLicences: String

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
    // Browsing sections (Live TV / Movies / Series) + channel search
    val sectionLive: String
    val sectionMovies: String
    val sectionSeries: String
    val searchChannels: String
    val clearSearch: String
    val noMatchingChannels: String
    // Per-source content override (source editor)
    val contentTypeLabel: String
    val contentTypeAuto: String
    val contentTypeLive: String
    val contentTypeVod: String
    val contentTypeHint: String
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
    /** Shown before the system's "Install unknown apps" screen is opened from the update dialog. */
    val updateNeedsPermission: String
    val updateAllowInstalls: String
    /** The user came back without allowing installs from DeepFlux. */
    val updatePermissionDenied: String
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

    // Fixes batch (2026-11-23): EPG next line, torrent-add confirmation,
    // browser tab cap / crash reload, updater honesty, destructive-tap
    // confirmations, chat copy.
    val epgNextFmt: String
    val addTorrentTitle: String
    val addTorrentNameUnknown: String
    val addTorrentInfoHashFmt: String
    // Where the request came from — the dialog always says
    val addTorrentFromFmt: String
    val addTorrentFromOtherApp: String
    val addTorrentFromFrame: String
    val torrentFileLabel: String
    val tabLimitReached: String
    val pageCrashedReloaded: String
    val pageKeepsCrashing: String
    val updateCheckFailedFmt: String
    val updateWaitingNetwork: String
    val clearChatTitle: String
    val clearChatMsg: String
    // Downloads toolbar: one control for both lanes, as in the notification
    val pauseAll: String
    val resumeAll: String
    val removeDownloadTitle: String
    val removeDownloadPartial: String
    val removeDownloadKeepFile: String
    val removeTorrentFmt: String
    val deleteFeedFmt: String
    val copy: String
    val copied: String

    // Browser notices (2026-11-23): these were English-only snackbars.
    val noticeNoExternalBrowser: String
    val noticeUnsupportedDownloadFmt: String
    val noticeDownloadingFmt: String
    val noticeTorrentFetchFailedFmt: String
    val noticeTorrentTooLarge: String
    val noticeTorrentDownloadFailedFmt: String
    val noticeNotTorrent: String
    val noticeLoginRequired: String
    val noticeStreamUnsupported: String
    val noticeUnsupportedVideo: String

    // Engine notices (2026-11-23): torrent + HTTP/HLS repository snackbars.
    val noticeDownloadFinishedFmt: String
    val noticeDownloadFailedFmt: String
    val noticeAlreadyInList: String
    val noticeInvalidMagnet: String
    val noticeCouldNotAddMagnet: String
    val noticeAddedFetching: String
    val noticeCouldNotReadTorrent: String
    val noticeTorrentAdded: String
    val noticeEngineStartFailed: String
    // A page firing add requests faster than the user can answer them
    val noticePageAddStorm: String
    // A torrent whose files include a symbolic link is not kept
    val noticeTorrentHasSymlinks: String
    /** Same refusal, for a .torrent FILE: nothing to remove — it never got in. */
    val noticeTorrentHasSymlinksFile: String
    /** An RSS item's "Download" could not even be started (link refused or not a magnet). */
    val noticeRssItemRefused: String
    /** A setting could not be written (the storage is full, most likely); the app goes on. */
    val noticeSettingNotSaved: String
    /** The downloaded update is not whole any more when the installer is about to start. */
    val updateFileIncomplete: String
    val noticeCouldNotAddTorrent: String
    /** Settings → "Save location": the system would not keep access to the picked folder. */
    val noticeFolderNotKept: String
    /** A finished download could not be copied out for the user. %s = the file's name. */
    val noticeExportFailedFmt: String
    /** Same, for a torrent. %d = how many files. */
    val noticeExportFailedCountFmt: String
    /** Android 9 only: no public Downloads collection exists, so the default destination does not. */
    val saveLocationNoDownloads: String
    // The browser: a page's download volley; an agent-opened tab kept off the local network
    val noticePageDownloadStorm: String
    val noticeAgentBlockedLan: String
    val noticePaused: String
    val noticeResumed: String
    val noticeRemovedFmt: String
    val noticeNoVideoYet: String
    val noticeStreamServerFailed: String
    val noticeCannotOpenItem: String
    val noticeFileGoneFmt: String

    // Commander notices (2026-11-23): failed deletes/renames used to pass in silence.
    val noticeDeletedFmt: String
    val noticeDeleteFailedFmt: String
    val noticeRenameFailed: String
    val noticeRenamedToFmt: String

    // Updater (2026-11-23): honest results + Play-installed builds.
    val noticeUpdateAvailableFmt: String
    val noticeInstallerFailed: String
    val updateViaPlay: String

    // Chat loop (2026-11-23): localized tool notes + network-error fallback.
    val toolNoteFmt: String
    val chatNetworkError: String

    // AI provider / model / thinking level (1.4.16): the chat talks to whichever the user picks.
    /** Chat refusal: [provider] has no key of the user's own and the built-in one is Venice's. */
    val needsOwnKeyFmt: String
    /** Chat refusal: the AI settings do not describe a usable endpoint. */
    val chatSettingsProblemFmt: String
    val llmProviderVenice: String
    val llmProviderDeepseek: String
    val llmProviderOpenrouter: String
    val llmProviderCustom: String
    val llmModelLabel: String
    /** [model]: the provider's default, shown under an empty model field. */
    val llmModelDefaultFmt: String
    /** Under the model field of a custom endpoint, which has no default to name. */
    val llmModelCustomHint: String
    val llmBaseUrlLabel: String
    /** Under the base URL field: where the address ends (the client adds /chat/completions). */
    val llmBaseUrlHint: String
    val llmThinking: String
    val llmThinkingDesc: String
    val llmEffortNone: String
    val llmEffortLow: String
    val llmEffortMedium: String
    val llmEffortHigh: String
    val llmEffortMax: String
    val llmCustomSendsEffort: String
    val llmCustomSendsEffortDesc: String
    val llmNeedsOwnKey: String
    /** The key field's label for a provider that needs the user's own key. */
    val llmKeyForFmt: String
    val llmKeyHintVenice: String
    val llmKeyHintDeepseek: String
    val llmKeyHintOpenrouter: String
    val llmKeyHintCustom: String

    // Saved tasks (1.4.16): every conversation is kept; the list, Rename, Delete, New task.
    val tasksTitle: String
    val close: String
    val newTask: String
    val renameTask: String
    val deleteTask: String
    /** The "⋯" button of a task row (not a visible label). */
    val taskActions: String
    val tasksEmpty: String
    /** Why the rows are disabled: a reply is running. */
    val tasksBusy: String
    val taskNameLabel: String
    /** [name]: the task about to be deleted for good. */
    val deleteTaskTitleFmt: String
    val deleteTaskMsg: String
}
