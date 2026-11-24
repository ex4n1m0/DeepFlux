package space.deepflux.android.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List

import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationRail
import androidx.compose.material3.NavigationRailItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.material3.windowsizeclass.WindowWidthSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import space.deepflux.android.ui.theme.DimText
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.ui.downloads.DownloadsScreen
import space.deepflux.android.ui.settings.SettingsScreen

enum class Screen { BROWSER, DOWNLOADS, CHAT, LIVE_TV, COMMANDER, ROOM, SETTINGS }

/**
 * Two-screen adaptive shell: bottom NavigationBar on phones (compact),
 * NavigationRail on tablets/foldables/landscape (medium+). The study's
 * "phone and tablets" requirement in its minimal form.
 */
@Composable
fun AppRoot(windowSizeClass: WindowSizeClass, repository: TorrentRepository) {
    // Boot into the Agent tab (owner 2026-11-15): the banner + version idle
    // screen is the app's face; Downloads is one tap away.
    var screen by rememberSaveable { mutableStateOf(Screen.CHAT) }
    val androidCtx = androidx.compose.ui.platform.LocalContext.current
    val iptvRepository = remember {
        space.deepflux.android.iptv.IPTVRepository(
            context = androidCtx.applicationContext,
            downloadsDir = repository.downloadsDir,
        )
    }
    // Process-wide HTTP download engine (DeepFluxApp owns it — see its kdoc).
    val httpDownloads = (androidCtx.applicationContext as space.deepflux.android.DeepFluxApp).httpDownloads
    // RSS feeds (plan §13 #4) — app singleton, shared with the service poll.
    val rssRepository = (androidCtx.applicationContext as space.deepflux.android.DeepFluxApp).rss
    // Live-TV artwork resolver (§14 P1) — app singleton; its caches must
    // survive tab switches.
    val artworkResolver = (androidCtx.applicationContext as space.deepflux.android.DeepFluxApp).artwork
    // Browser FIRST in the nav — the owner's #1 surface (plan §12). The
    // controller needs an ACTIVITY context (WebView JS dialogs/selects);
    // it lives with this composition and destroys its WebViews on dispose.
    val browserStore = remember { space.deepflux.android.browser.BrowserStore(androidCtx.applicationContext) }
    // Homepage (v1.3): read through a provider so a Settings change applies
    // to the NEXT new tab without recreating the controller (its WebViews
    // and session state must survive).
    var homepage by remember { mutableStateOf(space.deepflux.android.browser.BrowserController.DEFAULT_HOMEPAGE) }
    LaunchedEffect(repository) {
        repository.prefs.collect { homepage = it.homepage.ifBlank { space.deepflux.android.browser.BrowserController.DEFAULT_HOMEPAGE } }
    }
    val browserController = remember {
        space.deepflux.android.browser.BrowserController(androidCtx, repository, httpDownloads, browserStore) { homepage }
    }
    androidx.compose.runtime.DisposableEffect(Unit) {
        onDispose { browserController.destroy() }
    }
    // The whole app leaving the foreground reaches the browser from
    // MainActivity: its pages' JavaScript timers stop meanwhile (a hidden
    // tab's onPause() does not do that).
    androidx.compose.runtime.DisposableEffect(browserController) {
        val app = androidCtx.applicationContext as space.deepflux.android.DeepFluxApp
        app.onAppVisibilityChanged = { shown -> if (shown) browserController.onAppShown() else browserController.onAppHidden() }
        onDispose { app.onAppVisibilityChanged = null }
    }
    val chatSession = remember {
        space.deepflux.android.ui.chat.ChatSession(
            repository, iptvRepository, httpDownloads, rssRepository, androidCtx.applicationContext,
            openInBrowser = { url ->
                // Agent's open_url. Whether there is room for a tab is
                // decided HERE, on the chat thread, and reported back to the
                // tool: it used to answer "opened" while the browser had
                // refused the tab and left the user on another page.
                if (!browserController.canOpenTab()) return@ChatSession false
                // WebViews may only be created on a Looper (main) thread ("WebView
                // cannot be initialized on a thread that has no Looper").
                android.os.Handler(android.os.Looper.getMainLooper()).post {
                    // byAgent: in this tab, nothing the page does on its own may
                    // lead the browser to a non-public address.
                    browserController.newTab(url, byAgent = true)
                    screen = Screen.BROWSER
                }
                true
            },
            scanActivePage = { browserController.scanPageSync() },
        )
    }
    val epgRepository = remember { space.deepflux.android.iptv.EPGRepository(context = androidCtx.applicationContext) }
    // The OnlyHumans chat room (plan §13 #17) — one controller per
    // composition; leaving the nav does not dispose it.
    val ohRoom = remember { space.deepflux.android.room.OhRoom(androidCtx.applicationContext) }
    androidx.compose.runtime.DisposableEffect(Unit) {
        onDispose { ohRoom.shutdown() }
    }
    val snackbarHostState = remember { SnackbarHostState() }
    val torrents by repository.torrents.collectAsStateWithLifecycle()
    val httpItems by httpDownloads.items.collectAsStateWithLifecycle()

    LaunchedEffect(repository) {
        repository.notices.collect { snackbarHostState.showSnackbar(it) }
    }

    // Download notifications open the DOWNLOADS tab (fix 2026-11-23) — they
    // used to land on the boot tab, which is the Agent.
    val requestedTab by repository.requestedTab.collectAsStateWithLifecycle()
    LaunchedEffect(requestedTab) {
        val tab = requestedTab ?: return@LaunchedEffect
        Screen.entries.firstOrNull { it.name == tab }?.let { screen = it }
        repository.consumeRequestedTab()
    }

    // In-app updater (§13 #7): one app-scoped instance; the ~daily startup
    // check is deferred so it never competes with first-frame work.
    val appUpdater = (androidCtx.applicationContext as space.deepflux.android.DeepFluxApp).updater
    val updateInfo by appUpdater.available.collectAsStateWithLifecycle()
    val updateProgress by appUpdater.progress.collectAsStateWithLifecycle()
    LaunchedEffect(appUpdater) {
        kotlinx.coroutines.delay(4000)
        appUpdater.autoCheckIfDue()
    }

    val useRail = windowSizeClass.widthSizeClass != WindowWidthSizeClass.Compact

    // ---- Device mode (owner 2026-11-15): first-run TV/Mobile/Tablet picker,
    // persisted; TV mode scales the UI up (10-foot readability) and the
    // focusable rows/cells gain visible focus rings for D-pad navigation.
    val prefs by repository.prefs.collectAsStateWithLifecycle()
    val uiMode = prefs.uiMode.ifBlank {
        // Sensible preselection from the environment.
        val tm = androidCtx.getSystemService(android.content.Context.UI_MODE_SERVICE) as? android.app.UiModeManager
        if (tm?.currentModeType == android.content.res.Configuration.UI_MODE_TYPE_TELEVISION) "tv"
        else if (useRail) "tablet" else "mobile"
    }
    val tvMode = uiMode == "tv"
    val density = androidx.compose.ui.platform.LocalDensity.current
    val scaledDensity = if (tvMode) {
        androidx.compose.ui.unit.Density(density.density, fontScale = density.fontScale * 1.15f)
    } else {
        density
    }

    androidx.compose.runtime.CompositionLocalProvider(
        androidx.compose.ui.platform.LocalDensity provides scaledDensity,
    ) {
    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        bottomBar = {
            if (!useRail) {
                // Adaptive nav bar (owner request 2026-11-15): 7 destinations
                // on a 360dp phone ≈ 51dp per item — labels wrapped mid-word
                // ("Comman/der") and icons crowded. The bar now scales to the
                // space it actually gets: icon-only below 56dp/item, small
                // labels with hidden-unselected below 76dp, full labels above.
                androidx.compose.foundation.layout.BoxWithConstraints {
                    val perItem = maxWidth / Destination.entries.size
                    val showLabels = perItem >= 56.dp
                    val smallLabels = perItem < 76.dp
                    val iconSize = if (perItem < 64.dp) 20.dp else 24.dp
                    NavigationBar {
                        Destination.entries.forEach { dest ->
                            NavigationBarItem(
                                selected = screen == dest.screen,
                                onClick = { screen = dest.screen },
                                icon = {
                                    Icon(
                                        dest.icon,
                                        contentDescription = dest.label,
                                        modifier = Modifier.size(iconSize),
                                    )
                                },
                                label = if (showLabels) {
                                    {
                                        // Whole, or a little smaller — never cut. At a large system
                                        // font "Download" was cut to "Downlo" in its 52dp item
                                        // (emulator, 2026-11-24); hiding what did not fit took the
                                        // label away at the normal font size too (2026-11-25).
                                        val style = if (smallLabels) {
                                            androidx.compose.material3.MaterialTheme.typography.labelSmall
                                        } else {
                                            androidx.compose.material3.MaterialTheme.typography.labelMedium
                                        }
                                        Text(
                                            dest.label,
                                            style = style,
                                            maxLines = 1,
                                            softWrap = false,
                                            autoSize = androidx.compose.foundation.text.TextAutoSize.StepBased(
                                                minFontSize = 7.sp,
                                                maxFontSize = if (style.fontSize == androidx.compose.ui.unit.TextUnit.Unspecified) 12.sp else style.fontSize,
                                                stepSize = 0.25.sp,
                                            ),
                                        )
                                    }
                                } else {
                                    null
                                },
                                alwaysShowLabel = !smallLabels,
                            )
                        }
                    }
                }
            }
        },
    ) { padding ->
        Row(modifier = Modifier.fillMaxSize().padding(padding)) {
            if (useRail) {
                NavigationRail {
                    Destination.entries.forEach { dest ->
                        NavigationRailItem(
                            selected = screen == dest.screen,
                            onClick = { screen = dest.screen },
                            icon = { Icon(dest.icon, contentDescription = dest.label) },
                            label = { Text(dest.label) },
                        )
                    }
                }
            }
            Box(modifier = Modifier.weight(1f).fillMaxSize()) {
                // Leaving a tab disposes its screen. The state holder keeps
                // each tab's saved state — scroll positions, selected items,
                // typed text, every rememberSaveable — so coming back resumes
                // where the user was. Before this every tab switch reset.
                val tabState = androidx.compose.runtime.saveable.rememberSaveableStateHolder()
                tabState.SaveableStateProvider(screen.name) {
                    when (screen) {
                    Screen.BROWSER -> space.deepflux.android.ui.browser.BrowserScreen(controller = browserController)
                    Screen.DOWNLOADS -> DownloadsScreen(
                        torrents = torrents,
                        repository = repository,
                        httpDownloads = httpItems,
                        httpRepo = httpDownloads,
                        rss = rssRepository,
                        isWide = useRail,
                        // Hand-resumes in the list can end "Pause all" too: the
                        // standing "paused" notice must follow.
                        onPauseStateChanged = {
                            (androidCtx.applicationContext as space.deepflux.android.DeepFluxApp).syncPausedNotice()
                        },
                    )
                    Screen.CHAT -> space.deepflux.android.ui.chat.ChatScreen(
                        session = chatSession,
                        // A page link tapped in a reply: the user's own navigation.
                        openUrl = { url ->
                            android.os.Handler(android.os.Looper.getMainLooper()).post {
                                browserController.newTab(url)
                                screen = Screen.BROWSER
                            }
                        },
                    )
                    Screen.LIVE_TV -> space.deepflux.android.ui.iptv.IPTVScreen(
                        repository = iptvRepository,
                        epgRepository = epgRepository,
                        torrentRepo = repository,
                        artwork = artworkResolver,
                    )
                    Screen.COMMANDER -> space.deepflux.android.ui.commander.CommanderScreen(
                        appRoot = repository.downloadsDir,
                        torrentRepo = repository,
                        isWide = useRail,
                    )
                    Screen.ROOM -> space.deepflux.android.ui.room.RoomScreen(room = ohRoom)
                    Screen.SETTINGS -> SettingsScreen(repository = repository)
                    }
                }
            }
        }
    }

    // First-run device-mode picker (owner 2026-11-15): shown once until a
    // choice is persisted; TV gets remote-friendly scaling + focus rings.
    if (prefs.uiMode.isBlank()) {
        val pick: (String) -> Unit = { repository.setUiMode(it) }
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { repository.setUiMode(uiMode) },
            title = { Text(Strings.t.uiModeTitle) },
            text = {
                Column(verticalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(6.dp)) {
                    listOf(
                        "tv" to Strings.t.uiModeTv,
                        "mobile" to Strings.t.uiModeMobile,
                        "tablet" to Strings.t.uiModeTablet,
                    ).forEach { (id, label) ->
                        androidx.compose.material3.OutlinedButton(
                            onClick = { pick(id) },
                            modifier = Modifier.fillMaxWidth(),
                        ) {
                            Text(if (id == uiMode) "● $label" else label)
                        }
                    }
                }
            },
            confirmButton = {
                androidx.compose.material3.TextButton(onClick = { pick(uiMode) }) {
                    Text(Strings.t.ok)
                }
            },
        )
    }

    // Torrent-add confirmation (fix 2026-11-23): a page, a redirect or
    // another app can no longer add torrents unasked — the request waits
    // here for the user (TorrentRepository.requestAddMagnet). The agent's
    // add_torrent tool and the user's own URL-bar / links-sheet taps do not
    // pass through here.
    val pendingAdd by repository.pendingAdd.collectAsStateWithLifecycle()
    val add = pendingAdd
    if (add != null) {
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { repository.cancelPendingAdd() },
            title = { Text(Strings.t.addTorrentTitle) },
            text = {
                Column(verticalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(6.dp)) {
                    // The name is web content: shown cleaned up (no line
                    // breaks, no bidi overrides), see confirmationName.
                    Text(
                        space.deepflux.android.engine.TorrentRepository.confirmationName(add.name)
                            .ifBlank { Strings.t.addTorrentNameUnknown },
                        style = MaterialTheme.typography.bodyLarge,
                    )
                    Text(
                        Strings.t.addTorrentInfoHashFmt.format(add.infoHash),
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText,
                    )
                    // The dialog always says where the request came from. It
                    // used to show a bare host — and nothing at all for
                    // another app's share, which read like one of our own.
                    Text(
                        when {
                            add.fromSubframe -> Strings.t.addTorrentFromFrame
                            add.sourceHost.isNotBlank() -> Strings.t.addTorrentFromFmt.format(add.sourceHost)
                            else -> Strings.t.addTorrentFromOtherApp
                        },
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText,
                    )
                }
            },
            confirmButton = {
                androidx.compose.material3.TextButton(onClick = { repository.confirmPendingAdd() }) {
                    Text(Strings.t.add)
                }
            },
            dismissButton = {
                androidx.compose.material3.TextButton(onClick = { repository.cancelPendingAdd() }) {
                    Text(Strings.t.cancel)
                }
            },
        )
    }

    // Update → install. Android lets the installer start only once the user
    // allowed installs from DeepFlux, so that is asked for FIRST: the
    // installer is then the last thing on top of the app, and its "Done" /
    // "Open" lead back here. (Asked by the installer itself, the Settings
    // screen stayed in the back stack, and the user was left in Settings or
    // on the home screen with the app closed — emulator, 2026-11-24.)
    // The PATH, kept across the activity being rebuilt: Android may kill the
    // app while the system's screen is in front, and the activity that gets
    // the answer is a new one — it used to have forgotten what was being
    // installed, and nothing was installed and nothing was said (review).
    val installConsentFor = rememberSaveable { mutableStateOf<String?>(null) }
    val installConsentAsked = rememberSaveable { mutableStateOf(false) }
    // The offered update's size, kept the same way: the look at the file before
    // the installer starts must not depend on the offer still being in memory.
    val installConsentSize = rememberSaveable { mutableStateOf<Long?>(null) }
    val installConsent = androidx.activity.compose.rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult(),
    ) {
        installConsentAsked.value = false
        val path = installConsentFor.value
        installConsentFor.value = null
        val file = path?.let { java.io.File(it) }
        if (file != null) {
            // The user's own "Allow" answers: it may start the installer although one was
            // started for this file already (one that was cancelled) — as a tap does.
            if (appUpdater.canInstallApps()) appUpdater.install(file, installConsentSize.value, userTap = true) else appUpdater.installsNotAllowed()
        }
    }
    installConsentFor.value?.let {
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { installConsentFor.value = null },
            text = { Text(Strings.t.updateNeedsPermission) },
            confirmButton = {
                androidx.compose.material3.TextButton(
                    onClick = {
                        // Asked once, however many taps: two system screens on top of
                        // each other had the first answer arrive under the second. And a
                        // device without that screen is told, not dropped.
                        if (!installConsentAsked.value) {
                            installConsentAsked.value = true
                            runCatching { installConsent.launch(appUpdater.consentIntent()) }
                                .onFailure {
                                    installConsentAsked.value = false
                                    installConsentFor.value = null
                                    appUpdater.installsNotAllowed()
                                }
                        }
                    },
                ) {
                    Text(Strings.t.updateAllowInstalls)
                }
            },
            dismissButton = {
                androidx.compose.material3.TextButton(onClick = { installConsentFor.value = null }) { Text(Strings.t.cancel) }
            },
        )
    }

    // An update trouble with no dialog to show it — a process started again for the
    // consent answer. The updater holds it until the app is up to say it: the
    // notice flow keeps nothing for a listener that is not there yet.
    LaunchedEffect(appUpdater) {
        appUpdater.undialogged.collect { text ->
            if (text != null) {
                snackbarHostState.showSnackbar(text)
                appUpdater.undialoggedShown()
            }
        }
    }

    /** Hand the verified file to the installer — or ask for the system's consent first. */
    val installOrAsk = { file: java.io.File, userTap: Boolean ->
        if (appUpdater.canInstallApps()) {
            appUpdater.install(file, userTap = userTap)
        } else {
            installConsentAsked.value = false
            installConsentFor.value = file.absolutePath
            installConsentSize.value = appUpdater.available.value?.size
        }
    }

    // Update dialog (§13 #7) — never over the first-run picker.
    val upd = updateInfo
    if (upd != null && prefs.uiMode.isNotBlank()) {
        UpdateDialog(
            info = upd,
            progress = updateProgress,
            onDownload = { appUpdater.startDownload(upd) },
            onCancel = { appUpdater.cancelDownload(); appUpdater.dismiss() },
            onLater = { appUpdater.dismiss() },
            onSkip = { appUpdater.skip(upd.version) { repository.setUpdateSkipVersion(it) } },
            onInstall = { installOrAsk(it, false) },
            onInstallTap = { installOrAsk(it, true) },
        )
    }
    } // CompositionLocalProvider(LocalDensity)
}

