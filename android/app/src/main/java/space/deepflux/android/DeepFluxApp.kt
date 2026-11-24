package space.deepflux.android

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import space.deepflux.android.engine.TorrentRepository

class DeepFluxApp : Application() {

    lateinit var repository: TorrentRepository
        private set

    /**
     * ONE HTTP download engine per PROCESS. It used to be created by the UI
     * (`remember` in AppRoot), so leaving the app with Back and reopening it
     * built a second engine while the first kept running — and both could
     * start a worker on the same unfinished file (two writers on one
     * `.part`, found 2026-11-14). The Application outlives every activity.
     */
    lateinit var httpDownloads: space.deepflux.android.dlmgr.HttpDownloadRepository
        private set

    /**
     * RSS feeds + auto-download (plan §13 #4). An app singleton like the
     * engines: the TorrentService poll loop, the Downloads UI and the
     * agent tools all share ONE feed list.
     */
    lateinit var rss: space.deepflux.android.rss.RssRepository
        private set

    /**
     * Live-TV artwork (§14 P1): the cover/logo caches must survive tab
     * switches (AppRoot disposes IPTVScreen — per-screen instances
     * re-parsed everything on every visit) and the iptv-org index load
     * starts at boot so Live TV opens warm.
     */
    lateinit var artwork: space.deepflux.android.iptv.ArtworkResolver
        private set

    /**
     * In-app self-update (§13 #7): checks deepflux.space's feed, downloads
     * + sha256-verifies the APK, hands it to the system installer. App-scoped
     * so the startup check (AppRoot) and the Settings row share one state.
     */
    lateinit var updater: space.deepflux.android.data.AppUpdater
        private set

    /**
     * Android's background time limit paused every transfer
     * (TorrentService.onTimeout). In memory only: the next time the user
     * opens the app (MainActivity.onStart) everything is resumed and the flag
     * cleared. The pause itself is saved with the transfers, so if the
     * process dies first they stay paused and Downloads offers "Resume all".
     */
    var transfersPausedBySystemLimit = false
        internal set

    /**
     * Set by the UI while a browser exists: true when the whole app came to
     * the foreground, false when it left (MainActivity.onStart / onStop).
     * The browser stops its pages' JavaScript timers meanwhile.
     */
    var onAppVisibilityChanged: ((Boolean) -> Unit)? = null

    /**
     * Keeps the standing "paused" notice in step with the transfers
     * (TorrentService.syncPausedNotice): shown while transfers wait for
     * "Resume all" and no service notification carries that button.
     */
    fun syncPausedNotice() {
        space.deepflux.android.service.TorrentService.syncPausedNotice(
            this,
            wanted = space.deepflux.android.service.TorrentService.pausedNoticeWanted(
                anyPausedByPauseAll = repository.anyPausedByPauseAll() || httpDownloads.anyPausedByPauseAll(),
                serviceRunning = space.deepflux.android.service.TorrentService.running,
            ),
            bySystemLimit = transfersPausedBySystemLimit,
        )
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannels()
        repository = TorrentRepository(this)
        // start() only launches coroutines — engine/session work happens on
        // background dispatchers, so this never blocks app startup.
        repository.start()
        httpDownloads = space.deepflux.android.dlmgr.HttpDownloadRepository(
            context = this,
            saveDir = repository.downloadsDir,
            gateOk = { repository.networkGateOpen() },
            notify = { repository.postNotice(it) },
            // Finished files are copied into the folder chosen in Settings.
            exportFolder = { repository.finishedDownloadFolder() },
        )
        // The HTTP/HLS lane rides the SAME foreground service as torrents —
        // without it the OS may kill the process mid-download when the app
        // is backgrounded (polish pass 1, plan §13 #1).
        httpDownloads.onActiveChanged = { active ->
            if (active) repository.ensureService()
        }
        // Fix batch 2026-11-23 — the two lanes talk to each other through the
        // torrent repository:
        //  * its gate watcher tells the HTTP lane when Wi-Fi / charging is
        //    back, so gate-paused downloads resume (they stayed Paused);
        //  * finished/failed HTTP + HLS downloads raise a notification like
        //    torrents do (they were snackbar-only, unseen while the app is closed).
        repository.onGateOpened = { httpDownloads.onGateReopened() }
        // The standing "paused" notice follows "is something waiting for
        // Resume all?" — also when the last waiting torrent is removed by the
        // agent, where no screen re-syncs it.
        repository.onPauseAllChanged = { syncPausedNotice() }
        httpDownloads.onFinished = { name ->
            repository.postEventNotification(TorrentRepository.EventKind.FINISHED, name)
        }
        httpDownloads.onFailed = { name, reason ->
            repository.postEventNotification(TorrentRepository.EventKind.FAILED, name, reason)
        }
        httpDownloads.onCopyFailed = { name ->
            repository.postEventNotification(TorrentRepository.EventKind.COPY_FAILED, name)
        }
        rss = space.deepflux.android.rss.RssRepository(this, repository, httpDownloads)
        artwork = space.deepflux.android.iptv.ArtworkResolver(this)
        updater = space.deepflux.android.data.AppUpdater(
            context = this,
            settings = repository.appSettings,
            notify = { repository.postNotice(it) },
            // Same Wi-Fi / charging policy as every other download.
            networkGateOpen = { repository.networkGateOpen() },
        )
        // Heavy artwork loads (iptv-org index parse, cover disk cache) start
        // now, off the main thread — non-blocking for app startup.
        artwork.warmup()
        // Feed poll at process start (rate-limited inside); the TorrentService
        // loop keeps polling while downloads keep it alive.
        Thread({
            Thread.sleep(4_000) // let the engines finish booting first
            runCatching { rss.pollIfDue() }
        }, "rss-startup").apply { isDaemon = true }.start()
    }

    private fun createNotificationChannels() {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_PROGRESS,
                getString(R.string.channel_progress),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = getString(R.string.channel_progress_desc)
                setShowBadge(false)
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_EVENTS,
                getString(R.string.channel_events),
                NotificationManager.IMPORTANCE_DEFAULT,
            ).apply {
                description = getString(R.string.channel_events_desc)
            },
        )
    }

    companion object {
        const val CHANNEL_PROGRESS = "downloads"
        const val CHANNEL_EVENTS = "events"
    }
}
