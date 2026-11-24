package space.deepflux.android.ui.downloads

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import java.io.File
import kotlinx.coroutines.launch
import space.deepflux.android.dlmgr.HttpDownload
import space.deepflux.android.dlmgr.HttpDownloadRepository
import space.deepflux.android.dlmgr.HttpState
import space.deepflux.android.engine.MagnetParser
import space.deepflux.android.engine.TorrentItem
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.R
import space.deepflux.android.engine.TorrentState
import space.deepflux.android.engine.isActive
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.AccentData
import space.deepflux.android.ui.theme.DimText
import space.deepflux.android.ui.theme.ErrorRed
import space.deepflux.android.ui.theme.OkGreen
import space.deepflux.android.ui.theme.WarnYellow
import space.deepflux.android.ui.Strings
import space.deepflux.android.util.Fmt
import space.deepflux.android.util.Playables

@Composable
fun DownloadsScreen(
    torrents: List<TorrentItem>,
    repository: TorrentRepository,
    httpDownloads: List<HttpDownload>,
    httpRepo: HttpDownloadRepository,
    rss: space.deepflux.android.rss.RssRepository,
    isWide: Boolean = false,
    /** Called whenever "is something waiting for Resume all?" may have changed (keeps the paused notice in step). */
    onPauseStateChanged: () -> Unit = {},
) {
    var showAddDialog by remember { mutableStateOf(false) }
    // rememberSaveable: the selected torrent survives a tab switch (the tab
    // used to come back with the detail pane empty on tablets).
    var selectedHash by rememberSaveable { mutableStateOf<String?>(null) }
    // Destructive taps ask first (fix 2026-11-23): the remove button sits right
    // next to Play on a touch target, and on an unfinished download one tap
    // discarded the partial file with no undo.
    var confirmRemoveHttp by remember { mutableStateOf<HttpDownload?>(null) }
    var confirmRemoveTorrent by remember { mutableStateOf<TorrentItem?>(null) }
    // 0 = torrents, 1 = files, 2 = RSS (plan §13 #4).
    var section by rememberSaveable { mutableStateOf(0) }
    val selected = torrents.find { it.infoHash == selectedHash }
    // Collected (not .value-read) so the chip count follows feed adds/removes.
    val feedCount = rss.feeds.collectAsState().value.size
    // "Pause all" / "Resume all" for BOTH lanes — the same pair the notification
    // carries; it used to be reachable only from there.
    val torrentsWaitForResume by repository.pausedByPauseAll.collectAsState()
    val filesWaitForResume = httpDownloads.any { it.pauseAll }
    val anythingToPause = torrents.any { it.isActive } ||
        httpDownloads.any { it.state == HttpState.RUNNING || it.state == HttpState.QUEUED }
    LaunchedEffect(torrentsWaitForResume, filesWaitForResume) { onPauseStateChanged() }

    Scaffold(
        floatingActionButton = {
            ExtendedFloatingActionButton(
                onClick = { showAddDialog = true },
                icon = { Icon(Icons.Filled.Add, contentDescription = null) },
                text = { Text(Strings.t.addDownload) },
            )
        },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            // Section switch: Torrents | Files | RSS — the download lanes
            // plus the watched feeds.
            Row(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                SectionChip(
                    label = "${Strings.t.torrentsSection} (${torrents.size})",
                    selected = section == 0,
                    onClick = { section = 0 },
                )
                SectionChip(
                    label = "${Strings.t.filesSection} (${httpDownloads.size})",
                    selected = section == 1,
                    onClick = { section = 1 },
                )
                SectionChip(
                    label = "${Strings.t.rssSection} ($feedCount)",
                    selected = section == 2,
                    onClick = { section = 2 },
                )
                Spacer(Modifier.weight(1f))
                // "Resume all" while something waits for it, else "Pause all"
                // while anything runs.
                if (torrentsWaitForResume || filesWaitForResume) {
                    TextButton(
                        onClick = {
                            repository.resumeAll()
                            httpRepo.resumeAll()
                        },
                        contentPadding = PaddingValues(horizontal = 8.dp),
                    ) { Text(Strings.t.resumeAll, maxLines = 1) }
                } else if (anythingToPause) {
                    TextButton(
                        onClick = {
                            repository.pauseAll()
                            httpRepo.pauseAll()
                        },
                        contentPadding = PaddingValues(horizontal = 8.dp),
                    ) { Text(Strings.t.pauseAll, maxLines = 1) }
                }
            }
            Box(modifier = Modifier.weight(1f)) {
                if (section == 2) {
                    RssSection(rss)
                } else if (section == 1) {
                    if (httpDownloads.isEmpty()) {
                        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                Text(Strings.t.noFiles, style = MaterialTheme.typography.titleMedium)
                                Spacer(Modifier.height(6.dp))
                                Text(
                                    Strings.t.noFilesHint,
                                    style = MaterialTheme.typography.bodyMedium,
                                    color = DimText,
                                    modifier = Modifier.padding(horizontal = 32.dp),
                                )
                            }
                        }
                    } else {
                        HttpDownloadList(httpDownloads, httpRepo, repository) { confirmRemoveHttp = it }
                    }
                } else if (isWide && selected != null) {
                    // Two-pane: list (weight 1) + detail (weight 1.2)
                    Row(modifier = Modifier.fillMaxSize()) {
                        Box(modifier = Modifier.weight(1f)) {
                            TorrentList(
                                torrents, repository, selectedHash,
                                onSelect = { selectedHash = it },
                                onRemove = { confirmRemoveTorrent = it },
                            )
                        }
                        TorrentDetail(
                            selected, repository,
                            modifier = Modifier.weight(1.2f),
                            onPauseResume = {
                                if (selected.state == TorrentState.PAUSED) repository.resume(selected.infoHash)
                                else repository.pause(selected.infoHash)
                            },
                            onRemove = { confirmRemoveTorrent = selected },
                        )
                    }
                } else if (torrents.isEmpty()) {
                    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(Strings.t.noDownloads, style = MaterialTheme.typography.titleMedium)
                            Spacer(Modifier.height(6.dp))
                            Text(
                                Strings.t.noDownloadsHint,
                                style = MaterialTheme.typography.bodyMedium,
                                color = DimText,
                                modifier = Modifier.padding(horizontal = 32.dp),
                            )
                        }
                    }
                } else {
                    TorrentList(torrents, repository, null, null, onRemove = { confirmRemoveTorrent = it })
                }
            }
        }
    }

    if (showAddDialog) {
        AddMagnetDialog(
            sequentialDefault = repository.prefs.value.sequentialDefault,
            onAdd = { target, sequential ->
                // Route by shape: magnet → torrent engine, http(s) → the
                // plain-HTTP download engine (Phase A1). Both lanes share
                // the same list surface above.
                if (target.startsWith("http://", true) || target.startsWith("https://", true)) {
                    httpRepo.add(target)
                } else {
                    repository.addMagnet(target, sequentialOverride = sequential)
                }
                showAddDialog = false
            },
            onDismiss = { showAddDialog = false },
        )
    }

    confirmRemoveHttp?.let { item ->
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { confirmRemoveHttp = null },
            title = { Text(Strings.t.removeDownloadTitle) },
            text = {
                Text(if (item.state == HttpState.DONE) Strings.t.removeDownloadKeepFile else Strings.t.removeDownloadPartial)
            },
            confirmButton = {
                androidx.compose.material3.TextButton(onClick = {
                    httpRepo.remove(item.id)
                    confirmRemoveHttp = null
                }) { Text(Strings.t.remove, color = ErrorRed) }
            },
            dismissButton = {
                androidx.compose.material3.TextButton(onClick = { confirmRemoveHttp = null }) { Text(Strings.t.cancel) }
            },
        )
    }
    confirmRemoveTorrent?.let { torrent ->
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { confirmRemoveTorrent = null },
            title = { Text(Strings.t.remove) },
            text = {
                Text(
                    Strings.t.removeTorrentFmt.format(torrent.name.ifBlank { torrent.infoHash.take(12) }),
                    style = MaterialTheme.typography.bodyMedium,
                )
            },
            confirmButton = {
                androidx.compose.material3.TextButton(onClick = {
                    repository.remove(torrent.infoHash)
                    if (selectedHash == torrent.infoHash) selectedHash = null
                    confirmRemoveTorrent = null
                }) { Text(Strings.t.remove, color = ErrorRed) }
            },
            dismissButton = {
                androidx.compose.material3.TextButton(onClick = { confirmRemoveTorrent = null }) { Text(Strings.t.cancel) }
            },
        )
    }
}

