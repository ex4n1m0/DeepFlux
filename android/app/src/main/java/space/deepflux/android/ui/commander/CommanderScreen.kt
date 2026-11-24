package space.deepflux.android.ui.commander

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.filecmd.CommanderRepository
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.DimText
import space.deepflux.android.ui.theme.ErrorRed
import space.deepflux.android.ui.theme.OkGreen
import java.text.DateFormat
import java.util.Date

private object Fmt {
    fun bytes(v: Long): String = when {
        v >= 1 shl 30 -> "%.1f GB".format(v / 1073741824.0)
        v >= 1 shl 20 -> "%.1f MB".format(v / 1048576.0)
        v >= 1 shl 10 -> "%.1f KB".format(v / 1024.0)
        else -> "$v B"
    }
}

private val PLAYABLE = space.deepflux.android.util.Playables.EXT // one source of truth (P4)

/** Pane navigation as a plain list, for rememberSaveable (tab switches must not reset it). */
internal val SafStackSaver = androidx.compose.runtime.saveable.listSaver<List<Pair<String, String>>, String>(
    save = { stack -> stack.map { (uri, name) -> "$uri\u0001$name" } },
    restore = { saved -> saved.map { it.substringBefore('\u0001') to it.substringAfter('\u0001') } },
)

internal val SelectionSaver = androidx.compose.runtime.saveable.listSaver<Set<String>, String>(
    save = { it.toList() },
    restore = { it.toSet() },
)

/**
 * Commander tab (plan §12 Phase B): the desktop's dual-pane orthodox file
 * manager adapted to touch and scoped storage. Wide screens (the owner's
 * 16:9 tablet) get two independent panes; compact screens get one.
 * Long-press enters multi-select; the desktop's F-key operations live in
 * the selection action row (play / share / rename / delete / details).
 */
