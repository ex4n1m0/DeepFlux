package space.deepflux.android.ui.iptv

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import space.deepflux.android.iptv.PlaylistStore
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.DimText
import space.deepflux.android.ui.theme.ErrorRed

/**
 * Playlist management UI (v1.3): the editor (add/edit — name, type, URL or
 * Xtream credentials, EPG URL) and the manage sheet (list, switch, edit,
 * delete). Saving validates and never drops other entries; errors from the
 * LOAD surface back in [IPTVScreen] keep everything saved here intact.
 */

/** Same sample playlist the desktop installer seeds (iptv-org Macau, 7 channels). */
const val SAMPLE_PLAYLIST_URL = "https://iptv-org.github.io/iptv/countries/mo.m3u"

@Composable
fun PlaylistEditorDialog(
    initial: PlaylistStore.Playlist?,
    onSave: (PlaylistStore.Playlist) -> Unit,
    onDismiss: () -> Unit,
) {
    val editing = initial != null
    var kind by remember { mutableStateOf(initial?.kind ?: "m3u") }
    var name by remember { mutableStateOf(initial?.name ?: "") }
    var url by remember { mutableStateOf(initial?.url ?: "") }
    var user by remember { mutableStateOf(initial?.username ?: "") }
    var pass by remember { mutableStateOf(initial?.password ?: "") }
    var epg by remember { mutableStateOf(initial?.epgUrl ?: "") }
    var contentType by remember { mutableStateOf(initial?.contentType ?: PlaylistStore.CONTENT_AUTO) }

    // Local-folder sources (1.3.5): the system folder picker grants the
    // tree; picking one saves the playlist immediately.
    val ctx = androidx.compose.ui.platform.LocalContext.current
    val folderPicker = androidx.activity.compose.rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.OpenDocumentTree(),
    ) { uri ->
        if (uri != null) {
            runCatching {
                ctx.contentResolver.takePersistableUriPermission(
                    uri,
                    android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION,
                )
            }
            val folderName = space.deepflux.android.filecmd.CommanderRepository.pickedFolderName(uri)
            onSave(
                (initial ?: PlaylistStore.Playlist(id = "", name = "", kind = "local", url = "")).copy(
                    name = name.trim().ifBlank { folderName },
                    kind = "local",
                    url = uri.toString(),
                ),
            )
        }
    }

    val valid = when (kind) {
        "local" -> url.startsWith("content://")
        else -> name.isNotBlank() && url.trim().startsWith("http") &&
            (kind == "m3u" || (user.isNotBlank() && pass.isNotBlank()))
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (editing) Strings.t.editPlaylist else Strings.t.addPlaylist) },
        text = {
            Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilterChip(
                        selected = kind == "m3u",
                        onClick = { kind = "m3u" },
                        label = { Text(Strings.t.playlistTypeM3u) },
                        colors = FilterChipDefaults.filterChipColors(selectedContainerColor = Accent.copy(alpha = 0.25f)),
                    )
                    FilterChip(
                        selected = kind == "xtream",
                        onClick = { kind = "xtream" },
                        label = { Text(Strings.t.playlistTypeXtream) },
                        colors = FilterChipDefaults.filterChipColors(selectedContainerColor = Accent.copy(alpha = 0.25f)),
                    )
                    FilterChip(
                        selected = kind == "local",
                        onClick = { kind = "local" },
                        label = { Text(Strings.t.playlistTypeLocal) },
                        colors = FilterChipDefaults.filterChipColors(selectedContainerColor = Accent.copy(alpha = 0.25f)),
                    )
                }
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = name,
                    onValueChange = { name = it },
                    label = { Text(Strings.t.playlistNameLabel) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                if (kind == "local") {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        Strings.t.liveTvHint,
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText,
                    )
                    Spacer(Modifier.height(4.dp))
                    TextButton(onClick = { runCatching { folderPicker.launch(null) } }) {
                        Text("＋ " + Strings.t.chooseFolder, color = Accent)
                    }
                }
                Spacer(Modifier.height(6.dp))
                if (kind == "local") {
                    // Nothing else to type — the folder picker fills the rest.
                } else {
                    OutlinedTextField(
                        value = url,
                        onValueChange = { url = it },
                        label = {
                            Text(
                                if (kind == "xtream") Strings.t.serverUrl
                                else Strings.t.playlistUrl,
                            )
                        },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                        if (kind == "xtream") {
                            Spacer(Modifier.height(6.dp))
                            ContentChips(contentType) { contentType = it }
                            Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = user,
                            onValueChange = { user = it },
                            label = { Text(Strings.t.username) },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = pass,
                            onValueChange = { pass = it },
                            label = { Text(Strings.t.password) },
                            singleLine = true,
                            visualTransformation = PasswordVisualTransformation(),
                            modifier = Modifier.fillMaxWidth(),
                        )
                    } else {
                        ContentChips(contentType) { contentType = it }
                        Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = epg,
                            onValueChange = { epg = it },
                            label = { Text(Strings.t.epgUrl) },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = valid,
                onClick = {
                    onSave(
                        (initial ?: PlaylistStore.Playlist(id = "", name = "", kind = "m3u", url = "")).copy(
                            name = name.trim(),
                            kind = kind,
                            url = url.trim(),
                            username = if (kind == "xtream") user.trim() else "",
                            password = if (kind == "xtream") pass.trim() else "",
                            epgUrl = if (kind == "m3u") epg.trim() else "",
                            contentType = contentType,
                        ),
                    )
                },
            ) { Text(Strings.t.save) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(Strings.t.cancel) } },
    )
}

