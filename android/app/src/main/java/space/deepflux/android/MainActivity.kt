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

    // SAF folder picker result (v1.2)
    private val folderPicker =
        registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
            if (uri != null) {
                // Persist the permission across reboots
                contentResolver.takePersistableUriPermission(
                    uri,
                    android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION or
                        android.content.Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
                )
                repository.setCustomSaveDir(uri.toString())
            }
        }

    @OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        maybeRequestNotificationPermission()
        handleIntent(intent)
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
        // Foregrounded UI with active torrents → (re)assert the FGS legally.
        if (repository.torrents.value.any { it.isActive }) repository.ensureService()
    }

    override fun onStop() {
        super.onStop()
        // Persist the artwork cover cache (§14 P1): the resolver debounces
        // too, but a backgrounded process can be killed at any moment now.
        (application as DeepFluxApp).artwork.flushNow()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        val action = intent?.action ?: return
        if (action == Intent.ACTION_VIEW || action == Intent.ACTION_SEND) {
            repository.addFromIntent(intent)
        }
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