@Composable
fun CommanderScreen(appRoot: java.io.File, torrentRepo: TorrentRepository, isWide: Boolean) {
    // System Back goes UP in the pane the user touched last (fix 2026-11-23:
    // it used to leave the app — wrong for a file manager).
    var activePane by androidx.compose.runtime.saveable.rememberSaveable { mutableStateOf("L") }
    if (isWide) {
        Row(modifier = Modifier.fillMaxSize()) {
            // One repository PER PANE: the listing flow is pane state — a
            // shared one would have each pane repaint the other's directory.
            Box(modifier = Modifier.weight(1f)) {
                Pane(appRoot, torrentRepo, paneTag = "L", isBackTarget = activePane == "L", onInteract = { activePane = "L" })
            }
            Box(
                modifier = Modifier
                    .width(1.dp)
                    .fillMaxSize()
                    .background(MaterialTheme.colorScheme.surfaceVariant)
            )
            Box(modifier = Modifier.weight(1f)) {
                Pane(appRoot, torrentRepo, paneTag = "R", isBackTarget = activePane == "R", onInteract = { activePane = "R" })
            }
        }
    } else {
        Pane(appRoot, torrentRepo, paneTag = "L", isBackTarget = true, onInteract = {})
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun Pane(
    appRoot: java.io.File,
    torrentRepo: TorrentRepository,
    paneTag: String,
    /** System Back acts on this pane (the one touched last). */
    isBackTarget: Boolean,
    /** The user interacted with this pane — it becomes the Back target. */
    onInteract: () -> Unit,
) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    val repository = remember(paneTag) { CommanderRepository(ctx.applicationContext, appRoot) }
    val listing by repository.listing.collectAsState()
    val safRoots by repository.safRoots.roots.collectAsState()
    // Folder sizes + the storage total arrive from the repository's
    // off-thread walker (they used to be computed during composition).
    val dirSizes by repository.dirSizes.collectAsState()
    val storageUsed by repository.storageUsed.collectAsState()
    // Navigation state is rememberSaveable: the Commander tab is disposed on
    // every app-tab switch, and both panes used to come back at the storage
    // root. Saved as plain strings (see the savers below the pane).
    var root by rememberSaveable(paneTag) { mutableStateOf(CommanderRepository.Root.APP_STORAGE) }
    var subDir by rememberSaveable(paneTag) { mutableStateOf("") }
    // SAF navigation (v1.3): the active tree root (its uri) + a (uri, name) stack.
    var activeSafUri by rememberSaveable(paneTag) { mutableStateOf("") }
    var safStack by rememberSaveable(paneTag, stateSaver = SafStackSaver) { mutableStateOf(listOf<Pair<String, String>>()) }
    val selected = rememberSaveable(paneTag, stateSaver = SelectionSaver) { mutableStateOf(setOf<String>()) }

    /** The chosen SAF root, looked up from its saved uri (null once its grant is gone). */
    val activeSafRoot: space.deepflux.android.filecmd.SafRootsStore.SafRoot? =
        safRoots.find { it.uriString == activeSafUri }
    var renameTarget by remember { mutableStateOf<CommanderRepository.Entry?>(null) }
    var detailsTarget by remember { mutableStateOf<CommanderRepository.Entry?>(null) }
    var confirmDelete by remember { mutableStateOf(false) }

    // The system folder picker: a chosen folder gets a persisted grant and
    // becomes a browsable Commander root (the "broadest legitimate access"
    // lane — the user explicitly hands us each tree).
    val folderPicker = androidx.activity.compose.rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.OpenDocumentTree(),
    ) { uri ->
        if (uri != null) {
            runCatching {
                ctx.contentResolver.takePersistableUriPermission(
                    uri,
                    android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION or
                        android.content.Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
                )
            }
            val name = CommanderRepository.pickedFolderName(uri)
            repository.safRoots.add(uri.toString(), name)
            // Jump straight into the new folder.
            activeSafUri = uri.toString()
            root = CommanderRepository.Root.SAF
            safStack = emptyList()
            selected.value = emptySet()
        }
    }

    fun openCurrent() {
        when {
            root == CommanderRepository.Root.SAF -> {
                val r = activeSafRoot
                if (r == null) {
                    repository.open(CommanderRepository.Root.SAF)
                } else {
                    val top = safStack.lastOrNull()
                    repository.openSaf(top?.first ?: "", r, (listOf(r.name) + safStack.map { it.second }).joinToString("/"))
                }
            }
            else -> repository.open(root, subDir)
        }
    }

    LaunchedEffect(root, subDir, activeSafUri, safStack, safRoots) { openCurrent() }
    // The storage total is walked off the UI thread by the repository — just ask.
    LaunchedEffect(root, listing.dir) {
        if (root == CommanderRepository.Root.APP_STORAGE) repository.refreshStorageUsed()
    }

    val entries = listing.entries
    val sel = selected.value

    val canGoUp = (root == CommanderRepository.Root.APP_STORAGE && subDir.isNotBlank()) ||
        (root == CommanderRepository.Root.SAF && safStack.isNotEmpty())
    fun goUp() {
        if (root == CommanderRepository.Root.APP_STORAGE) {
            subDir = repository.parentOf(listing).orEmpty()
        } else {
            safStack = safStack.dropLast(1)
        }
    }
    // System Back = up one folder (this pane is the Back target when touched last).
    androidx.activity.compose.BackHandler(enabled = isBackTarget && canGoUp) { goUp() }

    Column(modifier = Modifier.fillMaxSize()) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 10.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            RootPicker(
                selected = root,
                activeSafRoot = activeSafRoot,
                safRoots = safRoots,
                onPick = {
                    onInteract()
                    root = it
                    subDir = ""
                    safStack = emptyList()
                    activeSafUri = ""
                    selected.value = emptySet()
                },
                onPickSaf = { r ->
                    onInteract()
                    root = CommanderRepository.Root.SAF
                    activeSafUri = r.uriString
                    safStack = emptyList()
                    selected.value = emptySet()
                },
                onRemoveSaf = { r ->
                    repository.safRoots.remove(r.uriString)
                    if (activeSafUri == r.uriString) {
                        activeSafUri = ""
                        root = CommanderRepository.Root.APP_STORAGE
                    }
                },
                onAddFolder = { runCatching { folderPicker.launch(null) } },
            )
            Spacer(Modifier.width(8.dp))
            if (canGoUp) {
                IconButton(onClick = {
                    onInteract()
                    goUp()
                }) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = Strings.t.backDesc)
                }
            }
            Spacer(Modifier.weight(1f))
            Text(
                if (root == CommanderRepository.Root.APP_STORAGE)
                    storageUsed?.let { Strings.t.storageUsedFmt.format(Fmt.bytes(it)) } ?: ""
                else "${entries.size} ${Strings.t.items}",
                style = MaterialTheme.typography.labelSmall,
                color = DimText,
            )
        }
        if (listing.dir.isNotBlank()) {
            Text(
                "📁 ${listing.dir}",
                style = MaterialTheme.typography.labelSmall,
                color = Accent,
                modifier = Modifier.padding(horizontal = 14.dp),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }

        Box(modifier = Modifier.weight(1f)) {
            if (entries.isEmpty() && listing.error.isBlank()) {
                Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text(
                        if (root == CommanderRepository.Root.SAF) Strings.t.noFoldersYet
                        else Strings.t.emptyFolder,
                        style = MaterialTheme.typography.bodyMedium,
                        color = DimText,
                    )
                }
            } else {
                LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(10.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    items(entries, key = { it.path }) { entry ->
                        val isSel = entry.path in sel
                        Card(
                            colors = CardDefaults.cardColors(
                                containerColor = if (isSel) Accent.copy(alpha = 0.2f)
                                else MaterialTheme.colorScheme.surfaceVariant
                            ),
                            modifier = Modifier
                                .fillMaxWidth()
                                .combinedClickable(
                                    onClick = {
                                        onInteract()
                                        when {
                                            sel.isNotEmpty() ->
                                                selected.value = if (isSel) sel - entry.path else sel + entry.path
                                            entry.isDirectory && root == CommanderRepository.Root.SAF ->
                                                safStack = safStack + (entry.path to entry.name)
                                            entry.isDirectory -> subDir = childDir(listing.dir, entry.name)
                                            else -> openEntry(entry, repository, torrentRepo, ctx)
                                        }
                                    },
                                    onLongClick = {
                                        onInteract()
                                        selected.value = if (isSel) sel - entry.path else sel + entry.path
                                    },
                                ),
                        ) {
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
                            ) {
                                if (sel.isNotEmpty()) {
                                    Checkbox(checked = isSel, onCheckedChange = {
                                        selected.value = if (isSel) sel - entry.path else sel + entry.path
                                    })
                                }
                                Text(
                                    if (entry.isDirectory) "📁" else docGlyph(entry.name),
                                    style = MaterialTheme.typography.titleMedium,
                                )
                                Spacer(Modifier.width(10.dp))
                                Column(modifier = Modifier.weight(1f)) {
                                    Text(
                                        entry.name,
                                        style = MaterialTheme.typography.bodyMedium,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                    )
                                    Text(
                                        sizeText(entry, dirSizes) + " · " + dateOf(entry.modified),
                                        style = MaterialTheme.typography.labelSmall,
                                        color = DimText,
                                    )
                                }
                                if (!entry.isDirectory && sel.isEmpty()) {
                                    // Media plays in-app; anything else opens in
                                    // its own app. (Every row used to show a
                                    // green play button, whatever the file was.)
                                    if (PLAYABLE.containsMatchIn(entry.name)) {
                                        IconButton(onClick = {
                                            onInteract()
                                            openEntry(entry, repository, torrentRepo, ctx)
                                        }) {
                                            Icon(Icons.Filled.PlayArrow, contentDescription = Strings.t.play, tint = OkGreen)
                                        }
                                    } else {
                                        IconButton(onClick = {
                                            onInteract()
                                            openEntry(entry, repository, torrentRepo, ctx)
                                        }) {
                                            Icon(
                                                space.deepflux.android.ui.AppIcons.OpenInNew,
                                                contentDescription = Strings.t.openAction,
                                                tint = Accent,
                                            )
                                        }
                                    }
                                    IconButton(onClick = {
                                        repository.shareIntent(entry)?.let { runCatching { ctx.startActivity(it) } }
                                    }) {
                                        Icon(Icons.Filled.Share, contentDescription = Strings.t.share, tint = Accent)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        if (sel.isNotEmpty()) {
            Surface(color = MaterialTheme.colorScheme.surfaceVariant) {
                Row(
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 10.dp, vertical = 2.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        Strings.t.selectedFmt.format(sel.size),
                        style = MaterialTheme.typography.labelMedium,
                        modifier = Modifier.weight(1f),
                    )
                    val single = entries.find { it.path == sel.first() }?.takeIf { sel.size == 1 }
                    if (single != null) {
                        TextButton(onClick = { renameTarget = single }) { Text(Strings.t.rename) }
                        TextButton(onClick = { detailsTarget = single }) { Text(Strings.t.details) }
                    }
                    IconButton(onClick = { confirmDelete = true }) {
                        Icon(Icons.Filled.Delete, contentDescription = Strings.t.remove, tint = ErrorRed)
                    }
                    TextButton(onClick = { selected.value = emptySet() }) { Text("✕") }
                }
            }
        }
        if (listing.error.isNotBlank()) {
            Text(
                listing.error,
                style = MaterialTheme.typography.labelSmall,
                color = ErrorRed,
                modifier = Modifier.padding(10.dp),
            )
        }
    }

    if (renameTarget != null) {
        RenameDialog(
            initial = renameTarget!!.name,
            onConfirm = { name ->
                val target = renameTarget!!
                val clean = name.trim().replace(Regex("[\\\\/:*?\"<>|]"), "_")
                when {
                    clean == target.name -> Unit // unchanged: nothing to do
                    repository.rename(target, name) -> {
                        selected.value = emptySet()
                        torrentRepo.postNotice(Strings.t.noticeRenamedToFmt.format(clean))
                    }
                    // Used to fail silently: Android refuses renames of files
                    // owned by other apps, and the row just stayed as it was.
                    else -> torrentRepo.postNotice(Strings.t.noticeRenameFailed)
                }
                renameTarget = null
                openCurrent()
            },
            onDismiss = { renameTarget = null },
        )
    }
    if (detailsTarget != null) {
        val e = detailsTarget!!
        AlertDialog(
            onDismissRequest = { detailsTarget = null },
            title = { Text(Strings.t.details) },
            text = {
                Column {
                    Text(e.name, style = MaterialTheme.typography.titleSmall)
                    Spacer(Modifier.height(6.dp))
                    Text(
                        "${Strings.t.nameLabel}: ${e.name}\n" +
                            "${Strings.t.sizeLabel}: ${sizeText(e, dirSizes)}\n" +
                            "${Strings.t.modifiedLabel}: ${dateOf(e.modified)}\n" +
                            "${Strings.t.typeLabel}: ${e.mime.ifBlank { "—" }}\n" +
                            when (e.source) {
                                CommanderRepository.Source.APP -> Strings.t.sourceApp
                                CommanderRepository.Source.SAF -> Strings.t.foldersSection
                                else -> Strings.t.sourcePublic
                            },
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            },
            confirmButton = { TextButton(onClick = { detailsTarget = null }) { Text(Strings.t.cancel) } },
        )
    }
    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text(Strings.t.deleteTitle) },
            text = { Text(Strings.t.confirmDeleteFmt.format(sel.size)) },
            confirmButton = {
                TextButton(onClick = {
                    // Count what Android refused: public files owned by other
                    // apps mostly cannot be deleted, and that used to pass in
                    // silence — the row simply stayed.
                    var failed = 0
                    sel.forEach { p ->
                        entries.find { it.path == p }?.let { if (!repository.delete(it)) failed++ }
                    }
                    val count = sel.size
                    selected.value = emptySet()
                    confirmDelete = false
                    openCurrent()
                    if (failed > 0) torrentRepo.postNotice(Strings.t.noticeDeleteFailedFmt.format(failed))
                    else torrentRepo.postNotice(Strings.t.noticeDeletedFmt.format(count))
                }) { Text(Strings.t.remove, color = ErrorRed) }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text(Strings.t.cancel) } },
        )
    }
}

private fun childDir(parent: String, name: String): String =
    if (parent.isBlank()) name else "$parent/$name"

/**
 * "12.3 MB". A folder in the app's own storage shows "…" until the
 * background walker has delivered its size. Other roots (public folders,
 * SAF) are never walked — there "…" read as "still loading", forever.
 */
private fun sizeText(entry: CommanderRepository.Entry, dirSizes: Map<String, Long>): String = when {
    !entry.isDirectory -> Fmt.bytes(entry.size)
    entry.source != CommanderRepository.Source.APP -> "—"
    else -> dirSizes[entry.path]?.let { Fmt.bytes(it) } ?: "…"
}

/** Play in-app (video/audio via the loopback stream); fall back to open-with. */
private fun openEntry(
    entry: CommanderRepository.Entry,
    repository: CommanderRepository,
    torrentRepo: TorrentRepository,
    ctx: android.content.Context,
) {
    val launched = if (PLAYABLE.containsMatchIn(entry.name)) {
        if (entry.source == CommanderRepository.Source.APP) {
            torrentRepo.playFile(entry.path, entry.name, ctx)
        } else {
            // MEDIASTORE + SAF entries are content uris — same loopback path.
            torrentRepo.playContentUri(android.net.Uri.parse(entry.path), entry.name, entry.mime, ctx)
        }
    } else false
    if (!launched) {
        val intent = repository.openWithIntent(entry)
        if (intent == null) {
            torrentRepo.postNotice(Strings.t.noticeCannotOpenItem)
            return
        }
        runCatching { ctx.startActivity(intent) }
            .onFailure { torrentRepo.postNotice(Strings.t.folderInaccessible) }
    }
}

private fun docGlyph(name: String): String = when {
    Regex("\\.(mp3|m4a|flac|aac|ogg|opus|wav)$", RegexOption.IGNORE_CASE).containsMatchIn(name) -> "🎵"
    name.endsWith(".torrent", true) -> "🌐"
    else -> "📄"
}

private fun dateOf(millis: Long): String =
    if (millis <= 0) "—"
    else DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT).format(Date(millis))

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun RootPicker(
    selected: CommanderRepository.Root,
    activeSafRoot: space.deepflux.android.filecmd.SafRootsStore.SafRoot?,
    safRoots: List<space.deepflux.android.filecmd.SafRootsStore.SafRoot>,
    onPick: (CommanderRepository.Root) -> Unit,
    onPickSaf: (space.deepflux.android.filecmd.SafRootsStore.SafRoot) -> Unit,
    onRemoveSaf: (space.deepflux.android.filecmd.SafRootsStore.SafRoot) -> Unit,
    onAddFolder: () -> Unit,
) {
    var open by remember { mutableStateOf(false) }
    fun currentLabel(): String = when {
        selected == CommanderRepository.Root.SAF -> activeSafRoot?.name ?: Strings.t.foldersSection
        else -> when (selected) {
            CommanderRepository.Root.APP_STORAGE -> Strings.t.appStorage
            CommanderRepository.Root.PUBLIC_DOWNLOADS -> Strings.t.publicDownloads
            CommanderRepository.Root.PUBLIC_VIDEOS -> Strings.t.publicVideos
            CommanderRepository.Root.PUBLIC_MUSIC -> Strings.t.publicMusic
            CommanderRepository.Root.PUBLIC_PICTURES -> Strings.t.publicPictures
            CommanderRepository.Root.SAF -> Strings.t.foldersSection
        }
    }
    Surface(
        shape = RoundedCornerShape(8.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
        modifier = Modifier.combinedClickable(onClick = { open = true }),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
        ) {
            Text(currentLabel(), style = MaterialTheme.typography.labelLarge, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text("  ▾", style = MaterialTheme.typography.labelMedium, color = DimText)
        }
    }
    DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
        DropdownMenuItem(
            text = { Text(Strings.t.appStorage) },
            onClick = { open = false; onPick(CommanderRepository.Root.APP_STORAGE) },
        )
        DropdownMenuItem(
            text = { Text(Strings.t.publicDownloads) },
            onClick = { open = false; onPick(CommanderRepository.Root.PUBLIC_DOWNLOADS) },
        )
        DropdownMenuItem(
            text = { Text(Strings.t.publicVideos) },
            onClick = { open = false; onPick(CommanderRepository.Root.PUBLIC_VIDEOS) },
        )
        DropdownMenuItem(
            text = { Text(Strings.t.publicMusic) },
            onClick = { open = false; onPick(CommanderRepository.Root.PUBLIC_MUSIC) },
        )
        DropdownMenuItem(
            text = { Text(Strings.t.publicPictures) },
            onClick = { open = false; onPick(CommanderRepository.Root.PUBLIC_PICTURES) },
        )
        // ---- Chosen folders (SAF) ----
        HorizontalDivider(color = MaterialTheme.colorScheme.outline, thickness = 0.5.dp)
        if (safRoots.isEmpty()) {
            DropdownMenuItem(
                text = { Text(Strings.t.noFoldersYet, style = MaterialTheme.typography.labelSmall, color = DimText) },
                onClick = {},
                enabled = false,
            )
        }
        safRoots.forEach { rootEntry ->
            DropdownMenuItem(
                text = { Text("📁 ${rootEntry.name}", maxLines = 1, overflow = TextOverflow.Ellipsis) },
                trailingIcon = {
                    IconButton(onClick = { onRemoveSaf(rootEntry) }, modifier = Modifier.padding(0.dp)) {
                        Icon(
                            Icons.Filled.Close,
                            contentDescription = Strings.t.remove,
                            tint = DimText,
                            modifier = Modifier.size(16.dp),
                        )
                    }
                },
                onClick = { open = false; onPickSaf(rootEntry) },
            )
        }
        DropdownMenuItem(
            text = { Text("＋ " + Strings.t.addFolder, color = Accent) },
            onClick = { open = false; onAddFolder() },
        )
        HorizontalDivider(color = MaterialTheme.colorScheme.outline, thickness = 0.5.dp)
        DropdownMenuItem(
            text = {
                Text(
                    Strings.t.storageAccessNote,
                    style = MaterialTheme.typography.labelSmall,
                    color = DimText,
                )
            },
            onClick = {},
            enabled = false,
        )
    }
}

@Composable
private fun RenameDialog(initial: String, onConfirm: (String) -> Unit, onDismiss: () -> Unit) {
    var text by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(Strings.t.rename) },
        text = {
            OutlinedTextField(
                value = text,
                onValueChange = { text = it },
                singleLine = true,
                isError = text.isBlank(),
                modifier = Modifier.fillMaxWidth(),
            )
        },
        confirmButton = {
            TextButton(enabled = text.isNotBlank(), onClick = { onConfirm(text) }) { Text(Strings.t.rename) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(Strings.t.cancel) } },
    )
}
