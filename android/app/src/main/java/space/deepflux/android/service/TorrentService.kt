package space.deepflux.android.service

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import androidx.core.app.NotificationManagerCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import space.deepflux.android.DeepFluxApp
import space.deepflux.android.MainActivity
import space.deepflux.android.R
import space.deepflux.android.engine.TorrentItem
import space.deepflux.android.engine.isActive
import space.deepflux.android.util.Fmt

/**
 * Foreground service (dataSync) that keeps BOTH download lanes (torrents +
 * plain HTTP/HLS) alive while the UI is closed. It owns no engine state —
 * the repository singletons do — it only mirrors them into a progress
 * notification and stops itself after an idle grace period.
 *
 * Started only from user-gesture paths (Android 12+ background-FGS
 * restrictions); see TorrentRepository.ensureService() and
 * HttpDownloadRepository.onActiveChanged.
 */
class TorrentService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var idleJob: Job? = null
    private var lastNotify = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIFICATION_ID, buildNotification(emptyList(), emptyList()))
        val app = application as DeepFluxApp
        scope.launch {
            var lastTorrents = app.repository.torrents.value
            var lastHttp = app.httpDownloads.items.value
            // Mirror BOTH lanes into one notification; a change in either re-renders.
            launch {
                app.repository.torrents.collect {
                    lastTorrents = it
                    updateNotification(lastTorrents, lastHttp)
                    scheduleIdleStop(lastTorrents, lastHttp)
                }
            }
            app.httpDownloads.items.collect {
                lastHttp = it
                val now = System.currentTimeMillis()
                if (it.any { h -> h.state == space.deepflux.android.dlmgr.HttpState.RUNNING } || now - lastNotify > 1000) {
                    updateNotification(lastTorrents, lastHttp)
                    lastNotify = now
                }
                scheduleIdleStop(lastTorrents, lastHttp)
            }
        }
        // RSS poll loop (plan §13 #4): while the service is alive, feeds
        // refresh every 15 min and keyword matches auto-download. pollIfDue
        // is overlap-guarded + interval-limited, so a long-lived service
        // never stacks fetches.
        scope.launch {
            while (true) {
                kotlinx.coroutines.delay(RSS_POLL_MS)
                runCatching { (application as DeepFluxApp).rss.pollIfDue() }
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    /**
     * The Android 15 dataSync FGS cap (~6h/day cumulative) makes long
     * seeding on a phone a non-goal (study §5): stop the service after a
     * 20s grace period with nothing active in EITHER lane. Downloads pause
     * with the process when it later dies; state rehydrates on next start.
     */
    private fun scheduleIdleStop(items: List<TorrentItem>, http: List<space.deepflux.android.dlmgr.HttpDownload>) {
        val busy = items.any { it.isActive } ||
            http.any { it.state == space.deepflux.android.dlmgr.HttpState.RUNNING }
        if (!busy) {
            if (idleJob == null) {
                idleJob = scope.launch {
                    delay(IDLE_GRACE_MS)
                    val app = application as DeepFluxApp
                    val stillIdle = app.repository.torrents.value.none { it.isActive } &&
                        app.httpDownloads.items.value.none { it.state == space.deepflux.android.dlmgr.HttpState.RUNNING }
                    if (stillIdle) stopSelf()
                    idleJob = null
                }
            }
        } else {
            idleJob?.cancel()
            idleJob = null
        }
    }

    private fun buildNotification(
        items: List<TorrentItem>,
        http: List<space.deepflux.android.dlmgr.HttpDownload>,
    ): Notification {
        val contentIntent = PendingIntent.getActivity(
            this,
            1001, // unique request code so it does not collide with other PendingIntents
            Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val activeTorrents = items.filter { it.isActive }
        val activeHttp = http.filter {
            it.state == space.deepflux.android.dlmgr.HttpState.RUNNING ||
                it.state == space.deepflux.android.dlmgr.HttpState.QUEUED
        }
        val parts = ArrayList<String>()
        var progressSum = 0f
        var progressCount = 0
        if (activeTorrents.isNotEmpty()) {
            val avg = activeTorrents.map { it.progress }.average().toFloat()
            parts.add(
                getString(
                    R.string.notif_active, activeTorrents.size,
                    Fmt.speed(items.sumOf { it.downloadRate }), (avg * 100).toInt()
                )
            )
            progressSum += avg; progressCount++
        }
        if (activeHttp.isNotEmpty()) {
            val withProgress = activeHttp.filter { it.hasProgress }
            val avg = if (withProgress.isEmpty()) -1f
            else withProgress.map { it.progress }.average().toFloat()
            parts.add(
                getString(
                    R.string.notif_files, activeHttp.size,
                    Fmt.speed(http.sumOf { it.speed }),
                    if (avg < 0) -1 else (avg * 100).toInt()
                )
            )
            if (avg >= 0) { progressSum += avg; progressCount++ }
        }
        val text = if (parts.isEmpty()) getString(R.string.notif_idle) else parts.joinToString(" · ")
        val overall = if (progressCount == 0) 0 else (progressSum / progressCount * 100).toInt()
        return androidx.core.app.NotificationCompat.Builder(this, DeepFluxApp.CHANNEL_PROGRESS)
            .setSmallIcon(R.drawable.ic_stat_deepflux)
            .setContentTitle("DeepFlux")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setProgress(100, overall, progressCount == 0)
            .setContentIntent(contentIntent)
            .build()
    }

    private fun updateNotification(
        items: List<TorrentItem>,
        http: List<space.deepflux.android.dlmgr.HttpDownload>,
    ) {
        runCatching { NotificationManagerCompat.from(this).notify(NOTIFICATION_ID, buildNotification(items, http)) }
    }

    companion object {
        const val NOTIFICATION_ID = 1001
        private const val IDLE_GRACE_MS = 20_000L
        private const val RSS_POLL_MS = 15 * 60 * 1000L

        fun startIntent(context: Context): Intent = Intent(context, TorrentService::class.java)
    }
}
