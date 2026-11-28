package space.deepflux.android.ui.chat

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.AlertDialog
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import space.deepflux.android.agent.SessionStore
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.Card
import space.deepflux.android.ui.theme.DimText
import space.deepflux.android.ui.theme.ErrorRed

/**
 * The list of saved tasks (1.4.16) — the desktop Agent tab's left sidebar, as a panel over
 * the chat: a name over a dim preview, newest first; a tap reopens the task; "⋯" on a row
 * offers Rename and Delete; "New task" starts an empty one. The actions are LABELED (owner,
 * the desktop's sidebar: icon-only buttons "read as mystery glyphs").
 *
 * While a reply is running, a task cannot be opened, renamed or deleted — a turn must never
 * write its reply into another task. "New task" still works: it stops the turn, as the
 * clear button always did, and the task as it stands is kept.
 *
 * Pure view: the session (ChatSession) owns the tasks and says what happened to them.
 */
@Composable
internal fun TasksPanel(
    tasks: List<SessionStore.Meta>,
    openTask: String?,
    busy: Boolean,
    onOpen: (String) -> Unit,
    onRename: (id: String, name: String) -> Unit,
    onDelete: (String) -> Unit,
    onNew: () -> Unit,
    onClose: () -> Unit,
) {
    var rename by remember { mutableStateOf<SessionStore.Meta?>(null) }
    var name by remember { mutableStateOf("") }
    var delete by remember { mutableStateOf<SessionStore.Meta?>(null) }

    // The scrim: a tap outside closes. The panel itself takes its taps, so one on its empty
    // space does not close it.
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black.copy(alpha = 0.6f))
            .clickable(onClick = onClose),
    ) {
        Surface(
            color = Card,
            modifier = Modifier
                .align(Alignment.CenterStart)
                .fillMaxHeight()
                .fillMaxWidth(0.86f)
                .widthIn(max = 360.dp)
                .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null) {},
        ) {
            Column(modifier = Modifier.fillMaxSize().statusBarsPadding().imePadding()) {
                Row(
                    modifier = Modifier.fillMaxWidth().padding(start = 16.dp, end = 4.dp, top = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(Strings.t.tasksTitle, style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                    IconButton(onClick = onClose) {
                        Icon(Icons.Filled.Close, contentDescription = Strings.t.close, tint = DimText)
                    }
                }
                TextButton(onClick = onNew, modifier = Modifier.padding(start = 8.dp, end = 8.dp)) {
                    Text(Strings.t.newTask, color = Accent)
                }
                if (busy) {
                    Text(
                        Strings.t.tasksBusy,
                        style = MaterialTheme.typography.labelSmall,
                        color = DimText,
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
                    )
                }
                if (tasks.isEmpty()) {
                    Text(
                        Strings.t.tasksEmpty,
                        style = MaterialTheme.typography.bodySmall,
                        color = DimText,
                        modifier = Modifier.padding(16.dp),
                    )
                }
                LazyColumn(modifier = Modifier.weight(1f).fillMaxWidth()) {
                    items(tasks, key = { it.id }) { task ->
                        TaskRow(task, current = task.id == openTask, enabled = !busy, onOpen = onOpen, onRename = { rename = it; name = it.name }, onDelete = { delete = it })
                        HorizontalDivider(color = MaterialTheme.colorScheme.outline, thickness = 0.5.dp)
                    }
                }
            }
        }
    }

    rename?.let { task ->
        AlertDialog(
            onDismissRequest = { rename = null },
            title = { Text(Strings.t.renameTask) },
            text = {
                OutlinedTextField(
                    value = name,
                    onValueChange = { name = it },
                    label = { Text(Strings.t.taskNameLabel) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
            },
            confirmButton = {
                TextButton(enabled = name.isNotBlank(), onClick = { onRename(task.id, name); rename = null }) {
                    Text(Strings.t.save)
                }
            },
            dismissButton = { TextButton(onClick = { rename = null }) { Text(Strings.t.cancel) } },
        )
    }

    delete?.let { task ->
        AlertDialog(
            onDismissRequest = { delete = null },
            title = { Text(Strings.t.deleteTaskTitleFmt.format(task.name)) },
            text = { Text(Strings.t.deleteTaskMsg) },
            confirmButton = {
                TextButton(onClick = { onDelete(task.id); delete = null }) {
                    Text(Strings.t.deleteTask, color = ErrorRed)
                }
            },
            dismissButton = { TextButton(onClick = { delete = null }) { Text(Strings.t.cancel) } },
        )
    }
}

/** One saved task: its name over a dim preview; tap to reopen, "⋯" for Rename and Delete. */
@Composable
private fun TaskRow(
    task: SessionStore.Meta,
    current: Boolean,
    enabled: Boolean,
    onOpen: (String) -> Unit,
    onRename: (SessionStore.Meta) -> Unit,
    onDelete: (SessionStore.Meta) -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(min = 56.dp)
            .background(if (current) Accent.copy(alpha = 0.12f) else Color.Transparent)
            .clickable(enabled = enabled) { onOpen(task.id) }
            .padding(start = 16.dp, top = 8.dp, bottom = 8.dp, end = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                task.name.ifBlank { task.preview },
                style = MaterialTheme.typography.bodyMedium,
                color = if (current) Accent else Color.Unspecified,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (task.preview.isNotBlank() && task.preview != task.name) {
                Text(
                    task.preview,
                    style = MaterialTheme.typography.labelSmall,
                    color = DimText,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
        var menu by remember { mutableStateOf(false) }
        Box {
            IconButton(onClick = { menu = true }, enabled = enabled) {
                Icon(Icons.Filled.MoreVert, contentDescription = Strings.t.taskActions, tint = DimText)
            }
            DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                DropdownMenuItem(
                    text = { Text(Strings.t.renameTask) },
                    onClick = { menu = false; onRename(task) },
                )
                DropdownMenuItem(
                    text = { Text(Strings.t.deleteTask, color = ErrorRed) },
                    onClick = { menu = false; onDelete(task) },
                )
            }
        }
    }
}
