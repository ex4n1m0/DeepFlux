package space.deepflux.android.ui.browser

import android.view.ViewGroup
import android.widget.FrameLayout
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.automirrored.filled.ExitToApp
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import space.deepflux.android.browser.BrowserController
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.Card
import space.deepflux.android.ui.theme.DimText

/**
 * Browser tab chrome (ANDROID_STUDY.md §12 Phase A2): tab strip, toolbar
 * with URL bar, progress line, and the WebView container owned by
 * [BrowserController]. The controller keeps every tab's WebView alive
 * across recomposition and across switching to other app tabs.
 */
@Composable
fun BrowserScreen(controller: BrowserController) {
    val tabs by controller.tabs.collectAsState()
    val activeId by controller.activeId.collectAsState()
    val fullscreen by controller.fullscreenView.collectAsState()
    val videos by controller.videos.collectAsState()
    val links by controller.pageLinks.collectAsState()
    val active = tabs.find { it.id == activeId }
    var showVideoSheet by remember { mutableStateOf(false) }
    var showBookmarksSheet by remember { mutableStateOf(false) }
    var showLinksSheet by remember { mutableStateOf(false) }
    val grabbableLinks = links.count { it.kind != BrowserController.PageLink.Kind.PAGE }

    // Open the first tab the first time the browser is shown.
    LaunchedEffect(Unit) {
        if (controller.tabs.value.isEmpty()) controller.newTab()
    }
    // Pause page media/timers while another app tab is in front.
    DisposableEffect(Unit) {
        controller.onShown()
        onDispose { controller.onHidden() }
    }

    BackHandler(enabled = fullscreen != null || active?.canGoBack == true) {
        if (!controller.exitFullscreen()) controller.goBack()
    }

    androidx.compose.foundation.layout.BoxWithConstraints(modifier = Modifier.fillMaxSize()) {
        // Portrait phones: the URL bar needs its own row (see Toolbar).
        val compactToolbar = maxWidth < 480.dp
        Column(modifier = Modifier.fillMaxSize()) {
            TabStrip(tabs, activeId, controller)
            Toolbar(active, controller, onShowBookmarks = { showBookmarksSheet = true }, compact = compactToolbar)
            if (active?.loading == true) {
                LinearProgressIndicator(
                    progress = { (active.progress / 100f).coerceIn(0.05f, 1f) },
                    modifier = Modifier.fillMaxWidth().height(2.dp),
                )
            }
            Box(modifier = Modifier.weight(1f).fillMaxWidth()) {
                AndroidView(
                    factory = {
                        // The container is long-lived and re-embedded every time
                        // this screen (re)enters composition — it must be parentless.
                        (controller.container.parent as? ViewGroup)?.removeView(controller.container)
                        controller.container
                    },
                    modifier = Modifier.fillMaxSize(),
                )
                if (active?.error?.isNotBlank() == true) {
                    // Main-frame load failure (v1.3): a real error page with a
                    // way forward, instead of the WebView's dead gray slab.
                    ErrorPage(
                        url = active.url,
                        message = active.error,
                        onRetry = { controller.reload() },
                        onOpenExternal = { controller.openInSystemBrowser() },
                    )
                } else if (active == null || active.url.isBlank()) {
                    // Start page (no homepage set): quick links instead of a
                    // bare "type something" line — the page is tappable.
                    Box(
                        modifier = Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background),
                        contentAlignment = Alignment.Center,
                    ) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(
                                Strings.t.browserStart,
                                style = MaterialTheme.typography.bodyMedium,
                                color = DimText,
                            )
                            Spacer(Modifier.height(14.dp))
                            Text(Strings.t.quickLinks, style = MaterialTheme.typography.labelSmall, color = DimText)
                            Spacer(Modifier.height(8.dp))
                            Row(horizontalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(8.dp)) {
                                listOf("deepflux.space", "duckduckgo.com").forEach { link ->
                                    Surface(
                                        shape = RoundedCornerShape(20.dp),
                                        color = Card,
                                        modifier = Modifier.clickable { controller.loadInput(link) },
                                    ) {
                                        Text(
                                            link,
                                            style = MaterialTheme.typography.labelMedium,
                                            color = Accent,
                                            modifier = Modifier.padding(horizontal = 14.dp, vertical = 7.dp),
                                        )
                                    }
                                }
                            }
                        }
                    }
                } else if (videos.isNotEmpty()) {
                    // A3 grabber chip: videos detected on this page.
                    Surface(
                        shape = RoundedCornerShape(20.dp),
                        color = Accent.copy(alpha = 0.9f),
                        modifier = Modifier
                            .align(Alignment.BottomEnd)
                            .padding(16.dp)
                            .clickable { showVideoSheet = true },
                    ) {
                        Text(
                            "🎬 ${videos.size}",
                            style = MaterialTheme.typography.labelLarge,
                            color = androidx.compose.ui.graphics.Color.Black,
                            modifier = Modifier.padding(horizontal = 14.dp, vertical = 8.dp),
                        )
                    }
                }

                // §13 #9: page-links chip (bottom-start; mirrors the 🎬 chip).
                if (grabbableLinks > 0) {
                    Surface(
                        shape = RoundedCornerShape(20.dp),
                        color = Accent.copy(alpha = 0.9f),
                        modifier = Modifier
                            .align(Alignment.BottomStart)
                            .padding(16.dp)
                            .clickable { showLinksSheet = true },
                    ) {
                        Text(
                            "🔗 $grabbableLinks",
                            style = MaterialTheme.typography.labelLarge,
                            color = androidx.compose.ui.graphics.Color.Black,
                            modifier = Modifier.padding(horizontal = 14.dp, vertical = 8.dp),
                        )
                    }
                }
            }
        }

        if (showVideoSheet) {
            VideoSheet(videos, controller, onDismiss = { showVideoSheet = false })
        }

        // Bookmarks + history (plan §13 #3).
        if (showBookmarksSheet) {
            BookmarksSheet(controller, onDismiss = { showBookmarksSheet = false })
        }

        // Page-links sheet (plan §13 #9 — site grabber v1).
        if (showLinksSheet) {
            LinksSheet(links, controller, onDismiss = { showLinksSheet = false })
        }

        // Fullscreen <video>: the page's custom view over everything.
        fullscreen?.let { view ->
            key(view) {
                AndroidView(
                    factory = { ctx ->
                        FrameLayout(ctx).apply {
                            setBackgroundColor(android.graphics.Color.BLACK)
                            (view.parent as? ViewGroup)?.removeView(view)
                            addView(
                                view,
                                FrameLayout.LayoutParams(
                                    ViewGroup.LayoutParams.MATCH_PARENT,
                                    ViewGroup.LayoutParams.MATCH_PARENT,
                                ),
                            )
                        }
                    },
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
    }
}

@Composable
private fun VideoSheet(
    videos: List<BrowserController.GrabbedVideo>,
    controller: BrowserController,
    onDismiss: () -> Unit,
) {
    androidx.compose.material3.AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("${Strings.t.videosFound} (${videos.size})") },
        text = {
            Column {
                videos.take(8).forEach { v ->
                    // Desktop naming (browser_extension.py): the PAGE TITLE is
                    // the video's name; the raw URL filename says nothing.
                    val name = v.title.ifBlank {
                        v.url.substringBefore('?').substringAfterLast('/').ifBlank { v.url }
                    }
                    val host = runCatching { java.net.URL(v.url).host }.getOrDefault("")
                    val kindLabel = when (v.kind) {
                        BrowserController.GrabbedVideo.Kind.HLS -> "HLS"
                        BrowserController.GrabbedVideo.Kind.DASH -> "DASH " + Strings.t.streamFlagged
                        BrowserController.GrabbedVideo.Kind.BLOB -> Strings.t.blobFlagged
                        BrowserController.GrabbedVideo.Kind.PROGRESSIVE ->
                            v.url.substringBefore('?').substringAfterLast('.', "video").uppercase().take(6)
                    }
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                java.net.URLDecoder.decode(name, "UTF-8").take(60),
                                style = MaterialTheme.typography.bodyMedium,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                            Text(
                                listOfNotNull(
                                    kindLabel,
                                    v.quality.ifBlank { null },
                                    host.takeIf { it.isNotBlank() },
                                ).joinToString(" · "),
                                style = MaterialTheme.typography.labelSmall,
                                color = DimText,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                        if (v.kind == BrowserController.GrabbedVideo.Kind.PROGRESSIVE ||
                            v.kind == BrowserController.GrabbedVideo.Kind.HLS
                        ) {
                            androidx.compose.material3.TextButton(onClick = {
                                controller.downloadVideo(v)
                                onDismiss()
                            }) { Text(Strings.t.grabVideo) }
                        }
                    }
                }
            }
        },
        confirmButton = {
            androidx.compose.material3.TextButton(onClick = onDismiss) { Text(Strings.t.cancel) }
        },
    )
}

