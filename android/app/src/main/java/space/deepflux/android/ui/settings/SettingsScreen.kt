package space.deepflux.android.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
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
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import space.deepflux.android.BuildConfig
import space.deepflux.android.data.EmbeddedKey
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.DimText
import space.deepflux.android.ui.theme.OkGreen

@Composable
fun SettingsScreen(repository: TorrentRepository) {
    val prefs by repository.prefs.collectAsStateWithLifecycle()
    var customKey by remember(prefs.customApiKey) { mutableStateOf(prefs.customApiKey) }
    var homepage by remember(prefs.homepage) { mutableStateOf(prefs.homepage) }

    // Grouped + compact (v1.3): section headers between cards, rows without
    // dead space, and on wide screens the column is capped so cards don't
    // stretch into unreadable 1200dp-wide slabs.
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .imePadding()
            .padding(horizontal = 14.dp, vertical = 10.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Column(
            modifier = Modifier.fillMaxWidth().widthIn(max = 640.dp).align(Alignment.CenterHorizontally),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            // Version line FIRST (owner 2026-11-15): read the running build
            // without scrolling to About.
            Text(
                "DeepFlux v${BuildConfig.VERSION_NAME} · Android",
                style = MaterialTheme.typography.labelSmall,
                color = DimText,
                modifier = Modifier.padding(start = 4.dp),
            )

            // ================= Device mode =================
            CompactCard(null) {
                Text(Strings.t.uiModeTitle, style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.height(6.dp))
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                    listOf(
                        "tv" to Strings.t.uiModeTv,
                        "mobile" to Strings.t.uiModeMobile,
                        "tablet" to Strings.t.uiModeTablet,
                    ).forEach { (id, label) ->
                        TextButton(onClick = { repository.setUiMode(id) }) {
                            Text(
                                (if (prefs.uiMode == id) "● " else "") + label,
                                color = if (prefs.uiMode == id) Accent else DimText,
                                style = MaterialTheme.typography.labelMedium,
                            )
                        }
                    }
                }
            }

            // ================= Downloads & storage =================
            SectionLabel(Strings.t.storageSection)
            CompactCard(null) {
                // Save location
                Text(Strings.t.saveLocation, style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.height(4.dp))
                Text(
                    if (prefs.customSaveDirUri.isNotBlank()) Strings.t.saveLocationCustom
                    else Strings.t.saveLocationDefault,
                    style = MaterialTheme.typography.bodySmall,
                    color = DimText,
                )
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(onClick = { repository.pickFolder?.invoke() }) { Text(Strings.t.chooseFolder) }
                }
                HorizontalDivider(color = MaterialTheme.colorScheme.outline, thickness = 0.5.dp)
                Spacer(Modifier.height(4.dp))
                SwitchRow(Strings.t.wifiOnly, Strings.t.wifiOnlyDesc, prefs.wifiOnly) { repository.setWifiOnly(it) }
                SwitchRow(Strings.t.chargingOnly, Strings.t.chargingOnlyDesc, prefs.chargingOnly) { repository.setChargingOnly(it) }
                SwitchRow(Strings.t.sequentialDefault, Strings.t.sequentialDesc, prefs.sequentialDefault, last = true) { repository.setSequentialDefault(it) }
            }

            // ================= Browser =================
            SectionLabel(Strings.t.browserSection)
            CompactCard(null) {
                OutlinedTextField(
                    value = homepage,
                    onValueChange = { homepage = it },
                    label = { Text(Strings.t.homepageLabel) },
                    supportingText = { Text(Strings.t.homepageDesc, style = MaterialTheme.typography.labelSmall) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(
                        enabled = homepage.trim() != prefs.homepage,
                        onClick = { repository.setHomepage(homepage) },
                    ) { Text(Strings.t.save) }
                }
            }

            // ================= AI & services =================
            SectionLabel(Strings.t.servicesSection)
            CompactCard(null) {
                // AI key
                val embedded = remember { EmbeddedKey.hasEmbedded() }
                Text(Strings.t.aiAgentKey, style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.height(4.dp))
                Text(
                    if (embedded) Strings.t.builtInKey
                    else Strings.t.noBuiltInKey,
                    style = MaterialTheme.typography.bodySmall,
                    color = if (embedded) OkGreen else DimText,
                )
                Spacer(Modifier.height(6.dp))
                OutlinedTextField(
                    value = customKey,
                    onValueChange = { customKey = it },
                    label = { Text(Strings.t.customKey) },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    modifier = Modifier.fillMaxWidth(),
                )
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(onClick = { repository.setCustomApiKey(customKey) }) {
                        Text(Strings.t.saveKey)
                    }
                }
                HorizontalDivider(color = MaterialTheme.colorScheme.outline, thickness = 0.5.dp)
                Spacer(Modifier.height(8.dp))
                // Subtitles (plan §13 #8): optional OpenSubtitles consumer key
                var osKey by remember(prefs.opensubtitlesApiKey) { mutableStateOf(prefs.opensubtitlesApiKey) }
                Text(Strings.t.subtitlesSection, style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.height(4.dp))
                Text(
                    Strings.t.subtitlesHint,
                    style = MaterialTheme.typography.bodySmall,
                    color = DimText,
                )
                Spacer(Modifier.height(6.dp))
                OutlinedTextField(
                    value = osKey,
                    onValueChange = { osKey = it },
                    label = { Text(Strings.t.apiKey) },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    modifier = Modifier.fillMaxWidth(),
                )
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(onClick = { repository.setOpensubtitlesApiKey(osKey) }) { Text(Strings.t.save) }
                }
            }

            // ================= Backup (plan §13 #6) =================
            BackupCard(repository)

            // ================= Updates (§13 #7) =================
            val appCtx = androidx.compose.ui.platform.LocalContext.current.applicationContext
            val updater = (appCtx as space.deepflux.android.DeepFluxApp).updater
            CompactCard(null) {
                SwitchRow(
                    title = Strings.t.updateAutoCheck,
                    subtitle = "",
                    checked = prefs.updatesAutoCheck,
                    last = true,
                    onChange = { repository.setUpdatesAutoCheck(it) },
                )
                TextButton(onClick = {
                    updater.checkNow { info ->
                        // Non-null also sets `available` → AppRoot opens the
                        // update dialog wherever the user is.
                        if (info == null) repository.postNotice(Strings.t.upToDate)
                    }
                }) {
                    Text(Strings.t.updateCheckNow, color = Accent)
                }
            }

            // ================= Language =================
            CompactCard(null) {
                Text(Strings.t.languageTitle, style = MaterialTheme.typography.titleSmall)
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceEvenly,
                ) {
                    TextButton(onClick = {
                        Strings.setLanguage(false)
                        repository.setLanguage(false)
                    }) {
                        Text("English", color = if (!Strings.isChinese) Accent else DimText)
                    }
                    TextButton(onClick = {
                        Strings.setLanguage(true)
                        repository.setLanguage(true)
                    }) {
                        Text("中文", color = if (Strings.isChinese) Accent else DimText)
                    }
                }
            }

            // ================= About =================
            CompactCard(null) {
                Text("DeepFlux ${BuildConfig.VERSION_NAME}", style = MaterialTheme.typography.titleSmall)
                Text(
                    "libtorrent ${repository.engineVersion()} · " + Strings.t.androidTwin,
                    style = MaterialTheme.typography.labelMedium,
                    color = DimText,
                )
            }
        }
    }
}

