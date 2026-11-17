package space.deepflux.android.ui.iptv

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.border
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import space.deepflux.android.iptv.IPTVRepository
import space.deepflux.android.iptv.M3UParser
import space.deepflux.android.player.PlayerActivity
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.Card as CardColor
import space.deepflux.android.ui.theme.DimText
import android.content.Intent
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import kotlinx.coroutines.flow.distinctUntilChanged
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Live TV screen (v1.3 multi-playlist rework): saved, switchable playlists
 * (M3U URL / Xtream Codes) managed with add/edit/delete/rename via
 * [PlaylistEditorDialog] / [ManagePlaylistsDialog]; channels list + 2×2
 * multiview + EPG as before. Load errors show as a banner and NEVER cost a
 * saved playlist or the cached channel list.
 *
 * Artwork (1.4.2, §14): the app-scoped [ArtworkResolver] serves tri-state
 * per-cell flows (loading pulse → crossfaded image → no-art icon) with
 * visible-first priority lookups and next-screenful prefetch.
 */
@Composable
fun IPTVScreen(
    repository: IPTVRepository,
    epgRepository: space.deepflux.android.iptv.EPGRepository,
    torrentRepo: space.deepflux.android.engine.TorrentRepository,
    artwork: space.deepflux.android.iptv.ArtworkResolver,
) {
    val state by repository.state.collectAsState()
    val playlists by repository.playlists.collectAsState()
    val activeId by repository.activeId.collectAsState()
    val epgState by epgRepository.state.collectAsState()
    val context = LocalContext.current
    // Favorites (owner 2026-11-15): star state drives row/cell badges and
    // the built-in ⭐ source.
    val favs by repository.favorites.items.collectAsState()
    val favUrls = remember(favs) { favs.map { it.url }.toHashSet() }
    // Multiview (§13 #15): 2×2 grid, one audio (first filled slot).
    var multiview by remember { mutableStateOf(false) }
    val multiController = remember { space.deepflux.android.iptv.MultiViewController(context) }
    androidx.compose.runtime.DisposableEffect(multiview) {
        onDispose {
            if (multiview) multiController.releaseAll()
        }
    }
    var showEditor by remember { mutableStateOf(false) }
    // Grid/list browse mode (owner request 2026-11-15) — separate from the
    // 2×2 multiview player: a poster grid makes the covers/logos shine.
    var grid by androidx.compose.runtime.saveable.rememberSaveable { mutableStateOf(false) }
    // Category navigation (owner request 2026-11-15): a 131k-channel
    // provider list is unnavigable flat — group-title filter, computed off
    // the main thread once per loaded list.
    var category by androidx.compose.runtime.saveable.rememberSaveable { mutableStateOf("") }
    var categoryMenu by remember { mutableStateOf(false) }
    val groups by androidx.compose.runtime.produceState<List<String>>(emptyList(), state.channels) {
        value = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Default) {
            state.channels.asSequence()
                .map { it.groupId }.filter { it.isNotBlank() }
                .distinct().sorted().take(600).toList()
        }
    }
    val shown = remember(state.channels, category) {
        if (category.isBlank()) state.channels else state.channels.filter { it.groupId == category }
    }
    var editTarget by remember { mutableStateOf<space.deepflux.android.iptv.PlaylistStore.Playlist?>(null) }
    var showManage by remember { mutableStateOf(false) }
    var pickerOpen by remember { mutableStateOf(false) }

    fun savePlaylist(p: space.deepflux.android.iptv.PlaylistStore.Playlist) {
        repository.save(p)
        if (p.epgUrl.isNotBlank()) epgRepository.setEpgUrl(p.epgUrl)
        showEditor = false
        editTarget = null
    }

    /** Play one entry: SAF document / app-storage file via the loopback
     *  streamer (local sources), stream URLs in the PlayerActivity. */
    fun playChannel(channel: M3UParser.Channel) {
        when {
            channel.url.startsWith("content:") ->
                torrentRepo.playContentUri(android.net.Uri.parse(channel.url), channel.name, "video/mp4", context)
            channel.url.startsWith("/") ->
                torrentRepo.playFile(channel.url, channel.name, context)
            else -> {
                val intent = Intent(context, PlayerActivity::class.java)
                    .putExtra(PlayerActivity.EXTRA_URL, channel.url)
                    .putExtra(PlayerActivity.EXTRA_TITLE, channel.name)
                // NO forced mime (owner report 2026-11-14): PlayerActivity
                // infers the container from the URL and falls back once.
                if (channel.httpUserAgent.isNotBlank()) {
                    intent.putExtra(PlayerActivity.EXTRA_USER_AGENT, channel.httpUserAgent)
                }
                context.startActivity(intent)
            }
        }
    }

    Column(modifier = Modifier.fillMaxSize()) {
        // ---- Playlist header: switcher + manage + grid/multiview ----
        // Always visible (1.3.5): the built-in Downloads source must be
        // reachable even before any playlist is saved.
        Row(
            modifier = Modifier.fillMaxWidth().padding(start = 12.dp, end = 8.dp, top = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
                // Playlist switcher chip
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier
                        .weight(1f)
                        .clickable { pickerOpen = true },
                ) {
                    Text(
                        "📺 " + (state.playlist?.name ?: ""),
                        style = MaterialTheme.typography.labelLarge,
                        color = Accent,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Text("  ▾", style = MaterialTheme.typography.labelMedium, color = DimText)
                }
                TextButton(onClick = { showManage = true }) { Text("⚙") }
                // Category filter (playlist group-title navigation).
                if (groups.isNotEmpty()) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.clickable { categoryMenu = true },
                    ) {
                        Text(
                            "🏷 " + (category.ifBlank { Strings.t.playlistAll }),
                            style = MaterialTheme.typography.labelLarge,
                            color = Accent,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        Text("  ▾", style = MaterialTheme.typography.labelMedium, color = DimText)
                    }
                    DropdownMenu(expanded = categoryMenu, onDismissRequest = { categoryMenu = false }) {
                        DropdownMenuItem(
                            text = { Text(Strings.t.playlistAll) },
                            onClick = { category = ""; categoryMenu = false },
                        )
                        groups.forEach { g ->
                            DropdownMenuItem(
                                text = { Text(g, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                                onClick = { category = g; categoryMenu = false },
                            )
                        }
                    }
                }
                if (state.channels.isNotEmpty()) {
                    TextButton(onClick = { grid = !grid }) {
                        Text(if (grid) "☰" else "⊞", color = Accent, style = MaterialTheme.typography.titleMedium)
                    }
                }
                TextButton(onClick = {
                    val next = !multiview
                    if (!next) multiController.releaseAll()
                    multiview = next
                }) {
                    Text(
                        if (multiview) Strings.t.listView else "⊞ 2×2",
                        color = Accent,
                        style = MaterialTheme.typography.labelLarge,
                    )
                }
                DropdownMenu(expanded = pickerOpen, onDismissRequest = { pickerOpen = false }) {
                    // Built-in Downloads source + saved playlists.
                    (listOf(repository.favoritesSource, repository.downloadsSource) + playlists).forEach { p ->
                        DropdownMenuItem(
                            text = { Text("📁 " + p.name, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                            onClick = {
                                pickerOpen = false
                                repository.select(p.id)
                                if (p.epgUrl.isNotBlank()) epgRepository.setEpgUrl(p.epgUrl)
                            },
                        )
                    }
                }
        }

        // ---- First-run setup card (no playlists saved yet) ----
        if (playlists.isEmpty() && state.channels.isEmpty() && !state.loading) {
            Column(
                modifier = Modifier.fillMaxWidth().padding(14.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(Strings.t.liveTvSetup, style = MaterialTheme.typography.titleLarge)
                Spacer(Modifier.height(6.dp))
                Text(
                    Strings.t.liveTvHint,
                    style = MaterialTheme.typography.bodySmall,
                    color = DimText,
                )
                Spacer(Modifier.height(10.dp))
                Row {
                    TextButton(onClick = { editTarget = null; showEditor = true }) {
                        Text(Strings.t.addPlaylist, color = Accent)
                    }
                    TextButton(onClick = {
                        savePlaylist(
                            space.deepflux.android.iptv.PlaylistStore.Playlist(
                                id = "", name = "Macau (sample)", kind = "m3u", url = SAMPLE_PLAYLIST_URL,
                            ),
                        )
                    }) { Text(Strings.t.samplePlaylist) }
                }
            }
        }

        // ---- Load error banner (entries + cached channels stay) ----
        if (state.error.isNotBlank()) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "${Strings.t.errorPrefix}${state.error}",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.error,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                TextButton(onClick = { repository.refresh() }) { Text(Strings.t.retry) }
            }
        }

        if (multiview && state.channels.isNotEmpty()) {
            MultiViewGrid(multiController, state.channels)
        }

        // Loading / channels
        Box(modifier = Modifier.weight(1f).fillMaxWidth()) {
            when {
                state.loading && state.channels.isEmpty() -> {
                    CircularProgressIndicator(modifier = Modifier.align(Alignment.Center))
                }
                state.channels.isEmpty() && playlists.isNotEmpty() && state.error.isBlank() -> {
                    Text(
                        Strings.t.emptyFolder,
                        style = MaterialTheme.typography.bodyMedium,
                        color = DimText,
                        modifier = Modifier.align(Alignment.Center),
                    )
                }
                else -> if (!multiview && state.channels.isNotEmpty()) {
                    if (grid) {
                        // Poster grid (1.3.5): artwork-first browsing.
                        val gridState = androidx.compose.foundation.lazy.grid.rememberLazyGridState()
                        androidx.compose.foundation.lazy.grid.LazyVerticalGrid(
                            state = gridState,
                            columns = androidx.compose.foundation.lazy.grid.GridCells.Adaptive(110.dp),
                            modifier = Modifier.fillMaxSize(),
                            contentPadding = androidx.compose.foundation.layout.PaddingValues(10.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            // Dup-safe key: real playlists list the same URL
                            // under multiple names/groups — a bare url key
                            // crashes Compose on duplicates.
                            items(shown.size, key = { "${shown[it].url}|${shown[it].name}" }) { i ->
                                ChannelCell(
                                    shown[i],
                                    starred = favUrls.contains(shown[i].url),
                                    artwork = artwork,
                                    onStar = { repository.favorites.toggle(shown[i]) },
                                ) { playChannel(shown[i]) }
                            }
                        }
                        PrefetchAhead(
                            lastVisibleIndex = { gridState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: -1 },
                            items = shown,
                            artwork = artwork,
                        )
                    } else {
                        val listState = androidx.compose.foundation.lazy.rememberLazyListState()
                        LazyColumn(
                            state = listState,
                            modifier = Modifier.fillMaxSize(),
                            contentPadding = androidx.compose.foundation.layout.PaddingValues(
                                horizontal = 10.dp, vertical = 6.dp
                            ),
                            verticalArrangement = Arrangement.spacedBy(6.dp),
                        ) {
                            items(shown, key = { "${it.url}|${it.name}" }) { channel ->
                                ChannelRow(
                                    channel,
                                    epgState,
                                    starred = favUrls.contains(channel.url),
                                    artwork = artwork,
                                    onStar = { repository.favorites.toggle(channel) },
                                ) { playChannel(channel) }
                            }
                        }
                        PrefetchAhead(
                            lastVisibleIndex = { listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: -1 },
                            items = shown,
                            artwork = artwork,
                        )
                    }
                }
            }
        }
    }

    // ---- Playlist dialogs ----
    if (showEditor) {
        PlaylistEditorDialog(
            initial = editTarget,
            onSave = { savePlaylist(it) },
            onDismiss = { showEditor = false; editTarget = null },
        )
    }
    if (showManage) {
        ManagePlaylistsDialog(
            playlists = playlists,
            activeId = activeId,
            onEdit = {
                showManage = false
                editTarget = it
                showEditor = true
            },
            onAdd = {
                showManage = false
                editTarget = null
                showEditor = true
            },
            onRemove = { repository.remove(it.id) },
            onDismiss = { showManage = false },
        )
    }
}

/**
 * Artwork state for one entry via the app-scoped resolver (§14): the
 * playlist's own logo wins instantly; everything else resolves through the
 * shared priority pipeline (iptv-org logos / TMDb-TVmaze-TPDB-StashDB
 * covers). Tri-state so the cell can tell "resolving" from "no artwork".
 */
@Composable
private fun artworkState(
    artwork: space.deepflux.android.iptv.ArtworkResolver,
    channel: M3UParser.Channel,
    isLocal: Boolean,
): space.deepflux.android.iptv.ArtworkResolver.State {
    if (channel.logoUrl.isNotBlank()) {
        // Playlist-provided logo: no resolver trip (desktop parity).
        return space.deepflux.android.iptv.ArtworkResolver.State.Ready(channel.logoUrl)
    }
    val state by artwork.flowFor(channel, isLocal).collectAsState()
    return state
}

/** True for SAF documents / app-storage files (the local-files lanes). */
private fun isLocalChannel(channel: M3UParser.Channel): Boolean =
    channel.url.startsWith("content:") || channel.url.startsWith("/")

/**
 * Warm the next screenful's lookups at LOW priority (§14 P3): composed
 * cells already resolve at high priority; this closes the gap so artwork
 * is more often ready when the user scrolls on.
 */
@Composable
private fun PrefetchAhead(
    lastVisibleIndex: () -> Int,
    items: List<M3UParser.Channel>,
    artwork: space.deepflux.android.iptv.ArtworkResolver,
) {
    LaunchedEffect(items, artwork) {
        androidx.compose.runtime.snapshotFlow { lastVisibleIndex() }
            .distinctUntilChanged()
            .collect { last ->
                if (last < 0) return@collect
                val upto = minOf(last + PREFETCH_AHEAD, items.lastIndex)
                for (i in last + 1..upto) {
                    val ch = items[i]
                    // Entries with a playlist logo never touch the resolver.
                    if (ch.logoUrl.isBlank()) {
                        artwork.flowFor(ch, isLocalChannel(ch), lowPriority = true)
                    }
                }
            }
    }
}

/** One poster-grid cell: artwork on top, name + group under it. */
@Composable
private fun ChannelCell(
    channel: M3UParser.Channel,
    starred: Boolean,
    artwork: space.deepflux.android.iptv.ArtworkResolver,
    onStar: () -> Unit,
    onClick: () -> Unit,
) {
    val isLocal = isLocalChannel(channel)
    val art = artworkState(artwork, channel, isLocal)
    val context = LocalContext.current
    // Dead artwork URLs (Coil error) fall back to the no-art icon.
    var failedUrl by remember { mutableStateOf("") }
    // TV focus ring (D-pad navigation): a visible border when focused.
    var focused by remember { mutableStateOf(false) }
    Card(
        colors = CardDefaults.cardColors(containerColor = CardColor),
        modifier = Modifier
            .fillMaxWidth()
            .onFocusChanged { focused = it.isFocused }
            .border(
                width = if (focused) 2.dp else 0.dp,
                color = if (focused) Accent else androidx.compose.ui.graphics.Color.Transparent,
                shape = androidx.compose.foundation.shape.RoundedCornerShape(10.dp),
            )
            .clickable(onClick = onClick),
    ) {
        Column {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .aspectRatio(1f)
                    .background(androidx.compose.ui.graphics.Color(12, 14, 22)),
                contentAlignment = Alignment.Center,
            ) {
                when (val st = art) {
                    space.deepflux.android.iptv.ArtworkResolver.State.Loading ->
                        PulsingArt(Modifier.fillMaxSize())
                    is space.deepflux.android.iptv.ArtworkResolver.State.Ready ->
                        if (failedUrl == st.url) {
                            Icon(
                                Icons.AutoMirrored.Filled.List,
                                contentDescription = null,
                                tint = Accent.copy(alpha = 0.5f),
                                modifier = Modifier.size(30.dp),
                            )
                        } else {
                            coil.compose.AsyncImage(
                                model = coil.request.ImageRequest.Builder(context)
                                    .data(st.url)
                                    .crossfade(220)
                                    .build(),
                                contentDescription = channel.name,
                                contentScale = androidx.compose.ui.layout.ContentScale.Fit,
                                onError = { failedUrl = st.url },
                                modifier = Modifier.fillMaxSize(),
                            )
                        }
                    is space.deepflux.android.iptv.ArtworkResolver.State.Empty ->
                        Icon(
                            Icons.AutoMirrored.Filled.List,
                            contentDescription = null,
                            tint = Accent.copy(alpha = 0.5f),
                            modifier = Modifier.size(30.dp),
                        )
                }
                // Star badge (top-right corner of the poster).
                Text(
                    if (starred) "★" else "☆",
                    style = MaterialTheme.typography.titleMedium,
                    color = if (starred) androidx.compose.ui.graphics.Color(0xFFFFD24A) else androidx.compose.ui.graphics.Color.White,
                    modifier = Modifier
                        .align(Alignment.TopEnd)
                        .background(androidx.compose.ui.graphics.Color(0x66000000))
                        .clickable(onClick = onStar)
                        .padding(horizontal = 6.dp, vertical = 2.dp),
                )
            }
            Column(modifier = Modifier.padding(horizontal = 8.dp, vertical = 6.dp)) {
                Text(
                    channel.name,
                    style = MaterialTheme.typography.labelMedium,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.heightIn(min = 32.dp),
                )
                if (channel.groupId.isNotBlank()) {
                    Text(
                        channel.groupId,
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
        }
    }
}

@Composable
private fun ChannelRow(
    channel: M3UParser.Channel,
    epgState: space.deepflux.android.iptv.EPGParser.EPGState,
    starred: Boolean,
    artwork: space.deepflux.android.iptv.ArtworkResolver,
    onStar: () -> Unit,
    onClick: () -> Unit,
) {
    val isLocal = isLocalChannel(channel)
    val art = artworkState(artwork, channel, isLocal)
    val context = LocalContext.current
    var failedUrl by remember { mutableStateOf("") }
    var focused by remember { mutableStateOf(false) }

    Card(
        colors = CardDefaults.cardColors(containerColor = CardColor),
        modifier = Modifier
            .fillMaxWidth()
            .onFocusChanged { focused = it.isFocused }
            .border(
                width = if (focused) 2.dp else 0.dp,
                color = if (focused) Accent else androidx.compose.ui.graphics.Color.Transparent,
                shape = androidx.compose.foundation.shape.RoundedCornerShape(10.dp),
            )
            .clickable(onClick = onClick),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            when (val st = art) {
                space.deepflux.android.iptv.ArtworkResolver.State.Loading -> PulsingArt(
                    Modifier
                        .size(width = 48.dp, height = 36.dp)
                        .clip(androidx.compose.foundation.shape.RoundedCornerShape(6.dp)),
                )
                is space.deepflux.android.iptv.ArtworkResolver.State.Ready ->
                    if (failedUrl == st.url) {
                        RowFallbackIcon()
                    } else {
                        coil.compose.AsyncImage(
                            model = coil.request.ImageRequest.Builder(context)
                                .data(st.url)
                                .crossfade(220)
                                .build(),
                            contentDescription = null,
                            contentScale = androidx.compose.ui.layout.ContentScale.Fit,
                            onError = { failedUrl = st.url },
                            modifier = Modifier
                                .size(width = 48.dp, height = 36.dp)
                                .clip(androidx.compose.foundation.shape.RoundedCornerShape(6.dp))
                                .background(CardColor),
                        )
                    }
                is space.deepflux.android.iptv.ArtworkResolver.State.Empty -> RowFallbackIcon()
            }
            Spacer(Modifier.size(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    channel.name,
                    style = MaterialTheme.typography.bodyLarge,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                if (channel.groupId.isNotBlank()) {
                    Text(
                        channel.groupId,
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText,
                        maxLines = 1,
                    )
                }
                // EPG "now playing" line (v0.9)
                val now = channel.let { ch ->
                    space.deepflux.android.iptv.EPGParser.nowAndNext(
                        epgState, ch.name
                    ).first
                }
                if (now != null) {
                    Text(
                        "▶ " + now.title + " (" + formatTime(now.startTime) + "-" + formatTime(now.endTime) + ")",
                        style = MaterialTheme.typography.labelSmall,
                        color = Accent,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
            // Star toggle at the row end.
            Text(
                if (starred) "★" else "☆",
                style = MaterialTheme.typography.titleLarge,
                color = if (starred) androidx.compose.ui.graphics.Color(0xFFFFD24A) else DimText,
                modifier = Modifier
                    .clickable(onClick = onStar)
                    .padding(horizontal = 8.dp, vertical = 8.dp),
            )
        }
    }
}

private fun formatTime(millis: Long): String =
    SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(millis))

/** How many items past the last visible one the prefetch lane warms. */
private const val PREFETCH_AHEAD = 10

/** "Resolving" placeholder (§14 P3): a quiet pulse on the panel-blue fill. */
@Composable
private fun PulsingArt(modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "artPulse")
    val alpha by transition.animateFloat(
        initialValue = 0.30f,
        targetValue = 0.65f,
        animationSpec = infiniteRepeatable(
            animation = tween(850),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "artPulseAlpha",
    )
    Box(
        modifier = modifier.background(
            androidx.compose.ui.graphics.Color(26, 42, 74).copy(alpha = alpha),
        ),
    )
}

/** The row-mode no-artwork fallback (unchanged look from 1.3.5). */
@Composable
private fun RowFallbackIcon() {
    Box(
        modifier = Modifier
            .size(36.dp)
            .background(
                Accent.copy(alpha = 0.2f),
                CircleShape,
            ),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            Icons.AutoMirrored.Filled.List,
            contentDescription = null,
            tint = Accent,
            modifier = Modifier.size(18.dp),
        )
    }
}

/**
 * 2×2 multiview grid (§13 #15): tap an empty slot to pick a channel,
 * tap a filled tile to open it full-screen, long-press to clear it.
 * Exactly one tile is audible (first filled — see MultiViewController).
 */
@Composable
private fun MultiViewGrid(
    controller: space.deepflux.android.iptv.MultiViewController,
    channels: List<space.deepflux.android.iptv.M3UParser.Channel>,
) {
    var pickerSlot by remember { mutableStateOf(-1) }
    // Recompose trigger when a slot changes.
    var slotsVersion by remember { mutableStateOf(0) }
    val ctx = androidx.compose.ui.platform.LocalContext.current

    // slotsVersion READ here: a bare write to the counter invalidates
    // nothing (Compose invalidation is reader-based) — the long-press
    // clear path used to leave the cleared tile on screen.
    androidx.compose.runtime.key(slotsVersion) {}
    Column(modifier = Modifier.fillMaxSize().padding(4.dp)) {
        for (row in 0..1) {
            Row(modifier = Modifier.weight(1f).fillMaxWidth()) {
                for (col in 0..1) {
                    val slot = row * 2 + col
                    Box(
                        modifier = Modifier
                            .weight(1f)
                            .fillMaxSize()
                            .padding(2.dp),
                    ) {
                        val url = controller.urls[slot]
                        if (url == null) {
                            androidx.compose.material3.Surface(
                                color = CardColor,
                                shape = androidx.compose.foundation.shape.RoundedCornerShape(8.dp),
                                modifier = Modifier.fillMaxSize(),
                            ) {
                                Box(
                                    modifier = Modifier.fillMaxSize(),
                                    contentAlignment = Alignment.Center,
                                ) {
                                    Text(
                                        "＋",
                                        style = MaterialTheme.typography.titleLarge,
                                        color = Accent,
                                    )
                                }
                            }
                            Box(
                                modifier = Modifier.fillMaxSize().clickable { pickerSlot = slot },
                            )
                        } else {
                            androidx.compose.ui.viewinterop.AndroidView(
                                factory = { ctx ->
                                    androidx.media3.ui.PlayerView(ctx).apply {
                                        useController = false
                                        player = controller.playerFor(slot)
                                    }
                                },
                                modifier = Modifier.fillMaxSize(),
                            )
                            Box(
                                modifier = Modifier
                                    .fillMaxSize()
                                    .combinedClickable(
                                        onClick = {
                                            // Full-screen this one stream.
                                            val intent = Intent(ctx, PlayerActivity::class.java)
                                                .putExtra(PlayerActivity.EXTRA_URL, url)
                                                .putExtra(PlayerActivity.EXTRA_TITLE, controller.names[slot] ?: "")
                                            if (controller.userAgents[slot]?.isNotBlank() == true) {
                                                intent.putExtra(PlayerActivity.EXTRA_USER_AGENT, controller.userAgents[slot])
                                            }
                                            ctx.startActivity(intent)
                                        },
                                        onLongClick = {
                                            controller.setSlot(slot, null, null)
                                            slotsVersion++
                                        },
                                    ),
                            )
                            Text(
                                controller.names[slot] ?: "",
                                style = MaterialTheme.typography.labelSmall,
                                color = androidx.compose.ui.graphics.Color.White,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                modifier = Modifier
                                    .align(Alignment.TopStart)
                                    .background(androidx.compose.ui.graphics.Color(0x88000000.toInt()))
                                    .padding(horizontal = 6.dp, vertical = 2.dp),
                            )
                        }
                    }
                }
            }
        }
    }

    if (pickerSlot >= 0) {
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { pickerSlot = -1 },
            title = { Text(Strings.t.liveTv) },
            text = {
                LazyColumn(
                    modifier = Modifier.fillMaxWidth().heightIn(max = 420.dp),
                ) {
                    items(channels, key = { "${it.url}|${it.name}" }) { channel ->
                        Text(
                            channel.name,
                            style = MaterialTheme.typography.bodyMedium,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                            modifier = Modifier
                                .fillMaxWidth()
                                .clickable {
                                    controller.setSlot(pickerSlot, channel.url, channel.name, channel.httpUserAgent.ifBlank { null })
                                    pickerSlot = -1
                                    slotsVersion++
                                }
                                .padding(vertical = 6.dp),
                        )
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = { pickerSlot = -1 }) { Text(Strings.t.cancel) }
            },
        )
    }
}
