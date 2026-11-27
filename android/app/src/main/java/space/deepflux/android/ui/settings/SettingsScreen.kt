package space.deepflux.android.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
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
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.theme.Accent
import space.deepflux.android.ui.theme.DimText

@Composable
fun SettingsScreen(repository: TorrentRepository) {
    val prefs by repository.prefs.collectAsStateWithLifecycle()
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
                // The chosen folder is where finished downloads are copied to.
                // The system or the user can take the app's access to it away —
                // then say so; the copies go to Downloads until it is chosen again.
                val context = androidx.compose.ui.platform.LocalContext.current
                val folderUri = prefs.customSaveDirUri
                // Asking the system is a call to another process: once per
                // folder, not once per recomposition of this screen.
                val folderUsable = remember(folderUri) {
                    folderUri.isBlank() ||
                        space.deepflux.android.dlmgr.FinishedExport.canWriteTo(context, android.net.Uri.parse(folderUri))
                }
                val folderName = remember(folderUri) {
                    if (folderUri.isBlank()) "" else
                        space.deepflux.android.filecmd.CommanderRepository.pickedFolderName(android.net.Uri.parse(folderUri))
                }
                val hasDownloadsCollection = remember { android.os.Build.VERSION.SDK_INT >= 29 }
                Text(
                    when {
                        // Android 9 has no public Downloads collection: without a chosen folder there is nothing to copy to.
                        folderUri.isBlank() && !hasDownloadsCollection -> Strings.t.saveLocationNoDownloads
                        folderUri.isBlank() -> Strings.t.saveLocationDefault
                        !folderUsable -> Strings.t.saveLocationLost
                        else -> Strings.t.saveLocationCustomFmt.format(folderName)
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = if (folderUsable) DimText else space.deepflux.android.ui.theme.WarnYellow,
                )
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    if (folderUri.isNotBlank()) {
                        TextButton(onClick = { repository.clearCustomSaveDir() }) { Text(Strings.t.useDefaultFolder) }
                    }
                    TextButton(onClick = { repository.pickFolder?.invoke() }) {
                        Text(if (folderUri.isBlank()) Strings.t.chooseFolder else Strings.t.changeFolder)
                    }
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
            // 1.4.16: which AI, which model, how hard it thinks, and the key — a card of
            // its own now (it used to be one key field, and the chat always talked to Venice).
            AiAgentCard(repository, prefs)
            CompactCard(null) {
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
                if (updater.installedByPlayStore) {
                    // Play delivers the updates for a Play install; the
                    // website APK could never install over it.
                    Text(Strings.t.updateViaPlay, style = MaterialTheme.typography.bodySmall, color = DimText)
                } else {
                    SwitchRow(
                        title = Strings.t.updateAutoCheck,
                        subtitle = "",
                        checked = prefs.updatesAutoCheck,
                        last = true,
                        onChange = { repository.setUpdatesAutoCheck(it) },
                    )
                    TextButton(onClick = {
                        // Says what actually happened: a FAILED check used to
                        // be reported as "up to date".
                        updater.checkNow { result ->
                            when (result) {
                                // AppRoot watches `available` and opens the dialog wherever the user is.
                                is space.deepflux.android.data.CheckResult.Available -> Unit
                                space.deepflux.android.data.CheckResult.UpToDate ->
                                    repository.postNotice(Strings.t.upToDate)
                                is space.deepflux.android.data.CheckResult.Failed ->
                                    repository.postNotice(Strings.t.updateCheckFailedFmt.format(result.reason))
                                space.deepflux.android.data.CheckResult.ManagedByStore -> Unit
                            }
                        }
                    }) {
                        Text(Strings.t.updateCheckNow, color = Accent)
                    }
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
                // 1.4.15: the app ships LGPL libraries (FFmpeg's audio decoders, VLC's
                // engine). Their licence and where their source is — verbatim from an
                // asset, so the licence text is never retyped.
                val context = androidx.compose.ui.platform.LocalContext.current
                var showLicences by remember { mutableStateOf(false) }
                if (showLicences) {
                    val licences = remember {
                        runCatching { context.assets.open("licenses.txt").bufferedReader().use { it.readText() } }
                            .getOrDefault("")
                    }
                    androidx.compose.material3.AlertDialog(
                        onDismissRequest = { showLicences = false },
                        title = { Text(Strings.t.openSourceLicences) },
                        text = {
                            Text(
                                licences,
                                style = MaterialTheme.typography.bodySmall,
                                modifier = Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState()),
                            )
                        },
                        confirmButton = { TextButton(onClick = { showLicences = false }) { Text(Strings.t.ok) } },
                    )
                }
                TextButton(onClick = { showLicences = true }) {
                    Text(Strings.t.openSourceLicences, color = DimText, style = MaterialTheme.typography.labelMedium)
                }
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
internal fun CompactCard(title: String?, content: @Composable () -> Unit) {
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
internal fun SwitchRow(
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
                                    // 1.4.16: the AI provider settings (older exports carry none of them).
                                    "llm_provider" -> repository.setLlmProvider(value)
                                    "llm_base_url" -> repository.setLlmBaseUrl(value)
                                    "llm_model" -> repository.setLlmModel(value)
                                    "llm_effort" -> repository.setLlmEffort(value)
                                    "llm_custom_sends_effort" -> repository.setLlmCustomSendsEffort(value.toBooleanStrict())
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