/** Small dim section caption — the grouping layer the flat list lacked. */
@Composable
private fun SectionLabel(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.labelMedium,
        color = DimText,
        modifier = Modifier.padding(start = 4.dp, top = 4.dp),
    )
}

@Composable
private fun CompactCard(title: String?, content: @Composable () -> Unit) {
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            if (title != null) {
                Text(title, style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.height(6.dp))
            }
            content()
        }
    }
}

@Composable
private fun SwitchRow(
    title: String,
    subtitle: String,
    checked: Boolean,
    last: Boolean = false,
    onChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, style = MaterialTheme.typography.labelMedium, color = DimText)
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
    if (!last) HorizontalDivider(color = MaterialTheme.colorScheme.outline, thickness = 0.5.dp)
}

/**
 * Encrypted settings export/import (plan §13 #6) — SAF pickers around the
 * desktop-compatible .dfc format. Import applies prefs + sidecars and
 * offers a restart (every store reloads on process start; the desktop app
 * does the same restart-after-import dance).
 */
@Composable
private fun BackupCard(repository: space.deepflux.android.engine.TorrentRepository) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    var isExport by remember { mutableStateOf(true) }
    var showPass by remember { mutableStateOf(false) }
    var exportUri by remember { mutableStateOf<android.net.Uri?>(null) }
    var importUri by remember { mutableStateOf<android.net.Uri?>(null) }
    var status by remember { mutableStateOf("") }
    var askRestart by remember { mutableStateOf(false) }

    val exportLauncher = androidx.activity.compose.rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.CreateDocument("application/octet-stream"),
    ) { uri ->
        if (uri != null) { exportUri = uri; isExport = true; showPass = true }
    }
    val importLauncher = androidx.activity.compose.rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.OpenDocument(),
    ) { uri ->
        if (uri != null) { importUri = uri; isExport = false; showPass = true }
    }

    CompactCard(Strings.t.backupTitle) {
        Text(
            Strings.t.backupHint,
            style = MaterialTheme.typography.bodySmall,
            color = DimText,
        )
        Spacer(Modifier.height(6.dp))
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            TextButton(onClick = {
                status = ""
                exportLauncher.launch("deepflux-settings.dfc")
            }) { Text(Strings.t.exportSettings) }
            TextButton(onClick = {
                status = ""
                importLauncher.launch(arrayOf("*/*"))
            }) { Text(Strings.t.importSettings) }
        }
        if (status.isNotBlank()) {
            Text(status, style = MaterialTheme.typography.bodySmall, color = Accent)
        }
    }

    if (showPass) {
        PassphraseDialog(
            requireRepeat = isExport,
            onCancel = { showPass = false },
            onConfirm = { pass ->
                showPass = false
                val outUri = exportUri
                val inUri = importUri
                Thread({
                    try {
                        if (isExport && outUri != null) {
                            val payload = space.deepflux.android.data.SettingsBackup.buildPayload(
                                repository.prefs.value, ctx.filesDir,
                            )
                            val bytes = space.deepflux.android.data.SettingsBackup.encrypt(payload, pass)
                            ctx.contentResolver.openOutputStream(outUri, "wt")?.use { it.write(bytes) }
                            status = Strings.t.exportDoneFmt.format((bytes.size / 1024).coerceAtLeast(1))
                        } else if (!isExport && inUri != null) {
                            val blob = ctx.contentResolver.openInputStream(inUri)?.use { it.readBytes() }
                                ?: throw space.deepflux.android.data.SettingsBackup.BackupException("could not read the file")
                            val payload = space.deepflux.android.data.SettingsBackup.decrypt(blob, pass)
                            val summary = space.deepflux.android.data.SettingsBackup.applyFilesAndPrefs(
                                payload, ctx.filesDir,
                            ) { key, value ->
                                when (key) {
                                    "custom_api_key" -> repository.setCustomApiKey(value)
                                    "opensubtitles_api_key" -> repository.setOpensubtitlesApiKey(value)
                                    "language" -> repository.setLanguage(value == "zh")
                                    "wifi_only" -> repository.setWifiOnly(value.toBooleanStrict())
                                    "charging_only" -> repository.setChargingOnly(value.toBooleanStrict())
                                    "sequential_default" -> repository.setSequentialDefault(value.toBooleanStrict())
                                    else -> {} // jackett_* from old exports: feature removed
                                }
                            }
                            status = summary
                            askRestart = true
                        }
                    } catch (e: space.deepflux.android.data.SettingsBackup.BackupException) {
                        status = e.message ?: "backup failed"
                    } catch (e: Exception) {
                        status = "backup failed: ${e.message}"
                    }
                }, "settings-backup").apply { isDaemon = true }.start()
            },
        )
    }

    if (askRestart) {
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { askRestart = false },
            title = { Text(Strings.t.importSettings) },
            text = { Text(Strings.t.importRestartMsg) },
            confirmButton = {
                TextButton(onClick = {
                    askRestart = false
                    val intent = android.content.Intent(ctx, space.deepflux.android.MainActivity::class.java)
                        .addFlags(
                            android.content.Intent.FLAG_ACTIVITY_NEW_TASK or
                                android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK,
                        )
                    ctx.startActivity(intent)
                    Runtime.getRuntime().exit(0)
                }) { Text(Strings.t.restartToApply) }
            },
            dismissButton = { TextButton(onClick = { askRestart = false }) { Text(Strings.t.cancel) } },
        )
    }
}