@Composable
private fun TabStrip(
    tabs: List<BrowserController.TabInfo>,
    activeId: Int,
    controller: BrowserController,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState())
            .padding(horizontal = 8.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        tabs.forEach { tab ->
            val selected = tab.id == activeId
            Surface(
                shape = RoundedCornerShape(8.dp),
                color = if (selected) Accent.copy(alpha = 0.25f) else Card,
                modifier = Modifier
                    .padding(end = 6.dp)
                    .clickable { controller.select(tab.id) },
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.padding(start = 10.dp, end = 2.dp),
                ) {
                    Text(
                        tab.title.ifBlank { tab.url.ifBlank { Strings.t.newTab } },
                        style = MaterialTheme.typography.labelMedium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.widthIn(max = 180.dp),
                    )
                    IconButton(
                        onClick = { controller.closeTab(tab.id) },
                        modifier = Modifier.size(32.dp),
                    ) {
                        Icon(
                            Icons.Filled.Close,
                            contentDescription = Strings.t.closeTab,
                            modifier = Modifier.size(14.dp),
                        )
                    }
                }
            }
        }
        IconButton(onClick = { controller.newTab() }, modifier = Modifier.size(36.dp)) {
            Icon(Icons.Filled.Add, contentDescription = Strings.t.newTab)
        }
    }
}

