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
        )
        // The HTTP/HLS lane rides the SAME foreground service as torrents —
        // without it the OS may kill the process mid-download when the app
        // is backgrounded (polish pass 1, plan §13 #1).
        httpDownloads.onActiveChanged = { active ->
            if (active) repository.ensureService()
        }
        rss = space.deepflux.android.rss.RssRepository(this, repository, httpDownloads)
        artwork = space.deepflux.android.iptv.ArtworkResolver(this)
        updater = space.deepflux.android.data.AppUpdater(
            context = this,
            settings = repository.appSettings,
            notify = { repository.postNotice(it) },
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
