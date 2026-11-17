package space.deepflux.android.ui.room

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
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
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import space.deepflux.android.room.OhRoom
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.Card
import space.deepflux.android.ui.theme.DimText
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The chat room (plan §13 #17 — the OnlyHumans room, Android port; v1.3
 * layout pass): join card matches the app's setup cards, the header uses
 * member chips + host badge, messages align like a chat (own right / others
 * left, capped width) and the composer mirrors the Agent screen's send row.
 * Behavior + visual identity unchanged — spacing, alignment and hierarchy
 * only.
 */
@Composable
fun RoomScreen(room: OhRoom) {
    val state by room.state.collectAsState()
    val (savedNick, savedWord) = remember { room.loadConfig() }
    var nick by remember { mutableStateOf(savedNick) }
    var word by remember { mutableStateOf(savedWord) }
    var input by remember { mutableStateOf("") }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .imePadding()
            .padding(horizontal = 12.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        if (state.role == OhRoom.Role.LEFT) {
            // ---- join card (centered, same shape as the app's setup cards) ----
            Box(modifier = Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                Surface(
                    shape = RoundedCornerShape(14.dp),
                    color = Card,
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp),
                ) {
                    Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(Strings.t.roomTab, style = MaterialTheme.typography.titleLarge)
                        Text(
                            Strings.t.roomIntro,
                            style = MaterialTheme.typography.bodySmall,
                            color = DimText,
                        )
                        OutlinedTextField(
                            value = nick,
                            onValueChange = { nick = it },
                            label = { Text(Strings.t.roomNick) },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        OutlinedTextField(
                            value = word,
                            onValueChange = { word = it },
                            label = { Text(Strings.t.roomWord) },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                            TextButton(enabled = word.isNotBlank(), onClick = { room.join(nick, word) }) {
                                Text(Strings.t.joinRoom)
                            }
                        }
                    }
                }
            }
        } else {
            // ---- room header ----
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        state.word + (if (state.hosting) " · ${Strings.t.roomHost}" else ""),
                        style = MaterialTheme.typography.titleSmall,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    MemberChips(state.members, state.sealedRoom)
                }
                if (state.hosting && state.role == OhRoom.Role.ACTIVE) {
                    IconButton(onClick = { room.rotate() }) {
                        Text("🔒", style = MaterialTheme.typography.titleMedium)
                    }
                }
                TextButton(onClick = { room.leave() }) { Text(Strings.t.leaveRoom) }
            }
            if (state.role == OhRoom.Role.CONNECTING) {
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth().height(2.dp))
                Text(
                    Strings.t.roomConnecting,
                    style = MaterialTheme.typography.labelSmall,
                    color = DimText,
                )
            }
            if (state.note.isNotBlank()) {
                Text(
                    state.note,
                    style = MaterialTheme.typography.labelSmall,
                    color = Accent,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }

            // ---- transcript ----
            val listState = rememberLazyListState()
            LaunchedEffect(state.messages.size) {
                if (state.messages.isNotEmpty()) listState.animateScrollToItem(state.messages.size - 1)
            }
            LazyColumn(
                state = listState,
                modifier = Modifier.weight(1f).fillMaxWidth(),
                contentPadding = PaddingValues(vertical = 4.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                items(state.messages, key = { "${it.ts}-${it.nick}-${it.text.hashCode()}" }) { m ->
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = if (m.own) Arrangement.End else Arrangement.Start,
                    ) {
                        Surface(
                            color = if (m.own) Accent.copy(alpha = 0.18f) else Card,
                            shape = RoundedCornerShape(12.dp),
                            modifier = Modifier.widthIn(max = 320.dp),
                        ) {
                            Column(modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp)) {
                                Text(
                                    "${m.nick} · ${SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(m.ts))}",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = DimText,
                                )
                                Text(m.text, style = MaterialTheme.typography.bodyMedium)
                            }
                        }
                    }
                }
            }

            // ---- composer (mirrors the Agent screen's send row) ----
            if (state.role == OhRoom.Role.ACTIVE) {
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                    OutlinedTextField(
                        value = input,
                        onValueChange = { input = it },
                        placeholder = { Text(Strings.t.roomMessageHint) },
                        singleLine = true,
                        modifier = Modifier.weight(1f),
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                        keyboardActions = KeyboardActions(onSend = {
                            if (room.sendMessage(input)) input = ""
                        }),
                    )
                    Spacer(Modifier.width(8.dp))
                    IconButton(
                        enabled = input.isNotBlank(),
                        onClick = {
                            if (room.sendMessage(input)) input = ""
                        },
                    ) {
                        Icon(Icons.AutoMirrored.Filled.Send, contentDescription = Strings.t.send, tint = Accent)
                    }
                }
            }
        }
    }
}

/** Members as compact chips (was: one long comma-joined line). */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun MemberChips(members: List<String>, sealedRoom: Boolean) {
    if (members.isEmpty()) return
    FlowRow(
        modifier = Modifier.fillMaxWidth().padding(top = 2.dp),
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        members.take(12).forEach { member ->
            Surface(
                shape = RoundedCornerShape(8.dp),
                color = Card,
            ) {
                Text(
                    (if (sealedRoom) "🔒 " else "") + member,
                    style = MaterialTheme.typography.labelSmall,
                    color = DimText,
                    modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
                )
            }
        }
        if (members.size > 12) {
            Text(
                "+${members.size - 12}",
                style = MaterialTheme.typography.labelSmall,
                color = DimText,
                modifier = Modifier.padding(horizontal = 4.dp),
            )
        }
    }
}