@Composable
private fun Toolbar(
    active: BrowserController.TabInfo?,
    controller: BrowserController,
    onShowBookmarks: () -> Unit,
    compact: Boolean,
) {
    val focusManager = LocalFocusManager.current
    var urlText by remember { mutableStateOf("") }
    var focused by remember { mutableStateOf(false) }
    // Mirror the page URL into the bar — but never while the user is typing.
    LaunchedEffect(active?.url, active?.id) {
        if (!focused) urlText = active?.url ?: ""
    }

    // The URL field is the primary control — on compact (portrait phone)
    // widths it gets its OWN full-width row (v1.3: squeezed to ~94dp between
    // six 48dp buttons, typing an address was practically impossible) and
    // the navigation buttons move to a row below. Wide screens keep the
    // classic single row.
    if (compact) {
        Column(modifier = Modifier.fillMaxWidth().padding(horizontal = 6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = urlText,
                    onValueChange = { urlText = it },
                    placeholder = { Text(Strings.t.urlHint) },
                    singleLine = true,
                    modifier = Modifier
                        .weight(1f)
                        .onFocusChanged { focused = it.isFocused }
                        .onPreviewKeyEvent { e ->
                            if (e.key == Key.Enter && e.type == KeyEventType.KeyDown) {
                                controller.loadInput(urlText)
                                focusManager.clearFocus()
                                true
                            } else {
                                false
                            }
                        },
                    keyboardOptions = KeyboardOptions(
                        keyboardType = KeyboardType.Uri,
                        imeAction = ImeAction.Go,
                    ),
                    keyboardActions = KeyboardActions(onGo = {
                        controller.loadInput(urlText)
                        focusManager.clearFocus()
                    }),
                )
                if (active?.loading == true) {
                    IconButton(onClick = { controller.stop() }) {
                        Icon(Icons.Filled.Close, contentDescription = Strings.t.stop)
                    }
                } else {
                    IconButton(
                        enabled = active?.url?.isNotBlank() == true,
                        onClick = { controller.reload() },
                    ) {
                        Icon(Icons.Filled.Refresh, contentDescription = Strings.t.reloadDesc)
                    }
                }
            }
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = androidx.compose.foundation.layout.Arrangement.SpaceEvenly,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                IconButton(enabled = active?.canGoBack == true, onClick = { controller.goBack() }) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = Strings.t.backDesc)
                }
                IconButton(enabled = active?.canGoForward == true, onClick = { controller.goForward() }) {
                    Icon(Icons.AutoMirrored.Filled.ArrowForward, contentDescription = Strings.t.forwardDesc)
                }
                BookmarkStar(active, controller)
                BookmarksButton(onShowBookmarks)
                IconButton(
                    enabled = active?.url?.startsWith("http") == true,
                    onClick = { controller.openInSystemBrowser() },
                ) {
                    Icon(Icons.AutoMirrored.Filled.ExitToApp, contentDescription = Strings.t.openExternal)
                }
            }
        }
    } else {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 6.dp, vertical = 2.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            IconButton(enabled = active?.canGoBack == true, onClick = { controller.goBack() }) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = Strings.t.backDesc)
            }
            IconButton(enabled = active?.canGoForward == true, onClick = { controller.goForward() }) {
                Icon(Icons.AutoMirrored.Filled.ArrowForward, contentDescription = Strings.t.forwardDesc)
            }
            if (active?.loading == true) {
                IconButton(onClick = { controller.stop() }) {
                    Icon(Icons.Filled.Close, contentDescription = Strings.t.stop)
                }
            } else {
                IconButton(enabled = active?.url?.isNotBlank() == true, onClick = { controller.reload() }) {
                    Icon(Icons.Filled.Refresh, contentDescription = Strings.t.reloadDesc)
                }
            }
            OutlinedTextField(
                value = urlText,
                onValueChange = { urlText = it },
                placeholder = { Text(Strings.t.urlHint) },
                singleLine = true,
                modifier = Modifier
                    .weight(1f)
                    .onFocusChanged { focused = it.isFocused }
                    // Hardware-keyboard devices (and automation) never show the
                    // soft IME, so its Go action is unreachable — submit on the
                    // Enter key event directly.
                    .onPreviewKeyEvent { e ->
                        if (e.key == Key.Enter && e.type == KeyEventType.KeyDown) {
                            controller.loadInput(urlText)
                            focusManager.clearFocus()
                            true
                        } else {
                            false
                        }
                    },
                keyboardOptions = KeyboardOptions(
                    keyboardType = KeyboardType.Uri,
                    imeAction = ImeAction.Go,
                ),
                keyboardActions = KeyboardActions(onGo = {
                    controller.loadInput(urlText)
                    focusManager.clearFocus()
                }),
            )
            Spacer(Modifier.width(4.dp))
            BookmarkStar(active, controller)
            BookmarksButton(onShowBookmarks)
            IconButton(
                enabled = active?.url?.startsWith("http") == true,
                onClick = { controller.openInSystemBrowser() },
            ) {
                Icon(Icons.AutoMirrored.Filled.ExitToApp, contentDescription = Strings.t.openExternal)
            }
        }
    }
}

