package space.deepflux.android.service

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
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
import space.deepflux.android.dlmgr.HttpState
import space.deepflux.android.engine.TorrentItem
import space.deepflux.android.engine.TorrentRepository
import space.deepflux.android.engine.TorrentState
import space.deepflux.android.engine.isActive
import space.deepflux.android.util.Fmt

/**
 * When the download service may hold the CPU and the Wi-Fi radio awake.
 * Pure Kotlin: the service feeds it what it sees, tests drive a clock.
 *
 * The locks used to live exactly as long as the service. The service
 * outlives transfers by its idle grace, and it keeps running the whole time
 * the Wi-Fi / charging gate is closed — while the engine is paused and not a
 * byte can move (review finding 2026-11-24: a wake lock for a phone that is
 * only waiting for Wi-Fi).
 *
 * Held while transfers are active, the gate is open, and bytes have moved
 * recently. "Recently" because a torrent between two pieces, or one still
 * fetching its metadata, is working without anything to show yet — a short
 * stall must not let the device suspend. After [STALL_MS] without a byte the
 * CPU is given back; the first byte afterwards (an inbound peer still wakes
 * the radio) takes it again.
 */
internal class TransferLockPolicy(
    private val stallMs: Long = STALL_MS,
    private val now: () -> Long = System::currentTimeMillis,
) {
    private var lastBytes = -1L
    private var lastProgressAt = now()
    private var lastExportBytes = -1L
    private var lastExportProgressAt = now()

    /**
     * [movedBytes] is what both lanes have moved in total so far;
     * [receiving] means a transfer reports a live rate (bytes inside an
     * unfinished torrent piece do not count as moved yet).
     *
     * [exporting] with [exportBytes] is a copy of a finished download into
     * the user's folder: local work too, so it holds the CPU with the gate
     * open or closed — and gives it back after the same stall window, so a
     * copy that hangs cannot keep the phone awake.
     */
    fun tick(
        gateOpen: Boolean,
        transfersActive: Boolean,
        checking: Boolean,
        receiving: Boolean,
        movedBytes: Long,
        exporting: Boolean = false,
        exportBytes: Long = -1,
    ): Boolean {
        val t = now()
        val progressed = (lastBytes >= 0 && movedBytes > lastBytes) || receiving
        lastBytes = movedBytes
        if (progressed) lastProgressAt = t
        val exportProgressed = exporting && (lastExportBytes < 0 || exportBytes > lastExportBytes)
        if (exportProgressed) lastExportProgressAt = t
        lastExportBytes = if (exporting) exportBytes else -1
        // A hash check — or a copy into the user's folder — is local
        // work: it needs the CPU, not the network.
        val moving = transfersActive && gateOpen && t - lastProgressAt < stallMs
        val copying = exporting && t - lastExportProgressAt < stallMs
        return moving || checking || copying
    }

    companion object {
        const val STALL_MS = 60_000L
    }
}

/**
 * Foreground service (dataSync) that keeps BOTH download lanes (torrents +
 * plain HTTP/HLS) alive while the UI is closed. It owns no engine state —
 * the repository singletons do — it only mirrors them into a progress
 * notification and stops itself after an idle grace period.
 *
 * Started only from user-gesture paths (Android 12+ background-FGS
 * restrictions); see TorrentRepository.ensureService() and
 * HttpDownloadRepository.onActiveChanged. It is NOT started at app launch:
 * the repository's first look at the network gate is not a transition.
 *
 * Fix batch 2026-11-23:
 *  - tapping the notification opens the DOWNLOADS tab (it landed on the
 *    Agent tab — the app's boot tab);
 *  - the notification carries "Pause all" / "Resume all";
 *  - a partial wake lock + a Wi-Fi lock are held while transfers need them —
 *    WAKE_LOCK was declared but never acquired, so transfers stalled as soon
 *    as the device suspended with the screen off;
 *  - Android 15's dataSync time limit is handled ([onTimeout]) instead of
 *    ending in a system force-stop.
 *
 * Review round 2026-11-24:
 *  - the locks follow actual progress with the gate open
 *    ([TransferLockPolicy]) instead of the service's lifetime;
 *  - "Pause all" makes everything idle, so the service stops — and its
 *    "Resume all" button vanished with it 20 s after the user pressed Pause.
 *    A standing notice ([syncPausedNotice]) takes over.
 */
