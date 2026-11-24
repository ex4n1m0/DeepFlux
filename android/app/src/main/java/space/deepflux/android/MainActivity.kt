package space.deepflux.android

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.material3.windowsizeclass.calculateWindowSizeClass
import androidx.core.content.ContextCompat
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.engine.isActive
import space.deepflux.android.ui.AppRoot
import space.deepflux.android.ui.theme.DeepFluxTheme

class MainActivity : ComponentActivity() {

    private val repository: TorrentRepository
        get() = (application as DeepFluxApp).repository

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* denial is fine, FGS still runs */ }

    // The folder finished downloads are copied to (v1.2 picker; the
    // destination itself since 2026-11-24).
    private val folderPicker =
        registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
            if (uri != null) {
                // Keep the app's access across launches. The system can refuse
                // (no keepable grant was handed out, or the app holds too
                // many) — then the folder is no use as a destination.
                repository.pickedFinishedDownloadsFolder(
                    uri,
                    space.deepflux.android.dlmgr.FinishedExport.keepAccess(this, uri),
                )
            }
        }

    @OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        maybeRequestNotificationPermission()
        // A recreated activity (rotation, process death) is handed its
        // ORIGINAL launch intent again. Handling it a second time re-raised
        // the add prompt for a magnet the user had already answered.
        if (savedInstanceState == null) handleIntent(intent)
        repository.pickFolder = { folderPicker.launch(null) }
        // Restore the persisted UI language BEFORE the first frame so a
        // Chinese-locale user never sees an English flash at boot.
        space.deepflux.android.ui.Strings.setLanguage(repository.loadLanguageIsChinese())
        setContent {
            DeepFluxTheme {
                val windowSizeClass: WindowSizeClass = calculateWindowSizeClass(this)
                AppRoot(
                    windowSizeClass = windowSizeClass,
                    repository = repository,
                )
            }
        }
    }

    override fun onStart() {
        super.onStart()
        (application as DeepFluxApp).onAppVisibilityChanged?.invoke(true)
        // Foregrounded UI with live transfers → (re)assert the FGS legally.
        // Both lanes: torrents that are active, and HTTP downloads running
        // or waiting for a slot (the activity starts before the repository's
        // first snapshot exists; its own edge-trigger covers that case).
        val app = application as DeepFluxApp
        if (repository.torrents.value.any { it.isActive } ||
            app.httpDownloads.items.value.any { it.state == space.deepflux.android.dlmgr.HttpState.RUNNING || it.state == space.deepflux.android.dlmgr.HttpState.QUEUED }
        ) {
            repository.ensureService()
        }
        // A "Resume all" notice from an earlier process carries an old token: re-arm it.
        app.syncPausedNotice()
        // Android's background time limit paused everything
        // (TorrentService.onTimeout): the user is back — pick up where it stopped.
        if (app.transfersPausedBySystemLimit) resumeAllTransfers()
    }

    override fun onStop() {
        super.onStop()
        (application as DeepFluxApp).onAppVisibilityChanged?.invoke(false)
        // Persist the artwork cover cache (§14 P1): the resolver debounces
        // too, but a backgrounded process can be killed at any moment now.
        (application as DeepFluxApp).artwork.flushNow()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        // What a later recreation must NOT handle again is this one, not the stale launch intent.
        setIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        if (intent == null) return
        val action = intent.action
        if (action == Intent.ACTION_VIEW || action == Intent.ACTION_SEND) {
            repository.addFromIntent(intent)
        }
        // Notification routing (fix 2026-11-23): download notifications open
        // the DOWNLOADS tab (they used to land on the boot tab), and the
        // notification's "Resume all" also resumes what "Pause all" paused.
        // The tab switch is harmless from anyone; the ACTION only counts
        // with our secret (TorrentService.isResumeAllRequest) — this activity
        // is exported, and any app could otherwise resume the user's paused
        // downloads by intent.
        val tab = intent.getStringExtra(space.deepflux.android.engine.TorrentRepository.EXTRA_OPEN_TAB)
        if (!tab.isNullOrBlank()) repository.requestTab(tab)
        if (space.deepflux.android.service.TorrentService.isResumeAllRequest(this, intent)) resumeAllTransfers()
    }

    private fun resumeAllTransfers() {
        val app = application as DeepFluxApp
        app.transfersPausedBySystemLimit = false
        repository.resumeAll()
        app.httpDownloads.resumeAll()
        app.syncPausedNotice() // the standing "paused" notice has done its job
    }

    private fun maybeRequestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
}