/** Star toggle (§13 #3): ☆ → bookmark the page, ★ → remove it. */
@Composable
private fun BookmarkStar(active: BrowserController.TabInfo?, controller: BrowserController) {
    val bookmarks by controller.store.bookmarks.collectAsState()
    val bookmarked = active?.url?.startsWith("http") == true && bookmarks.any { it.url == active.url }
    IconButton(
        enabled = active?.url?.startsWith("http") == true,
        onClick = { active?.let { controller.store.toggleBookmark(it.url, it.title) } },
    ) {
        Text(
            if (bookmarked) "★" else "☆",
            style = MaterialTheme.typography.titleLarge,
            color = if (bookmarked) Accent else MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.semantics {
                contentDescription = if (bookmarked) Strings.t.unbookmark else Strings.t.bookmarkThis
            },
        )
    }
}

@Composable
private fun BookmarksButton(onShowBookmarks: () -> Unit) {
    IconButton(onClick = onShowBookmarks) {
        Text(
            "🔖",
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.semantics { contentDescription = Strings.t.bookmarksHistory },
        )
    }
}

/**
 * Bookmarks + history sheet (plan §13 #3): two chips switch the list; a
 * row opens the URL in the active tab (magnet bookmarks route to the
 * engine via loadInput); ✕ removes the single entry. History has a
 * Clear-all action; the desktop's folder tree is deliberately absent.
 */