class TorrentService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var idleJob: Job? = null
    private var lastNotify = 0L
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    /** Decides when the wake + Wi-Fi locks are held. Guarded by [lockGuard]. On the elapsed-realtime clock: the stall window must not jump when the wall clock is corrected. */
    private val lockPolicy = TransferLockPolicy(now = { android.os.SystemClock.elapsedRealtime() })
    private val lockGuard = Any()

    /** The app singletons this service mirrors (DeepFluxApp owns them). */
    private val app: DeepFluxApp get() = application as DeepFluxApp

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        running = true
        startForeground(NOTIFICATION_ID, buildNotification(emptyList(), emptyList(), resumeAll = false))
        val app = application as DeepFluxApp
        // While the service lives, its own notification carries "Resume all".
        app.syncPausedNotice()
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
                if (it.any { h -> h.state == HttpState.RUNNING } || now - lastNotify > 1000) {
                    updateNotification(lastTorrents, lastHttp)
                    lastNotify = now
                }
                scheduleIdleStop(lastTorrents, lastHttp)
            }
        }
        // Lock watchdog. Progress and stalls change while no flow does, and
        // so can the gate (the user leaves Wi-Fi with nothing else changing).
        // Guarded: one exception must not end the loop — that left whatever
        // lock was held at that moment held for the service's whole life.
        scope.launch {
            while (true) {
                runCatching { syncLocks() }.onFailure {
                    Log.w(TAG, "lock sync failed: ${it.message}")
                    synchronized(lockGuard) { releaseLocks() } // fail open: no lock
                }
                delay(LOCK_TICK_MS)
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

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_PAUSE_ALL) {
            // The notification's "Pause all" button — no UI is opened.
            val app = application as DeepFluxApp
            app.repository.pauseAll()
            app.httpDownloads.pauseAll()
        }
        return START_STICKY
    }

    /**
     * Android 15+ ends a dataSync foreground service after ~6 h per day.
     * An app that does not stop it here is force-stopped by the system
     * (transfers die mid-write). Instead: pause both lanes cleanly — exactly
     * what "Pause all" does, so they are restorable — and stop. [onDestroy]
     * leaves the standing "paused" notice with the limit's wording and a
     * "Resume all" button. The pause is saved, so it survives the process.
     */
    override fun onTimeout(startId: Int, fgsType: Int) {
        val app = application as DeepFluxApp
        app.repository.pauseAll()
        app.httpDownloads.pauseAll()
        app.transfersPausedBySystemLimit = true
        stopSelf()
    }

    override fun onDestroy() {
        running = false
        scope.cancel()
        synchronized(lockGuard) { releaseLocks() }
        (application as? DeepFluxApp)?.syncPausedNotice()
        super.onDestroy()
    }

    /**
     * Downloads must progress with the screen OFF. A foreground service
     * alone does not keep the CPU awake: once the device suspends, the
     * process is frozen and transfers stall until something wakes it. The
     * partial wake lock holds the CPU; the Wi-Fi lock keeps the radio out of
     * its power-save state. Both are held only while [TransferLockPolicy]
     * says transfers are moving bytes, or a finished download is being
     * copied into the user's folder (local work, gate or no gate) — and
     * never through a long stall.
     */
    private fun syncLocks() = synchronized(lockGuard) {
        val torrents = app.repository.torrents.value
        val http = app.httpDownloads.items.value
        val hold = lockPolicy.tick(
            gateOpen = app.repository.gateOpen.value,
            transfersActive = torrents.any { it.isActive } ||
                http.any { it.state == HttpState.RUNNING || it.state == HttpState.QUEUED },
            checking = torrents.any { it.state == TorrentState.CHECKING },
            receiving = torrents.any { it.downloadRate > 0 } || http.any { it.speed > 0 },
            movedBytes = torrents.sumOf { it.doneBytes } + http.sumOf { it.doneBytes },
            exporting = space.deepflux.android.dlmgr.FinishedExport.busy(),
            exportBytes = space.deepflux.android.dlmgr.FinishedExport.copiedBytes(),
        )
        // A lock that cannot be acquired is reported, not fatal — and never
        // leaves its twin held on its own.
        if (hold) {
            runCatching { acquireLocks() }.onFailure {
                Log.w(TAG, "could not take the wake/Wi-Fi lock: ${it.message}")
                releaseLocks()
            }
        } else {
            releaseLocks()
        }
    }

    private fun acquireLocks() {
        if (wakeLock == null) {
            wakeLock = (getSystemService(POWER_SERVICE) as? PowerManager)
                ?.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "DeepFlux:downloads")
                ?.apply {
                    setReferenceCounted(false)
                    // Safety net only: releaseLocks() releases when transfers stop moving.
                    acquire(WAKE_LOCK_SAFETY_TIMEOUT_MS)
                }
        }
        if (wifiLock == null) {
            @Suppress("DEPRECATION") // WIFI_MODE_FULL is the only mode without a latency contract
            wifiLock = (applicationContext.getSystemService(WIFI_SERVICE) as? WifiManager)
                ?.createWifiLock(WifiManager.WIFI_MODE_FULL, "DeepFlux:downloads")
                ?.apply {
                    setReferenceCounted(false)
                    acquire()
                }
        }
    }

    private fun releaseLocks() {
        wakeLock?.let { if (it.isHeld) runCatching { it.release() } }
        wakeLock = null
        wifiLock?.let { if (it.isHeld) runCatching { it.release() } }
        wifiLock = null
    }

    /**
     * The Android 15 dataSync FGS cap (~6h/day cumulative) makes long
     * seeding on a phone a non-goal (study §5): stop the service after a
     * 20s grace period with nothing active in EITHER lane. Downloads pause
     * with the process when it later dies; state rehydrates on next start.
     */
    private fun scheduleIdleStop(items: List<TorrentItem>, http: List<space.deepflux.android.dlmgr.HttpDownload>) {
        val busy = items.any { it.isActive } ||
            http.any { it.state == HttpState.RUNNING }
        if (!busy) {
            if (idleJob == null) {
                idleJob = scope.launch {
                    delay(IDLE_GRACE_MS)
                    val app = application as DeepFluxApp
                    // A copy into the user's folder is part of finishing a
                    // download: the service — and with it the process —
                    // stays until it is done, or the copy is cut off.
                    while (true) {
                        val stillIdle = app.repository.torrents.value.none { it.isActive } &&
                            app.httpDownloads.items.value.none { it.state == HttpState.RUNNING }
                        // A transfer came back: its own flows schedule the next stop.
                        if (!stillIdle) break
                        if (!space.deepflux.android.dlmgr.FinishedExport.busy()) {
                            stopSelf()
                            break
                        }
                        delay(EXPORT_WAIT_MS)
                    }
                    idleJob = null
                }
            }
        } else {
            idleJob?.cancel()
            idleJob = null
        }
    }

    private fun updateNotification(
        items: List<TorrentItem>,
        http: List<space.deepflux.android.dlmgr.HttpDownload>,
    ) {
        val app = application as DeepFluxApp
        val resumeAll = app.repository.anyPausedByPauseAll() || app.httpDownloads.anyPausedByPauseAll()
        runCatching {
            NotificationManagerCompat.from(this).notify(NOTIFICATION_ID, buildNotification(items, http, resumeAll))
        }
    }

    private fun buildNotification(
        items: List<TorrentItem>,
        http: List<space.deepflux.android.dlmgr.HttpDownload>,
        resumeAll: Boolean,
    ): Notification {
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        val contentIntent = PendingIntent.getActivity(
            this,
            REQ_CONTENT, // unique request code so it does not collide with other PendingIntents
            openDownloadsIntent(this),
            flags,
        )
        val activeTorrents = items.filter { it.isActive }
        val activeHttp = http.filter {
            it.state == HttpState.RUNNING ||
                it.state == HttpState.QUEUED
        }
        val progress = describeProgress(
            resources,
            torrents = activeTorrents.size,
            torrentSpeed = Fmt.speed(items.sumOf { it.downloadRate }),
            torrentProgress = activeTorrents.takeIf { it.isNotEmpty() }?.map { it.progress }?.average()?.toFloat() ?: 0f,
            files = activeHttp.size,
            fileSpeed = Fmt.speed(http.sumOf { it.speed }),
            // null while no file download knows its size yet (a chunked
            // transfer, HLS before its last segment): the text then says
            // so — it used to print "-1%".
            fileProgress = activeHttp.filter { it.hasProgress }.takeIf { it.isNotEmpty() }?.map { it.progress }?.average()?.toFloat(),
        )
        val text = progress.text
        val builder = androidx.core.app.NotificationCompat.Builder(this, DeepFluxApp.CHANNEL_PROGRESS)
            .setSmallIcon(R.drawable.ic_stat_deepflux)
            .setContentTitle("DeepFlux")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            // No bar while nothing runs: "No active downloads" over a moving
            // bar said the opposite — through the idle grace after every finish.
            .apply {
                if (progress.active) setProgress(100, progress.overallPercent, progress.indeterminate) else setProgress(0, 0, false)
            }
            .setContentIntent(contentIntent)
        // One transport control: "Pause all" while anything runs, else
        // "Resume all" while there is something a "Pause all" left paused.
        if (activeTorrents.isNotEmpty() || activeHttp.isNotEmpty()) {
            builder.addAction(
                R.drawable.ic_stat_pause,
                getString(R.string.notif_pause_all),
                PendingIntent.getService(this, REQ_PAUSE_ALL, pauseAllIntent(this), flags),
            )
        } else if (resumeAll) {
            builder.addAction(
                R.drawable.ic_stat_play,
                getString(R.string.notif_resume_all),
                PendingIntent.getActivity(this, REQ_RESUME_ALL, resumeAllIntent(this), flags),
            )
        }
        return builder.build()
    }

    companion object {
        const val NOTIFICATION_ID = 1001

        /** The standing "paused" notice — a normal notification, no service behind it. */
        const val NOTIFICATION_ID_PAUSED = 1002

        /** What the standing notification says, and how far its bar is. */
        /** [active]: anything is running — the bar is shown then and only then. */
        internal class ProgressText(val text: String, val overallPercent: Int, val indeterminate: Boolean, val active: Boolean)

        /**
         * The progress notification's wording — pure, on the app's
         * resources, so it can be tested: a download whose size is unknown
         * used to print "-1%", and a single one was "1 files".
         */
        internal fun describeProgress(
            res: android.content.res.Resources,
            torrents: Int,
            torrentSpeed: String,
            torrentProgress: Float,
            files: Int,
            fileSpeed: String,
            fileProgress: Float?,
        ): ProgressText {
            val parts = ArrayList<String>()
            val known = ArrayList<Float>()
            if (torrents > 0) {
                parts.add(res.getString(R.string.notif_active, torrents, torrentSpeed, (torrentProgress * 100).toInt()))
                known.add(torrentProgress)
            }
            if (files > 0) {
                val shown = if (fileProgress == null) res.getString(R.string.notif_progress_unknown)
                else res.getString(R.string.notif_progress_pct, (fileProgress * 100).toInt())
                parts.add(res.getQuantityString(R.plurals.notif_files, files, files, fileSpeed, shown))
                if (fileProgress != null) known.add(fileProgress)
            }
            val text = if (parts.isEmpty()) res.getString(R.string.notif_idle) else parts.joinToString(" · ")
            return ProgressText(
                text,
                if (known.isEmpty()) 0 else (known.average() * 100).toInt(),
                known.isEmpty(),
                torrents > 0 || files > 0,
            )
        }

        private const val IDLE_GRACE_MS = 20_000L
        /** How often the idle grace re-checks whether a copy for the user is still running. */
        private const val EXPORT_WAIT_MS = 2_000L
        private const val RSS_POLL_MS = 15 * 60 * 1000L

        /** How often the lock policy looks at progress. */
        private const val LOCK_TICK_MS = 5_000L

        /** Backstop only (lint's wakelock-timeout rule) — released when transfers stop moving. */
        private const val WAKE_LOCK_SAFETY_TIMEOUT_MS = 12 * 60 * 60 * 1000L

        /** True between the service's onCreate and onDestroy. */
        @Volatile
        var running = false
            private set

        /** Notification button: pause every transfer, in the service (no UI). */
        const val ACTION_PAUSE_ALL = "space.deepflux.android.action.PAUSE_ALL"

        private const val REQ_CONTENT = 1001
        private const val REQ_PAUSE_ALL = 1002
        private const val REQ_RESUME_ALL = 1003
        private const val TAG = "TorrentService"

        fun startIntent(context: Context): Intent = Intent(context, TorrentService::class.java)

        /** Opens the app on the Downloads tab — every download notification's tap target. */
        fun openDownloadsIntent(context: Context): Intent =
            Intent(context, MainActivity::class.java)
                .putExtra(TorrentRepository.EXTRA_OPEN_TAB, TorrentRepository.TAB_DOWNLOADS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)

        @Volatile
        private var cachedToken: String? = null

        /** Tests only: forgets the in-memory token, as a new process would. */
        @androidx.annotation.VisibleForTesting
        internal fun forgetTokenForTests() {
            cachedToken = null
        }

        /**
         * The secret behind "Resume all", kept in the app's private storage
         * so that it survives the process. MainActivity is exported
         * (launcher), so any app could send it an intent with the action
         * extra; without this token a third-party app could resume the
         * user's paused downloads.
         *
         * It used to be random per process, on the belief that a notification
         * cannot outlive its process. True for a foreground service's own
         * notification — not for the standing "paused" notice, which is a
         * normal one and is shown precisely while no service runs: after a
         * kill its button was dead until the app was opened (second review,
         * 2026-11-24). Private storage is readable by this app alone, so the
         * guarantee is unchanged, and the file is not on the backup allow-list.
         * Should the write fail, the token still lasts for this process.
         */
        fun notificationToken(context: Context): String {
            cachedToken?.let { return it }
            synchronized(this) {
                cachedToken?.let { return it }
                val file = java.io.File(context.filesDir, "notification_token")
                val stored = runCatching { file.readText().trim() }.getOrNull().orEmpty()
                if (stored.length >= 32 && stored.all { it.isLetterOrDigit() }) {
                    cachedToken = stored
                    return stored
                }
                val fresh = java.util.UUID.randomUUID().toString().replace("-", "")
                runCatching {
                    val tmp = java.io.File(file.parentFile, file.name + ".tmp")
                    tmp.writeText(fresh)
                    if (!tmp.renameTo(file)) {
                        file.delete()
                        tmp.renameTo(file)
                    }
                }
                cachedToken = fresh
                return fresh
            }
        }

        /** The notification's "Resume all": opens Downloads AND resumes what "Pause all" paused. */
        fun resumeAllIntent(context: Context): Intent =
            openDownloadsIntent(context)
                .putExtra(TorrentRepository.EXTRA_ACTION, TorrentRepository.ACTION_RESUME_ALL)
                .putExtra(TorrentRepository.EXTRA_NOTIFICATION_TOKEN, notificationToken(context))

        /**
         * Whether [intent] is OUR notification's "Resume all" — the exact
         * decision MainActivity makes. The activity is exported, so the
         * action extra alone must never resume anyone's downloads.
         */
        fun isResumeAllRequest(context: Context, intent: Intent): Boolean =
            intent.getStringExtra(TorrentRepository.EXTRA_ACTION) == TorrentRepository.ACTION_RESUME_ALL &&
                intent.getStringExtra(TorrentRepository.EXTRA_NOTIFICATION_TOKEN) == notificationToken(context)

        /** The notification's "Pause all": straight to the service, nothing is opened. */
        fun pauseAllIntent(context: Context): Intent =
            Intent(context, TorrentService::class.java).setAction(ACTION_PAUSE_ALL)

        /**
         * True when the standing "paused" notice belongs in the shade:
         * transfers sit paused by "Pause all" (or by the system's background
         * time limit), and the service is not showing its own notification —
         * that one already carries "Resume all".
         */
        fun pausedNoticeWanted(anyPausedByPauseAll: Boolean, serviceRunning: Boolean): Boolean =
            anyPausedByPauseAll && !serviceRunning

        /**
         * Shows or removes the standing "paused" notice. A NORMAL
         * notification: "Pause all" idles every transfer, so the service
         * stops 20 s later and its "Resume all" button used to go with it.
         * [bySystemLimit] switches to the wording of the background time
         * limit (TorrentService.onTimeout), which pauses the same way.
         */
        fun syncPausedNotice(context: Context, wanted: Boolean, bySystemLimit: Boolean) {
            val nm = NotificationManagerCompat.from(context)
            if (!wanted) {
                nm.cancel(NOTIFICATION_ID_PAUSED)
                return
            }
            if (Build.VERSION.SDK_INT >= 33 &&
                ContextCompat.checkSelfPermission(context, android.Manifest.permission.POST_NOTIFICATIONS) !=
                PackageManager.PERMISSION_GRANTED
            ) {
                return
            }
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            val title = if (bySystemLimit) context.getString(R.string.notif_limit_title) else context.getString(R.string.notif_paused_title)
            val text = if (bySystemLimit) context.getString(R.string.notif_limit_text) else context.getString(R.string.notif_paused_text)
            val notification = NotificationCompat.Builder(context, DeepFluxApp.CHANNEL_PROGRESS)
                .setSmallIcon(R.drawable.ic_stat_deepflux)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                .setContentIntent(
                    PendingIntent.getActivity(context, REQ_CONTENT, openDownloadsIntent(context), flags),
                )
                .addAction(
                    R.drawable.ic_stat_play,
                    context.getString(R.string.notif_resume_all),
                    PendingIntent.getActivity(context, REQ_RESUME_ALL, resumeAllIntent(context), flags),
                )
                .setAutoCancel(true)
                .build()
            runCatching { nm.notify(NOTIFICATION_ID_PAUSED, notification) }
        }
    }
}