/**
 * The per-source content override: what the user says this source carries.
 * "Auto" leaves it to the entry rules; "Live TV" and "VOD" decide for the
 * whole source — for providers whose group names no keyword rule knows.
 */
@Composable
private fun ContentChips(contentType: String, onPick: (String) -> Unit) {
    Spacer(Modifier.height(10.dp))
    Text(Strings.t.contentTypeLabel, style = MaterialTheme.typography.labelLarge)
    Spacer(Modifier.height(4.dp))
    Row(
        modifier = Modifier.horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        for ((value, label) in listOf(
            PlaylistStore.CONTENT_AUTO to Strings.t.contentTypeAuto,
            PlaylistStore.CONTENT_LIVE to Strings.t.contentTypeLive,
            PlaylistStore.CONTENT_VOD to Strings.t.contentTypeVod,
        )) {
            FilterChip(
                selected = contentType == value,
                onClick = { onPick(value) },
                label = { Text(label) },
                colors = FilterChipDefaults.filterChipColors(selectedContainerColor = Accent.copy(alpha = 0.25f)),
            )
        }
    }
    Text(Strings.t.contentTypeHint, style = MaterialTheme.typography.labelSmall, color = DimText)
}

@Composable
fun ManagePlaylistsDialog(
    playlists: List<PlaylistStore.Playlist>,
    activeId: String,
    onEdit: (PlaylistStore.Playlist) -> Unit,
    onAdd: () -> Unit,
    onRemove: (PlaylistStore.Playlist) -> Unit,
    onDismiss: () -> Unit,
) {
    var confirmDelete by remember { mutableStateOf<PlaylistStore.Playlist?>(null) }

    if (confirmDelete != null) {
        AlertDialog(
            onDismissRequest = { confirmDelete = null },
            title = { Text(Strings.t.deletePlaylistTitle) },
            text = { Text(Strings.t.confirmDeletePlaylistFmt.format(confirmDelete!!.name)) },
            confirmButton = {
                TextButton(onClick = {
                    onRemove(confirmDelete!!)
                    confirmDelete = null
                }) { Text(Strings.t.remove, color = ErrorRed) }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = null }) { Text(Strings.t.cancel) } },
        )
        return
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(Strings.t.managePlaylists) },
        text = {
            if (playlists.isEmpty()) {
                Text(Strings.t.liveTvHint, style = MaterialTheme.typography.bodySmall, color = DimText)
            } else {
                LazyColumn(modifier = Modifier.heightIn(max = 380.dp)) {
                    items(playlists, key = { it.id }) { p ->
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
                        ) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text(
                                    (if (p.id == activeId) "▶ " else "") + p.name,
                                    style = MaterialTheme.typography.bodyMedium,
                                    color = if (p.id == activeId) Accent else MaterialTheme.colorScheme.onSurface,
                                    maxLines = 1,
                                )
                                Text(
                                    when (p.kind) {
                                        PlaylistStore.KIND_XTREAM -> Strings.t.playlistTypeXtream
                                        PlaylistStore.KIND_LOCAL -> Strings.t.playlistTypeLocal
                                        else -> Strings.t.playlistTypeM3u
                                    },
                                    style = MaterialTheme.typography.labelSmall,
                                    color = DimText,
                                )
                            }
                            TextButton(onClick = { onEdit(p) }) { Text(Strings.t.editPlaylist) }
                            IconButton(onClick = { confirmDelete = p }) {
                                Icon(
                                    Icons.Filled.Close,
                                    contentDescription = Strings.t.remove,
                                    tint = DimText,
                                )
                            }
                        }
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onAdd) { Text(Strings.t.addPlaylist) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(Strings.t.cancel) } },
    )
}