@Composable
private fun BookmarksSheet(controller: BrowserController, onDismiss: () -> Unit) {
    val bookmarks by controller.store.bookmarks.collectAsState()
    val history by controller.store.history.collectAsState()
    var showHistory by remember { mutableStateOf(false) }

    androidx.compose.material3.AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(Strings.t.bookmarksHistory) },
        text = {
            Column {
                Row(horizontalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(8.dp)) {
                    androidx.compose.material3.FilterChip(
                        selected = !showHistory,
                        onClick = { showHistory = false },
                        label = { Text("${Strings.t.bookmarks} (${bookmarks.size})") },
                        colors = androidx.compose.material3.FilterChipDefaults.filterChipColors(
                            selectedContainerColor = Accent.copy(alpha = 0.25f),
                        ),
                    )
                    androidx.compose.material3.FilterChip(
                        selected = showHistory,
                        onClick = { showHistory = true },
                        label = { Text("${Strings.t.history} (${history.size})") },
                        colors = androidx.compose.material3.FilterChipDefaults.filterChipColors(
                            selectedContainerColor = Accent.copy(alpha = 0.25f),
                        ),
                    )
                }
                Spacer(Modifier.height(8.dp))
                val entries = if (showHistory) history.map { it.title to it.url } else bookmarks.map { it.title to it.url }
                if (entries.isEmpty()) {
                    Box(
                        modifier = Modifier.fillMaxWidth().heightIn(min = 80.dp),
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(
                            if (showHistory) Strings.t.noHistory else Strings.t.noBookmarks,
                            style = MaterialTheme.typography.bodyMedium,
                            color = DimText,
                        )
                    }
                } else {
                    Column(
                        modifier = Modifier
                            .heightIn(max = 420.dp)
                            .verticalScroll(rememberScrollState()),
                    ) {
                        entries.forEachIndexed { index, (title, url) ->
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
                            ) {
                                Column(
                                    modifier = Modifier
                                        .weight(1f)
                                        .clickable {
                                            controller.loadInput(url)
                                            onDismiss()
                                        },
                                ) {
                                    Text(
                                        title.ifBlank { url },
                                        style = MaterialTheme.typography.bodyMedium,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                    )
                                    Text(
                                        url,
                                        style = MaterialTheme.typography.labelSmall,
                                        color = DimText,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                    )
                                }
                                IconButton(
                                    onClick = {
                                        if (showHistory) controller.store.removeHistoryAt(index)
                                        else controller.store.removeBookmark(url)
                                    },
                                    modifier = Modifier.size(32.dp),
                                ) {
                                    Icon(
                                        Icons.Filled.Close,
                                        contentDescription = Strings.t.remove,
                                        modifier = Modifier.size(14.dp),
                                    )
                                }
                            }
                        }
                    }
                }
            }
        },
        confirmButton = {
            androidx.compose.material3.TextButton(onClick = onDismiss) { Text(Strings.t.cancel) }
        },
        dismissButton = {
            if (showHistory && history.isNotEmpty()) {
                androidx.compose.material3.TextButton(onClick = { controller.store.clearHistory() }) {
                    Text(Strings.t.clearHistory)
                }
            }
        },
    )
}

/**
 * Page-links sheet (plan §13 #9 — the site-grabber concept at v1):
 * torrent magnets / .torrent files first, then direct video links, then
 * page links. Torrent and video rows download with the page session;
 * page rows navigate.
 */