@Composable
private fun PassphraseDialog(
    requireRepeat: Boolean,
    onCancel: () -> Unit,
    onConfirm: (String) -> Unit,
) {
    var pass by remember { mutableStateOf("") }
    var pass2 by remember { mutableStateOf("") }
    var mismatch by remember { mutableStateOf(false) }
    androidx.compose.material3.AlertDialog(
        onDismissRequest = onCancel,
        title = { Text(Strings.t.passphraseLabel) },
        text = {
            Column {
                OutlinedTextField(
                    value = pass,
                    onValueChange = { pass = it; mismatch = false },
                    label = { Text(Strings.t.passphraseLabel) },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    modifier = Modifier.fillMaxWidth(),
                )
                if (requireRepeat) {
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = pass2,
                        onValueChange = { pass2 = it; mismatch = false },
                        label = { Text(Strings.t.passphraseAgainLabel) },
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                if (mismatch) {
                    Spacer(Modifier.height(4.dp))
                    Text(Strings.t.passphraseMismatch, style = MaterialTheme.typography.bodySmall, color = androidx.compose.ui.graphics.Color(0xFFFF6B6B))
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = pass.length >= 4 && (!requireRepeat || pass2.isNotEmpty()),
                onClick = {
                    if (!requireRepeat || pass == pass2) onConfirm(pass) else mismatch = true
                },
            ) { Text(Strings.t.ok) }
        },
        dismissButton = { TextButton(onClick = onCancel) { Text(Strings.t.cancel) } },
    )
}