/**
 * The self-update flow: offer → download (progress) → sha256 verify → one
 * tap on the SYSTEM installer (Android never allows silent self-install;
 * the "Install unknown apps" grant is requested on the first run).
 */
@Composable
private fun UpdateDialog(
    info: space.deepflux.android.data.UpdateInfo,
    progress: space.deepflux.android.data.UpdateProgress,
    onDownload: () -> Unit,
    onCancel: () -> Unit,
    onLater: () -> Unit,
    onSkip: () -> Unit,
    onInstall: (java.io.File) -> Unit,
    /** The user's own tap on "Update now" — it starts the installer again (see `AppUpdater.install`). */
    onInstallTap: (java.io.File) -> Unit,
) {
    // Hand the verified file to the installer as soon as it lands.
    LaunchedEffect(progress) {
        val p = progress
        if (p is space.deepflux.android.data.UpdateProgress.Ready) onInstall(p.file)
    }
    androidx.compose.material3.AlertDialog(
        onDismissRequest = onLater,
        title = { Text("${Strings.t.updateAvailableTitle} — v${info.version}") },
        text = {
            Column(verticalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(8.dp)) {
                if (info.notes.isNotBlank()) {
                    Text(info.notes, style = MaterialTheme.typography.bodySmall, color = DimText)
                }
                Text(
                    "%.1f MB · %s".format(info.size / 1024.0 / 1024.0, info.released),
                    style = MaterialTheme.typography.labelSmall,
                    color = DimText,
                )
                when (val p = progress) {
                    is space.deepflux.android.data.UpdateProgress.Downloading -> {
                        androidx.compose.material3.LinearProgressIndicator(
                            progress = { if (p.total > 0) p.read.toFloat() / p.total else 0f },
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Text(
                            "${Strings.t.updateDownloading} %d%%".format(
                                if (p.total > 0) (p.read * 100 / p.total).toInt() else 0
                            ),
                            style = MaterialTheme.typography.labelMedium,
                        )
                    }
                    space.deepflux.android.data.UpdateProgress.Verifying ->
                        Text(Strings.t.updateVerifying, style = MaterialTheme.typography.labelMedium)
                    is space.deepflux.android.data.UpdateProgress.Ready ->
                        Text(Strings.t.updateReady, style = MaterialTheme.typography.labelMedium)
                    is space.deepflux.android.data.UpdateProgress.Error ->
                        if (p.waitingForNetwork) {
                            // Not a failure: the Wi-Fi / charging gate is
                            // closed. "Retry" (still shown) starts it once the
                            // gate opens.
                            Text(Strings.t.updateWaitingNetwork, style = MaterialTheme.typography.labelMedium, color = DimText)
                        } else {
                            Text(
                                Strings.t.updateFailedFmt.format(p.message),
                                style = MaterialTheme.typography.labelMedium,
                                color = androidx.compose.ui.graphics.Color(0xFFE57373),
                            )
                        }
                    space.deepflux.android.data.UpdateProgress.Idle -> Unit
                }
            }
        },
        confirmButton = {
            when (val p = progress) {
                space.deepflux.android.data.UpdateProgress.Idle ->
                    androidx.compose.material3.TextButton(onClick = onDownload) { Text(Strings.t.updateNow) }
                is space.deepflux.android.data.UpdateProgress.Error ->
                    androidx.compose.material3.TextButton(onClick = onDownload) { Text(Strings.t.retry) }
                is space.deepflux.android.data.UpdateProgress.Downloading ->
                    androidx.compose.material3.TextButton(onClick = onCancel) { Text(Strings.t.cancel) }
                // Re-fireable install: the first intent may have been eaten by
                // the one-time "install unknown apps" SETTINGS detour — the
                // user returns to a dialog that can still act (found live
                // 2026-11-16) — or cancelled, or answered with "not installed".
                is space.deepflux.android.data.UpdateProgress.Ready ->
                    androidx.compose.material3.TextButton(onClick = { onInstallTap(p.file) }) { Text(Strings.t.updateNow) }
                space.deepflux.android.data.UpdateProgress.Verifying -> Unit
            }
        },
        dismissButton = {
            if (progress is space.deepflux.android.data.UpdateProgress.Idle) {
                androidx.compose.material3.TextButton(onClick = onSkip) { Text(Strings.t.updateSkip) }
            }
            androidx.compose.material3.TextButton(onClick = onLater) { Text(Strings.t.updateLater) }
        },
    )
}

private enum class Destination(val screen: Screen, val icon: androidx.compose.ui.graphics.vector.ImageVector) {
    BROWSER(Screen.BROWSER, Icons.Filled.Search),
    DOWNLOADS(Screen.DOWNLOADS, space.deepflux.android.ui.AppIcons.Download),
    CHAT(Screen.CHAT, space.deepflux.android.ui.AppIcons.Chat),
    LIVE_TV(Screen.LIVE_TV, space.deepflux.android.ui.AppIcons.Tv),
    COMMANDER(Screen.COMMANDER, space.deepflux.android.ui.AppIcons.Folder),
    ROOM(Screen.ROOM, space.deepflux.android.ui.AppIcons.Forum),
    SETTINGS(Screen.SETTINGS, Icons.Filled.Settings),
}

/**
 * Resolved at READ time (inside composition), never cached in the enum —
 * enum vals are evaluated once at class-init, which froze the old labels
 * and mixed hardcoded English with catalog Chinese.
 */
private val Destination.label: String
    get() = when (this) {
        Destination.BROWSER -> Strings.t.browser
        Destination.DOWNLOADS -> Strings.t.downloads
        Destination.CHAT -> Strings.t.agent
        Destination.LIVE_TV -> Strings.t.liveTv
        Destination.COMMANDER -> Strings.t.commander
        Destination.ROOM -> Strings.t.roomTab
        Destination.SETTINGS -> Strings.t.settings
    }