@Composable
private fun LinksSheet(
    links: List<BrowserController.PageLink>,
    controller: BrowserController,
    onDismiss: () -> Unit,
) {
    val torrents = links.filter { it.kind == BrowserController.PageLink.Kind.TORRENT_MAGNET || it.kind == BrowserController.PageLink.Kind.TORRENT_FILE }
    val vids = links.filter { it.kind == BrowserController.PageLink.Kind.VIDEO }
    val pages = links.filter { it.kind == BrowserController.PageLink.Kind.PAGE }
    androidx.compose.material3.AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("${Strings.t.linksFound} (${torrents.size + vids.size})") },
        text = {
            Column(
                modifier = Modifier
                    .heightIn(max = 430.dp)
                    .verticalScroll(rememberScrollState()),
            ) {
                torrents.take(12).forEach { l ->
                    LinkRow(l, controller, onDismiss)
                }
                vids.take(8).forEach { l ->
                    LinkRow(l, controller, onDismiss)
                }
                if (pages.isNotEmpty()) {
                    Spacer(Modifier.height(6.dp))
                    Text(Strings.t.pagesSection, style = MaterialTheme.typography.labelMedium, color = DimText)
                    pages.take(15).forEach { l -> LinkRow(l, controller, onDismiss) }
                }
            }
        },
        confirmButton = {
            androidx.compose.material3.TextButton(onClick = onDismiss) { Text(Strings.t.cancel) }
        },
    )
}

/**
 * Main-frame load failure (v1.3): covers the WebView's dead gray error
 * page with the app's own card — what happened, the URL, and a way out.
 */
@Composable
private fun ErrorPage(
    url: String,
    message: String,
    onRetry: () -> Unit,
    onOpenExternal: () -> Unit,
) {
    Box(
        modifier = Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background),
        contentAlignment = Alignment.Center,
    ) {
        Surface(
            shape = RoundedCornerShape(14.dp),
            color = Card,
            modifier = Modifier
                .fillMaxWidth()
                .padding(24.dp),
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier.padding(20.dp),
            ) {
                Text("⚠️", style = MaterialTheme.typography.headlineMedium)
                Spacer(Modifier.height(8.dp))
                Text(Strings.t.pageUnavailable, style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.height(6.dp))
                Text(
                    Strings.t.pageUnavailableHint,
                    style = MaterialTheme.typography.bodySmall,
                    color = DimText,
                )
                if (url.isNotBlank()) {
                    Spacer(Modifier.height(6.dp))
                    Text(
                        url,
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
                Text(
                    message,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.error,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
                Spacer(Modifier.height(12.dp))
                Row {
                    TextButton(onClick = onRetry) { Text(Strings.t.retry) }
                    TextButton(onClick = onOpenExternal) { Text(Strings.t.openExternal) }
                }
            }
        }
    }
}

@Composable
private fun LinkRow(
    l: BrowserController.PageLink,
    controller: BrowserController,
    onDismiss: () -> Unit,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                l.text.ifBlank { l.url.substringBefore('?').substringAfterLast('/') },
                style = MaterialTheme.typography.bodySmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                when (l.kind) {
                    BrowserController.PageLink.Kind.TORRENT_MAGNET -> "magnet"
                    BrowserController.PageLink.Kind.TORRENT_FILE -> ".torrent"
                    BrowserController.PageLink.Kind.VIDEO -> "video"
                    else -> l.url
                },
                style = MaterialTheme.typography.labelSmall,
                color = DimText,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        when (l.kind) {
            BrowserController.PageLink.Kind.TORRENT_MAGNET -> androidx.compose.material3.TextButton(onClick = {
                controller.addMagnetFromPage(l.url); onDismiss()
            }) { Text(Strings.t.grabVideo) }
            BrowserController.PageLink.Kind.TORRENT_FILE -> androidx.compose.material3.TextButton(onClick = {
                controller.downloadTorrentLink(l.url); onDismiss()
            }) { Text(Strings.t.grabVideo) }
            BrowserController.PageLink.Kind.VIDEO -> androidx.compose.material3.TextButton(onClick = {
                controller.downloadFileLink(l.url); onDismiss()
            }) { Text(Strings.t.grabVideo) }
            else -> androidx.compose.material3.TextButton(onClick = {
                controller.loadInput(l.url); onDismiss()
            }) { Text(Strings.t.openAction) }
        }
    }
}