@Composable
private fun SectionChip(label: String, selected: Boolean, onClick: () -> Unit) {
    androidx.compose.material3.FilterChip(
        selected = selected,
        onClick = onClick,
        label = { Text(label, style = MaterialTheme.typography.labelMedium) },
        colors = androidx.compose.material3.FilterChipDefaults.filterChipColors(
            selectedContainerColor = Accent.copy(alpha = 0.25f),
            selectedLabelColor = androidx.compose.ui.graphics.Color.Unspecified,
        ),
    )
}

@Composable
private fun HttpDownloadList(
    items: List<HttpDownload>,
    repo: HttpDownloadRepository,
    torrentRepo: TorrentRepository,
    onRemove: (HttpDownload) -> Unit,
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        // Room below the last card for the floating "Add download" button: it
        // covers the bottom of the list, and the last card's buttons sat under
        // it for good — the video's Play among them (emulator, 2026-11-25).
        contentPadding = PaddingValues(start = 12.dp, top = 12.dp, end = 12.dp, bottom = 96.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        items(items, key = { it.id }) { item ->
            HttpCard(item, repo, torrentRepo, onRemove)
        }
    }
}

@Composable
private fun HttpCard(
    item: HttpDownload,
    repo: HttpDownloadRepository,
    torrentRepo: TorrentRepository,
    onRemove: (HttpDownload) -> Unit,
) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    val opener = androidx.compose.runtime.remember(ctx) {
        space.deepflux.android.filecmd.DownloadOpener(ctx.applicationContext ?: ctx)
    }
    // A finished download opens in one tap — played by the app itself when it
    // can, else in whatever app the user picks. It used to have no way to be
    // opened from here unless it was a video.
    val play = {
        torrentRepo.playFile(File(torrentRepo.downloadsDir, item.fileName).absolutePath, item.fileName, ctx)
    }
    val openFinished = {
        if (!opener.open(File(torrentRepo.downloadsDir, item.fileName))) {
            torrentRepo.postNotice(Strings.t.noticeCannotOpenItem)
        }
    }
    val finished = item.state == HttpState.DONE
    val playable = Playables.isPlayable(item.fileName)
    Card(
        modifier = Modifier.fillMaxWidth().clickable(enabled = finished) { if (playable) play() else openFinished() },
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
    ) {
        Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
            Text(
                item.fileName,
                style = MaterialTheme.typography.titleSmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(8.dp))
            if (item.hasProgress) {
                LinearProgressIndicator(
                    progress = { item.progress },
                    modifier = Modifier.fillMaxWidth().height(6.dp),
                )
            } else if (item.state == HttpState.RUNNING) {
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth().height(6.dp))
            }
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    httpStateLabel(item.state),
                    style = MaterialTheme.typography.labelMedium,
                    color = httpStateColor(item.state),
                )
                Spacer(Modifier.width(10.dp))
                Text(
                    when {
                        item.totalBytes > 0 ->
                            "${Fmt.bytes(item.doneBytes)} / ${Fmt.bytes(item.totalBytes)} · ${Fmt.percent(item.progress)}"
                        // HLS: the final size is unknown until the last segment.
                        item.segmentsTotal > 0 ->
                            "${Fmt.bytes(item.doneBytes)} · ${Fmt.percent(item.progress)}"
                        else -> Fmt.bytes(item.doneBytes)
                    },
                    style = MaterialTheme.typography.labelMedium,
                    color = DimText,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                if (item.state == HttpState.RUNNING || item.state == HttpState.QUEUED) {
                    Text(
                        "↓ ${Fmt.speed(item.speed)}",
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText,
                    )
                }
            }
            Spacer(Modifier.height(4.dp))
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                Text(
                    item.url,
                    style = MaterialTheme.typography.labelSmall,
                    color = DimText,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                if (finished && playable) {
                    // As before 1.4.14: a finished video keeps its Play button (the row plays too).
                    IconButton(onClick = { play() }) {
                        Icon(Icons.Filled.PlayArrow, contentDescription = Strings.t.play, tint = OkGreen)
                    }
                } else if (finished) {
                    IconButton(onClick = openFinished) {
                        Icon(
                            androidx.compose.ui.res.painterResource(R.drawable.ic_open),
                            contentDescription = Strings.t.openAction,
                            tint = OkGreen,
                        )
                    }
                } else if ((item.state == HttpState.RUNNING || item.state == HttpState.QUEUED) &&
                    Playables.isPlayable(item.fileName)
                ) {
                    // Stream-while-downloading: the loopback server follows
                    // the growing .part (HLS .ts concatenations stream
                    // cleanly; MP4 plays only when its moov atom is at the
                    // front — the player says so if not).
                    IconButton(onClick = {
                        torrentRepo.playFile(
                            File(torrentRepo.downloadsDir, item.fileName + ".part").absolutePath,
                            item.fileName,
                            ctx,
                        )
                    }) {
                        Icon(Icons.Filled.PlayArrow, contentDescription = Strings.t.play, tint = OkGreen)
                    }
                }
                if (item.state == HttpState.PAUSED || item.state == HttpState.ERROR) {
                    IconButton(onClick = { repo.resume(item.id) }) {
                        Icon(Icons.Filled.PlayArrow, contentDescription = Strings.t.resume, tint = Accent)
                    }
                } else if (item.state == HttpState.RUNNING || item.state == HttpState.QUEUED) {
                    IconButton(
                        onClick = { repo.pause(item.id) },
                        modifier = Modifier.semantics {
                            contentDescription = Strings.t.pause
                        },
                    ) {
                        PauseBars(tint = MaterialTheme.colorScheme.onSurface)
                    }
                }
                IconButton(onClick = { onRemove(item) }) {
                    Icon(Icons.Filled.Delete, contentDescription = Strings.t.remove, tint = ErrorRed)
                }
            }
            if (item.error.isNotBlank()) {
                Text(
                    item.error,
                    style = MaterialTheme.typography.labelSmall,
                    color = ErrorRed,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

// Playability now lives in one place: util/Playables.kt (polish pass 1, P4).

private fun httpStateLabel(state: HttpState): String = when (state) {
    HttpState.QUEUED -> Strings.t.queued
    HttpState.RUNNING -> Strings.t.downloading
    HttpState.PAUSED -> Strings.t.paused
    HttpState.DONE -> Strings.t.done
    HttpState.ERROR -> Strings.t.error
}

private fun httpStateColor(state: HttpState): Color = when (state) {
    HttpState.QUEUED -> DimText
    HttpState.RUNNING -> AccentData
    HttpState.PAUSED -> WarnYellow
    HttpState.DONE -> OkGreen
    HttpState.ERROR -> ErrorRed
}

@Composable
private fun TorrentCard(
    torrent: TorrentItem,
    repository: TorrentRepository,
    onRemove: (TorrentItem) -> Unit,
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
    ) {
        Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
            Text(
                torrent.name,
                style = MaterialTheme.typography.titleSmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(8.dp))
            LinearProgressIndicator(
                progress = { torrent.progress.coerceIn(0f, 1f) },
                modifier = Modifier.fillMaxWidth().height(6.dp),
            )
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    stateLabel(torrent.state),
                    style = MaterialTheme.typography.labelMedium,
                    color = stateColor(torrent.state),
                )
                Spacer(Modifier.width(10.dp))
                Text(
                    "${Fmt.bytes(torrent.doneBytes)} / ${Fmt.bytes(torrent.totalBytes)} · ${Fmt.percent(torrent.progress)}",
                    style = MaterialTheme.typography.labelMedium,
                    color = DimText,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            Spacer(Modifier.height(4.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "↓ ${Fmt.speed(torrent.downloadRate)}   ↑ ${Fmt.speed(torrent.uploadRate)}   " +
                        Strings.t.seedsPeersFmt.format(torrent.seeds, torrent.peers),
                    style = MaterialTheme.typography.labelSmall,
                    color = DimText,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                if (torrent.firstVideoPath.isNotBlank()) {
                    val ctx = androidx.compose.ui.platform.LocalContext.current
                    IconButton(onClick = { repository.playVideo(torrent, ctx) }) {
                        Icon(Icons.Filled.PlayArrow, contentDescription = Strings.t.play, tint = OkGreen)
                    }
                }
                if (torrent.state == TorrentState.PAUSED) {
                    IconButton(onClick = { repository.resume(torrent.infoHash) }) {
                        Icon(Icons.Filled.PlayArrow, contentDescription = Strings.t.resume, tint = Accent)
                    }
                } else {
                    IconButton(onClick = { repository.pause(torrent.infoHash) }) {
                        PauseBars(tint = MaterialTheme.colorScheme.onSurface)
                    }
                }
                IconButton(onClick = { onRemove(torrent) }) {
                    Icon(Icons.Filled.Delete, contentDescription = Strings.t.remove, tint = ErrorRed)
                }
            }
            if (torrent.errorMessage.isNotBlank()) {
                Text(
                    torrent.errorMessage,
                    style = MaterialTheme.typography.labelSmall,
                    color = ErrorRed,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

/** Pause glyph assembled from primitives — the core material icon set has no Pause. */
@Composable
private fun PauseBars(tint: Color) {
    Row(
        horizontalArrangement = Arrangement.spacedBy(3.dp),
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.size(width = 24.dp, height = 24.dp),
    ) {
        Box(Modifier.width(5.dp).height(14.dp).background(tint, RoundedCornerShape(1.dp)))
        Box(Modifier.width(5.dp).height(14.dp).background(tint, RoundedCornerShape(1.dp)))
    }
}

@Composable
private fun AddMagnetDialog(
    sequentialDefault: Boolean,
    onAdd: (magnet: String, sequential: Boolean) -> Unit,
    onDismiss: () -> Unit,
) {
    var text by rememberSaveable { mutableStateOf("") }
    var sequential by rememberSaveable { mutableStateOf(sequentialDefault) }
    // A valid magnet OR an http(s) URL — the dialog feeds both download lanes.
    val valid = remember(text) {
        text.startsWith("http://", true) || text.startsWith("https://", true) ||
            MagnetParser.infoHashHex(MagnetParser.extractMagnet(text) ?: text) != null
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(Strings.t.addMagnetTitle) },
        text = {
            Column {
                OutlinedTextField(
                    value = text,
                    onValueChange = { text = it },
                    label = { Text(Strings.t.magnetPlaceholder) },
                    singleLine = true,
                    isError = text.isNotBlank() && !valid,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = sequential, onCheckedChange = { sequential = it })
                    Text(Strings.t.sequential, style = MaterialTheme.typography.bodyMedium)
                }
                if (text.isNotBlank() && !valid) {
                    Text(
                        Strings.t.invalidTarget,
                        style = MaterialTheme.typography.labelSmall,
                        color = ErrorRed,
                    )
                }
            }
        },
        confirmButton = {
            TextButton(enabled = valid, onClick = { onAdd(text.trim(), sequential) }) {
                Text(Strings.t.add)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text(Strings.t.cancel) }
        },
    )
}

private fun stateLabel(state: TorrentState): String = when (state) {
    TorrentState.CHECKING -> Strings.t.checking
    TorrentState.METADATA -> Strings.t.fetchingInfo
    TorrentState.DOWNLOADING -> Strings.t.downloading
    TorrentState.FINISHED -> Strings.t.done
    TorrentState.SEEDING -> Strings.t.seeding
    TorrentState.PAUSED -> Strings.t.paused
    TorrentState.ERROR -> Strings.t.error
    TorrentState.UNKNOWN -> "—"
}

private fun stateColor(state: TorrentState): Color = when (state) {
    TorrentState.DOWNLOADING -> AccentData
    TorrentState.METADATA, TorrentState.CHECKING -> DimText
    TorrentState.FINISHED, TorrentState.SEEDING -> OkGreen
    TorrentState.PAUSED -> WarnYellow
    TorrentState.ERROR -> ErrorRed
    TorrentState.UNKNOWN -> DimText
}

@Composable
private fun TorrentList(
    torrents: List<TorrentItem>,
    repository: TorrentRepository,
    selectedHash: String?,
    onSelect: ((String) -> Unit)? = null,
    onRemove: (TorrentItem) -> Unit = {},
    modifier: Modifier = Modifier,
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize().then(modifier),
        // Room below the last card for the floating "Add download" button: it
        // covers the bottom of the list, and the last card's buttons sat under
        // it for good — the video's Play among them (emulator, 2026-11-25).
        contentPadding = PaddingValues(start = 12.dp, top = 12.dp, end = 12.dp, bottom = 96.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        items(torrents, key = { "${it.infoHash}-${it.addedAt}" }) { torrent ->
            if (onSelect != null) {
                Card(
                    colors = CardDefaults.cardColors(
                        containerColor = if (torrent.infoHash == selectedHash)
                            space.deepflux.android.ui.theme.Accent.copy(alpha = 0.15f)
                        else MaterialTheme.colorScheme.surfaceVariant
                    ),
                    modifier = Modifier.fillMaxWidth().clickable(
                        onClick = { onSelect(torrent.infoHash) }
                    ),
                ) {
                    Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
                        Text(torrent.name, style = MaterialTheme.typography.titleSmall, maxLines = 1)
                        Text(
                            "${stateLabel(torrent.state)} · ${(torrent.progress * 100).toInt()}%",
                            style = MaterialTheme.typography.labelSmall,
                            color = DimText,
                        )
                    }
                }
            } else {
                TorrentCard(torrent, repository, onRemove = onRemove)
            }
        }
    }
}

/**
 * Tablet / TV detail pane (fix 2026-11-23): it used to show Play only — on a
 * wide screen the list cards carry no buttons, so after the first tap on a
 * torrent there was no way to pause, resume or remove it at all.
 */
@Composable
private fun TorrentDetail(
    torrent: TorrentItem,
    repository: TorrentRepository,
    modifier: Modifier = Modifier,
    onPauseResume: () -> Unit,
    onRemove: () -> Unit,
) {
    Column(modifier = Modifier.fillMaxSize().then(modifier).padding(16.dp)) {
        Text(torrent.name, style = MaterialTheme.typography.titleLarge)
        Spacer(Modifier.height(12.dp))
        LinearProgressIndicator(
            progress = { torrent.progress.coerceIn(0f, 1f) },
            modifier = Modifier.fillMaxWidth().height(8.dp),
        )
        Spacer(Modifier.height(12.dp))
        Text("↓ ${Fmt.speed(torrent.downloadRate)}   ↑ ${Fmt.speed(torrent.uploadRate)}",
            style = MaterialTheme.typography.bodyLarge)
        Text("${Fmt.bytes(torrent.doneBytes)} / ${Fmt.bytes(torrent.totalBytes)}",
            style = MaterialTheme.typography.bodyMedium, color = DimText)
        Text(Strings.t.seedsPeersFmt.format(torrent.seeds, torrent.peers),
            style = MaterialTheme.typography.bodyMedium, color = DimText)
        Spacer(Modifier.height(16.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (torrent.state == TorrentState.PAUSED) {
                androidx.compose.material3.Button(onClick = onPauseResume) {
                    Icon(Icons.Filled.PlayArrow, contentDescription = null)
                    Spacer(Modifier.width(6.dp))
                    Text(Strings.t.resume)
                }
            } else {
                androidx.compose.material3.OutlinedButton(onClick = onPauseResume) {
                    PauseBars(tint = MaterialTheme.colorScheme.onSurface)
                    Spacer(Modifier.width(6.dp))
                    Text(Strings.t.pause)
                }
            }
            Spacer(Modifier.width(10.dp))
            androidx.compose.material3.OutlinedButton(onClick = onRemove) {
                Icon(Icons.Filled.Delete, contentDescription = null, tint = ErrorRed)
                Spacer(Modifier.width(6.dp))
                Text(Strings.t.remove, color = ErrorRed)
            }
        }
        Spacer(Modifier.height(16.dp))
        if (torrent.firstVideoPath.isNotBlank()) {
            val ctx = androidx.compose.ui.platform.LocalContext.current
            ExtendedFloatingActionButton(
                onClick = { repository.playVideo(torrent, ctx) },
                icon = { Icon(Icons.Filled.PlayArrow, contentDescription = null) },
                text = { Text(Strings.t.play) },
            )
        }
    }
}

// ------------------------------------------------------------------ RSS (plan §13 #4)

/**
 * Watched RSS feeds: rows expand to the fetched items, each item
 * downloadable with one tap. Refresh/Edit/Delete per feed; adding and
 * editing happen in dialogs (URL + keywords + auto-download toggle).
 */
@Composable
private fun RssSection(rss: space.deepflux.android.rss.RssRepository) {
    val feeds by rss.feeds.collectAsState()
    val itemsByFeed by rss.itemsByFeed.collectAsState()
    val refreshing by rss.refreshing.collectAsState()
    val scope = androidx.compose.runtime.rememberCoroutineScope()
    var showAdd by remember { mutableStateOf(false) }
    var editing by remember { mutableStateOf<space.deepflux.android.rss.RssRepository.RssFeed?>(null) }
    var expandedId by remember { mutableStateOf<String?>(null) }
    // A feed is the user's configuration; its ✕ sits next to Edit — ask first.
    var confirmDeleteFeed by remember { mutableStateOf<space.deepflux.android.rss.RssRepository.RssFeed?>(null) }

    if (feeds.isEmpty()) {
        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(Strings.t.noFeeds, style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.height(6.dp))
                Text(
                    Strings.t.noFeedsHint,
                    style = MaterialTheme.typography.bodyMedium,
                    color = DimText,
                    modifier = Modifier.padding(horizontal = 32.dp),
                )
                Spacer(Modifier.height(10.dp))
                TextButton(onClick = { showAdd = true }) { Text(Strings.t.addFeed) }
            }
        }
    } else {
        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            // Room below the last card for the floating "Add download" button: it
            // covers the bottom of the list, and the last card's buttons sat under
            // it for good — the video's Play among them (emulator, 2026-11-25).
            contentPadding = PaddingValues(start = 12.dp, top = 12.dp, end = 12.dp, bottom = 96.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            items(feeds, key = { it.id }) { feed ->
                RssFeedCard(
                    feed = feed,
                    items = itemsByFeed[feed.id].orEmpty(),
                    isRefreshing = feed.id in refreshing,
                    expanded = expandedId == feed.id,
                    onToggle = {
                        expandedId = if (expandedId == feed.id) null else feed.id
                        if (expandedId == feed.id && itemsByFeed[feed.id] == null) {
                            scope.launch(kotlinx.coroutines.Dispatchers.IO) { runCatching { rss.refresh(feed.id) } }
                        }
                    },
                    onRefresh = { scope.launch(kotlinx.coroutines.Dispatchers.IO) { runCatching { rss.refresh(feed.id) } } },
                    onEdit = { editing = feed },
                    onDelete = { confirmDeleteFeed = feed },
                    onDownload = { guid -> scope.launch(kotlinx.coroutines.Dispatchers.IO) { rss.downloadItem(feed.id, guid) } },
                )
            }
            item {
                TextButton(onClick = { showAdd = true }) { Text(Strings.t.addFeed) }
            }
        }
    }

    if (showAdd) {
        AddFeedDialog(
            onAdd = { url, keywords, auto ->
                scope.launch(kotlinx.coroutines.Dispatchers.IO) {
                    rss.addFeed(url, keywords, auto)
                        .exceptionOrNull()
                        ?.let { rss.postNotice("${Strings.t.errorPrefix}${it.message ?: "feed error"}") }
                }
                showAdd = false
            },
            onDismiss = { showAdd = false },
        )
    }
    if (confirmDeleteFeed != null) {
        val feed = confirmDeleteFeed!!
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { confirmDeleteFeed = null },
            title = { Text(Strings.t.deleteFeed) },
            text = { Text(Strings.t.deleteFeedFmt.format(feed.title.ifBlank { feed.url })) },
            confirmButton = {
                androidx.compose.material3.TextButton(onClick = {
                    rss.removeFeed(feed.id)
                    confirmDeleteFeed = null
                }) { Text(Strings.t.remove, color = ErrorRed) }
            },
            dismissButton = {
                androidx.compose.material3.TextButton(onClick = { confirmDeleteFeed = null }) { Text(Strings.t.cancel) }
            },
        )
    }
    if (editing != null) {
        EditFeedDialog(
            feed = editing!!,
            onSave = { auto, keywords ->
                rss.setFeedConfig(editing!!.id, auto, keywords)
                editing = null
            },
            onDismiss = { editing = null },
        )
    }
}

@Composable
private fun RssFeedCard(
    feed: space.deepflux.android.rss.RssRepository.RssFeed,
    items: List<space.deepflux.android.rss.RssClient.RssItem>,
    isRefreshing: Boolean,
    expanded: Boolean,
    onToggle: () -> Unit,
    onRefresh: () -> Unit,
    onEdit: () -> Unit,
    onDelete: () -> Unit,
    onDownload: (String) -> Unit,
) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
        Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(
                    modifier = Modifier.weight(1f).clickable(onClick = onToggle),
                ) {
                    Text(
                        feed.title.ifBlank { feed.url },
                        style = MaterialTheme.typography.titleSmall,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Text(
                        feed.url,
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Spacer(Modifier.height(2.dp))
                    Text(
                        when {
                            feed.autoDownload && feed.keywords.isNotEmpty() ->
                                "⚡ ${feed.keywords.joinToString(", ")}"
                            feed.autoDownload -> "⚡"
                            else -> "—"
                        },
                        style = MaterialTheme.typography.labelSmall,
                        color = if (feed.autoDownload) AccentData else DimText,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    if (feed.lastError.isNotBlank()) {
                        Text(
                            "${Strings.t.errorPrefix}${feed.lastError}",
                            style = MaterialTheme.typography.labelSmall,
                            color = ErrorRed,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
                if (isRefreshing) {
                    androidx.compose.material3.CircularProgressIndicator(
                        modifier = Modifier.size(22.dp),
                        strokeWidth = 2.dp,
                    )
                } else {
                    IconButton(onClick = onRefresh) {
                        Icon(Icons.Filled.Refresh, contentDescription = Strings.t.refresh)
                    }
                }
                IconButton(onClick = onEdit) {
                    Icon(Icons.Filled.Edit, contentDescription = Strings.t.editFeed)
                }
                IconButton(onClick = onDelete) {
                    Icon(Icons.Filled.Delete, contentDescription = Strings.t.deleteFeed, tint = ErrorRed)
                }
            }
            if (expanded) {
                Spacer(Modifier.height(6.dp))
                if (items.isEmpty()) {
                    Text(
                        if (feed.lastError.isBlank()) Strings.t.loading else Strings.t.errorPrefix + feed.lastError,
                        style = MaterialTheme.typography.bodySmall,
                        color = DimText,
                        modifier = Modifier.padding(vertical = 6.dp),
                    )
                } else {
                    Text(
                        Strings.t.feedItemsCountFmt.format(items.size),
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText,
                    )
                    items.take(30).forEach { item ->
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
                        ) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text(
                                    item.title,
                                    style = MaterialTheme.typography.bodySmall,
                                    maxLines = 2,
                                    overflow = TextOverflow.Ellipsis,
                                )
                            }
                            TextButton(onClick = { onDownload(item.guid) }) {
                                Text(Strings.t.grabVideo)
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun AddFeedDialog(
    onAdd: (url: String, keywords: List<String>, auto: Boolean) -> Unit,
    onDismiss: () -> Unit,
) {
    var url by remember { mutableStateOf("") }
    var keywords by remember { mutableStateOf("") }
    var auto by remember { mutableStateOf(false) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(Strings.t.addFeed) },
        text = {
            Column {
                OutlinedTextField(
                    value = url,
                    onValueChange = { url = it },
                    label = { Text(Strings.t.feedUrlLabel) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = keywords,
                    onValueChange = { keywords = it },
                    label = { Text(Strings.t.keywordsLabel) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = auto, onCheckedChange = { auto = it })
                    Text(Strings.t.autoDownload, style = MaterialTheme.typography.bodyMedium)
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = url.trim().startsWith("http"),
                onClick = {
                    val kws = keywords.split(',').map { it.trim() }.filter { it.isNotBlank() }
                    onAdd(url.trim(), kws, auto && kws.isNotEmpty())
                },
            ) { Text(Strings.t.add) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(Strings.t.cancel) } },
    )
}

@Composable
private fun EditFeedDialog(
    feed: space.deepflux.android.rss.RssRepository.RssFeed,
    onSave: (auto: Boolean, keywords: List<String>) -> Unit,
    onDismiss: () -> Unit,
) {
    var keywords by remember { mutableStateOf(feed.keywords.joinToString(", ")) }
    var auto by remember { mutableStateOf(feed.autoDownload) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("${Strings.t.editFeed} — ${feed.title.ifBlank { feed.url }}") },
        text = {
            Column {
                Text(feed.url, style = MaterialTheme.typography.labelSmall, color = DimText, maxLines = 2)
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = keywords,
                    onValueChange = { keywords = it },
                    label = { Text(Strings.t.keywordsLabel) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = auto, onCheckedChange = { auto = it })
                    Text(Strings.t.autoDownload, style = MaterialTheme.typography.bodyMedium)
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = !auto || keywords.split(',').any { it.isNotBlank() },
                onClick = {
                    onSave(auto, keywords.split(',').map { it.trim() }.filter { it.isNotBlank() })
                },
            ) { Text(Strings.t.save) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(Strings.t.cancel) } },
    )
}
