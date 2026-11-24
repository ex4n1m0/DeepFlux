package space.deepflux.android.player

import android.os.Bundle
import android.util.Log
import android.view.Gravity
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.PlayerView
import space.deepflux.android.ui.Strings
import kotlinx.coroutines.flow.first

/**
 * Video player for one stream URL — HLS/IPTV or loopback torrent stream.
 * Hardened: follows cross-protocol redirects (IPTV CDNs redirect 302),
 * honors per-channel User-Agents from the playlist (some CDNs 403 the
 * app default UA), shows a Connecting… indicator while the stream loads
 * (live sources can take 10s+ before the first frame), and surfaces a
 * localized error + Retry instead of a bare error code.
 */
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
class PlayerActivity : ComponentActivity() {

    private var player: ExoPlayer? = null
    private var statusText: TextView? = null
    private var detailText: TextView? = null
    private var retryButton: Button? = null

    // Format-fallback state: the first attempt uses the URL-inferred (or
    // caller-provided) mime; if the parse/sniff proves it wrong we retry ONCE
    // with the other family before giving up and showing the error UI.
    private var currentMime: String? = null
    private var fellBackOnce = false

    // Non-fatal decode gap for the current stream ("no sound: … (AC3)").
    private var trackWarning: String = ""

    // ---- player basics (2026-11-24): rotation, aspect ratio, audio track.
    private var playerView: PlayerView? = null
    private var rotation = PlayerModes.Rotation.LANDSCAPE
    /** Set once the rotation was decided for this stream — by its video track, or by the user's button. */
    private var rotationChosen = false
    private var aspect = PlayerModes.Aspect.FIT
    private var audioButton: TextView? = null
    private var audioDialog: android.app.AlertDialog? = null
    /** True once the stream has played: later stalls are "buffering", not "connecting". */
    private var everReady = false

    /** The audio tracks as the picker lists them. */
    private class AudioChoice(val group: Int, val track: Int, val label: String, val selected: Boolean)

    private var audioChoices: List<AudioChoice> = emptyList()

    // The error that triggered the (single) format fallback, so the real
    // cause survives when the second guess fails too.
    private var firstError: PlaybackException? = null

    /** A text button of the top bar, like the CC button. */
    private fun topBarButton(label: String, description: String, onClick: () -> Unit) = TextView(this).apply {
        text = label
        setTextColor(0xFFDDDDDD.toInt())
        textSize = 14f
        setPadding(20, 16, 20, 16)
        contentDescription = description
        isClickable = true
        isFocusable = true
        setOnClickListener { onClick() }
    }

    private fun cycleRotation() {
        rotationChosen = true
        applyRotation(PlayerModes.nextRotation(rotation))
    }

    /** Landscape / portrait follow the sensor (either way up); Auto leaves it to the system's rotation setting. */
    private fun applyRotation(r: PlayerModes.Rotation) {
        rotation = r
        requestedOrientation = when (r) {
            PlayerModes.Rotation.LANDSCAPE -> android.content.pm.ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
            PlayerModes.Rotation.PORTRAIT -> android.content.pm.ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
            PlayerModes.Rotation.AUTO -> android.content.pm.ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        }
    }

    private fun cycleAspect() {
        aspect = PlayerModes.nextAspect(aspect)
        applyAspect()
    }

    private fun applyAspect() {
        playerView?.resizeMode = when (aspect) {
            PlayerModes.Aspect.FIT -> androidx.media3.ui.AspectRatioFrameLayout.RESIZE_MODE_FIT
            PlayerModes.Aspect.FILL -> androidx.media3.ui.AspectRatioFrameLayout.RESIZE_MODE_ZOOM
            PlayerModes.Aspect.STRETCH -> androidx.media3.ui.AspectRatioFrameLayout.RESIZE_MODE_FILL
        }
    }

    private fun showAudioPicker() {
        val p = player ?: return
        if (audioChoices.size < 2) return
        audioDialog?.dismiss()
        audioDialog = android.app.AlertDialog.Builder(this)
            .setTitle(Strings.t.playerAudioTrack)
            .setSingleChoiceItems(
                audioChoices.map { it.label }.toTypedArray(),
                audioChoices.indexOfFirst { it.selected },
            ) { _, which ->
                val choice = audioChoices.getOrNull(which) ?: return@setSingleChoiceItems
                val group = p.currentTracks.groups.getOrNull(choice.group) ?: return@setSingleChoiceItems
                p.trackSelectionParameters = p.trackSelectionParameters.buildUpon()
                    .setOverrideForType(
                        androidx.media3.common.TrackSelectionOverride(group.mediaTrackGroup, choice.track),
                    )
                    .build()
                audioDialog?.dismiss()
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_ROTATION, rotation.name)
        outState.putString(STATE_ASPECT, aspect.name)
    }

    // ---- resume positions (plan §13 #13) — keyed by stream title, stored
    // in player_positions.json (atomic writes, same sidecar style as the
    // download engines).
    private var streamTitle: String = ""
    private val saveHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private var resumeBar: LinearLayout? = null
    private var contentContainer: FrameLayout? = null
    private val positionSaver = object : Runnable {
        override fun run() {
            val p = player
            if (p != null && p.isPlaying) savePosition(p.currentPosition, p.duration)
            saveHandler.postDelayed(this, 5_000)
        }
    }
    private fun positionsFile(): java.io.File = java.io.File(filesDir, "player_positions.json")

    // ---- top-bar auto-hide (owner 2026-11-16) ----
    // The title/✕/CC bar fades out 10s after playback is actually running
    // and ANY touch brings it back. The 2026-11-13 rule ("a visible way
    // out at all times") evolves: while playing, the way out is one tap
    // away; paused/connecting/errored keeps the bar pinned so an obvious
    // exit always remains on screen.
    private var topBar: LinearLayout? = null
    private val chromeHandler = android.os.Handler(android.os.Looper.getMainLooper())

    /** Auto-hide only while truly playing — READY + playWhenReady, so a
     *  mid-stream rebuffer stall never flickers the chrome back and forth. */
    private fun chromeAutoHideDue(): Boolean = player?.let {
        it.playbackState == Player.STATE_READY && it.playWhenReady
    } == true

    private val hideTopBar = Runnable {
        val bar = topBar ?: return@Runnable
        if (!chromeAutoHideDue()) return@Runnable
        bar.animate().alpha(0f).setDuration(220).withEndAction {
            // Re-check: state may have changed (pause/touch) during the fade.
            if (chromeAutoHideDue()) bar.visibility = android.view.View.GONE
        }.start()
    }

    /** Show the bar (any touch) and restart the hide timer if playing. */
    private fun showTopBar() {
        val bar = topBar ?: return
        if (bar.visibility != android.view.View.VISIBLE) {
            bar.alpha = 0f
            bar.visibility = android.view.View.VISIBLE
        }
        bar.animate().alpha(1f).setDuration(160).start()
        scheduleTopBarHide()
    }

    private fun scheduleTopBarHide() {
        chromeHandler.removeCallbacks(hideTopBar)
        if (chromeAutoHideDue()) chromeHandler.postDelayed(hideTopBar, TOP_BAR_AUTOHIDE_MS)
    }

    // ---- subtitles (plan §13 #8) ----
    /** Loaded sidecar .srt (auto-found sibling or an OpenSubtitles download). */
    private var subtitleFile: java.io.File? = null
    private var subsEnabled = true
    /** Real local path when the stream came from our own file (hash search). */
    private var filePathExtra: String? = null
    private var ccButton: TextView? = null
    private var subsDialog: android.app.AlertDialog? = null
    private fun loadPositions(): org.json.JSONObject = runCatching {
        if (positionsFile().exists()) org.json.JSONObject(positionsFile().readText()) else org.json.JSONObject()
    }.getOrNull() ?: org.json.JSONObject()

    private fun savePosition(positionMs: Long, durationMs: Long) {
        if (positionMs < 10_000 || durationMs <= 0 || streamTitle.isBlank()) return
        runCatching {
            val all = loadPositions()
            // Monotonic: reopening without resuming plays from 0 — a fresh
            // session must never push the remembered position BACKWARDS.
            val existing = all.optJSONObject(streamTitle)
            if (existing != null && existing.optLong("pos", 0) > positionMs) return
            all.put(streamTitle, org.json.JSONObject()
                .put("pos", positionMs).put("dur", durationMs).put("at", System.currentTimeMillis()))
            prune(all)
            val f = positionsFile()
            val tmp = java.io.File(f.parentFile, f.name + ".tmp")
            tmp.writeText(all.toString())
            if (!tmp.renameTo(f)) { f.delete(); tmp.renameTo(f) }
        }
    }

    private fun clearPosition() {
        if (streamTitle.isBlank()) return
        runCatching {
            val all = loadPositions()
            all.remove(streamTitle)
            positionsFile().writeText(all.toString())
        }
    }

    /** Keep the newest 50 entries only. */
    private fun prune(all: org.json.JSONObject) {
        val keys = all.keys().asSequence().toList()
        if (keys.size <= 50) return
        val byAge = keys.sortedByDescending { all.optJSONObject(it)?.optLong("at", 0) ?: 0 }
        byAge.drop(50).forEach { all.remove(it) }
    }

    private fun formatTime(ms: Long): String {
        val totalSec = ms / 1000
        val h = totalSec / 3600
        val m = (totalSec % 3600) / 60
        val s = totalSec % 60
        return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
    }

    /** "Resume at 12:34 / Start over" bar when a meaningful position exists. */
    private fun maybeShowResumeBar(durationMs: Long) {
        val saved = loadPositions().optJSONObject(streamTitle)
        Log.i(TAG, "resume check: title='$streamTitle' saved=$saved dur=$durationMs")
        if (saved == null) return
        val pos = saved.optLong("pos", 0)
        if (pos < 15_000 || durationMs <= 0 || pos > durationMs * 95 / 100) return
        val p = player ?: return
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            setBackgroundColor(0xCC000000.toInt())
        }
        fun hide() {
            resumeBar?.let { (it.parent as? FrameLayout)?.removeView(it) }
            resumeBar = null
        }
        bar.addView(
            Button(this).apply {
                text = Strings.t.resumeAtFmt.format(formatTime(pos))
                setOnClickListener { p.seekTo(pos); p.playWhenReady = true; hide() }
            }
        )
        bar.addView(
            Button(this).apply {
                text = Strings.t.startOver
                setOnClickListener { p.seekTo(0); hide() }
            }
        )
        hide()
        resumeBar = bar
        contentContainer?.addView(
            bar,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM,
            )
        )
        saveHandler.postDelayed({ hide() }, 8_000)
    }

    /** "audio/eac3" → "EAC3" for the on-screen decode warning. */
    private fun codecLabel(mime: String?): String =
        (mime ?: "?").substringAfter('/').removePrefix("x-").uppercase()

    // ------------------------------------------------------------- subtitles (§13 #8)

    /** MediaItem with the sidecar subtitle attached when one is loaded. */
    private fun buildMediaItem(url: String, mimeType: String?): MediaItem {
        val builder = MediaItem.Builder().setUri(url)
        if (mimeType != null) builder.setMimeType(mimeType)
        subtitleFile?.let { srt ->
            builder.setSubtitleConfigurations(
                listOf(
                    MediaItem.SubtitleConfiguration.Builder(android.net.Uri.fromFile(srt))
                        .setMimeType(MimeTypes.APPLICATION_SUBRIP)
                        .setSelectionFlags(androidx.media3.common.C.SELECTION_FLAG_DEFAULT)
                        .build(),
                ),
            )
        }
        return builder.build()
    }

    /** A sidecar downloaded earlier: next to a local file, else the store. */
    private fun findExistingSubtitle(title: String): java.io.File? {
        filePathExtra?.let { path ->
            val f = java.io.File(path)
            val stem = f.nameWithoutExtension
            val dir = f.parentFile
            if (dir != null && stem.isNotBlank()) {
                return dir.listFiles()
                    ?.filter { it.isFile && it.extension.equals("srt", true) && it.name.startsWith("$stem.") }
                    ?.maxByOrNull { it.lastModified() }
            }
        }
        val safe = sanitizeFileStem(title)
        if (safe.isEmpty()) return null
        return java.io.File(filesDir, "subtitles").listFiles()
            ?.filter { it.isFile && it.extension.equals("srt", true) && it.name.startsWith("$safe.") }
            ?.maxByOrNull { it.lastModified() }
    }

    private fun sanitizeFileStem(title: String): String =
        title.replace(Regex("[\\\\/:*?\"<>|\\p{Cntrl}]"), " ").trim().take(60)

    private fun subtitleDest(title: String): java.io.File {
        val safe = sanitizeFileStem(title).ifBlank { "subtitle" }
        val dir: java.io.File = filePathExtra?.let { java.io.File(it).parentFile }?.takeIf { it.canWrite() }
            ?: java.io.File(filesDir, "subtitles").apply { mkdirs() }
        var n = 0
        var candidate = java.io.File(dir, "$safe.en.srt")
        while (candidate.exists()) {
            n++
            candidate = java.io.File(dir, "$safe.en.$n.srt")
        }
        return candidate
    }

    /** CC tap: toggle when subtitles are loaded, otherwise search OpenSubtitles. */
    private fun onCcClick() {
        if (subtitleFile != null) {
            subsEnabled = !subsEnabled
            setSubsEnabled(subsEnabled)
            return
        }
        openSubtitleSearch()
    }

    private fun setSubsEnabled(on: Boolean) {
        val p = player ?: return
        p.trackSelectionParameters = p.trackSelectionParameters.buildUpon()
            .setTrackTypeDisabled(androidx.media3.common.C.TRACK_TYPE_TEXT, !on)
            .build()
        ccButton?.setTextColor(
            when {
                subtitleFile == null -> 0xFFDDDDDD.toInt()
                on -> 0xFF7FD4FF.toInt()
                else -> 0xFF888888.toInt()
            },
        )
    }

    private fun openSubtitleSearch() {
        val title = streamTitle
        subsDialog = android.app.AlertDialog.Builder(this)
            .setTitle(Strings.t.subtitlesSection)
            .setMessage(Strings.t.subtitlesSearching)
            .setNegativeButton(Strings.t.cancel, null)
            .show()
        Thread({
            val result = runCatching {
                val prefs = kotlinx.coroutines.runBlocking {
                    space.deepflux.android.data.AppSettings(this@PlayerActivity).prefs.first()
                }
                space.deepflux.android.iptv.OpenSubtitles.search(
                    apiKey = prefs.opensubtitlesApiKey,
                    query = space.deepflux.android.iptv.OpenSubtitles.cleanMediaQuery(title),
                    filePath = filePathExtra,
                )
            }
            runOnUiThread {
                subsDialog?.dismiss()
                subsDialog = null
                result.fold(
                    onSuccess = { subs -> showSubtitleResults(subs, title) },
                    onFailure = { e ->
                        android.app.AlertDialog.Builder(this)
                            .setTitle(Strings.t.subtitlesSection)
                            .setMessage(e.message ?: Strings.t.subtitlesNone)
                            .setPositiveButton(Strings.t.ok, null)
                            .show()
                    },
                )
            }
        }, "subs-search").apply { isDaemon = true }.start()
    }

    private fun showSubtitleResults(subs: List<space.deepflux.android.iptv.OpenSubtitles.SubtitleResult>, title: String) {
        if (subs.isEmpty()) {
            android.app.AlertDialog.Builder(this)
                .setTitle(Strings.t.subtitlesSection)
                .setMessage(Strings.t.subtitlesNone)
                .setPositiveButton(Strings.t.ok, null)
                .show()
            return
        }
        val labels = subs.take(12).map {
            (if (it.hashMatch) "⭐ " else "") + it.release.take(60) +
                " · ${it.language}" + (if (it.year.isNotBlank()) " · ${it.year}" else "") +
                " · ↓${it.downloads}"
        }.toTypedArray()
        android.app.AlertDialog.Builder(this)
            .setTitle(Strings.t.subtitlesSection)
            .setItems(labels) { _, which ->
                val pick = subs.take(12)[which]
                Thread({
                    try {
                        val bytes = space.deepflux.android.iptv.OpenSubtitles.download(
                            kotlinx.coroutines.runBlocking {
                                space.deepflux.android.data.AppSettings(this@PlayerActivity).prefs.first().opensubtitlesApiKey
                            },
                            pick.fileId,
                        )
                        val dest = subtitleDest(title)
                        dest.writeBytes(bytes)
                        Log.i(TAG, "subtitle saved: $dest (${bytes.size} bytes)")
                        runOnUiThread { applySubtitle(dest) }
                    } catch (e: Exception) {
                        runOnUiThread {
                            android.app.AlertDialog.Builder(this)
                                .setTitle(Strings.t.subtitlesSection)
                                .setMessage(e.message ?: "download failed")
                                .setPositiveButton(Strings.t.ok, null)
                                .show()
                        }
                    }
                }, "subs-download").apply { isDaemon = true }.start()
            }
            .setNegativeButton(Strings.t.cancel, null)
            .show()
    }

    /** Re-prepare with the sidecar attached; playback position is preserved. */
    private fun applySubtitle(file: java.io.File) {
        subtitleFile = file
        subsEnabled = true
        val p = player ?: return
        val url = intent.getStringExtra(EXTRA_URL) ?: return
        val pos = p.currentPosition
        p.setMediaItem(buildMediaItem(url, currentMime))
        p.prepare()
        p.seekTo(pos)
        p.playWhenReady = true
        setSubsEnabled(true)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val url = intent.getStringExtra(EXTRA_URL)
        val title = intent.getStringExtra(EXTRA_TITLE) ?: "DeepFlux"
        if (url.isNullOrBlank()) {
            finish()
            return
        }
        active = this
        // The user's rotation and aspect choices survive the activity (a rotation
        // does not recreate it — see the manifest — but a process death does).
        savedInstanceState?.getString(STATE_ROTATION)?.let { s ->
            runCatching { PlayerModes.Rotation.valueOf(s) }.getOrNull()?.let { rotation = it; rotationChosen = true }
        }
        savedInstanceState?.getString(STATE_ASPECT)?.let { s ->
            runCatching { PlayerModes.Aspect.valueOf(s) }.getOrNull()?.let { aspect = it }
        }
        // Edge to edge — under the system bars and the display cutout — with the
        // bars hidden; a swipe shows them for a moment. The bar with ✕ keeps
        // clear of the cutout (see its inset listener below).
        androidx.core.view.WindowCompat.setDecorFitsSystemWindows(window, false)
        if (android.os.Build.VERSION.SDK_INT >= 28) {
            window.attributes = window.attributes.apply {
                layoutInDisplayCutoutMode = android.view.WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
            }
        }
        androidx.core.view.WindowInsetsControllerCompat(window, window.decorView).apply {
            hide(androidx.core.view.WindowInsetsCompat.Type.systemBars())
            systemBarsBehavior = androidx.core.view.WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }

        val mimeType = inferMimeType(url, intent.getStringExtra(EXTRA_MIME))
        val userAgent = intent.getStringExtra(EXTRA_USER_AGENT)?.takeIf { it.isNotBlank() }
            ?: DEFAULT_USER_AGENT
        filePathExtra = intent.getStringExtra(EXTRA_FILE_PATH)?.takeIf { java.io.File(it).isFile }
        // §13 #8: auto-load a sidecar that already exists (a downloaded
        // subtitle persists next to the file / in the subtitles store).
        subtitleFile = findExistingSubtitle(title)
        // Host only: stream URLs carry session tokens and passwords.
        Log.i(TAG, "playing: ${runCatching { java.net.URL(url).host }.getOrDefault("?")} (mime=$mimeType, ua=${if (userAgent == DEFAULT_USER_AGENT) "default" else "channel"}, title=$title)")

        val container = FrameLayout(this)
        val view = PlayerView(this).apply {
            useController = true
            // A stall mid-stream shows the spinner — not a frozen picture and silence.
            setShowBuffering(PlayerView.SHOW_BUFFERING_WHEN_PLAYING)
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
        }
        container.addView(view)
        playerView = view
        applyAspect()

        // Status overlay: Connecting… while loading, error + Retry on failure.
        val overlay = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(24, 120, 24, 24)
        }
        statusText = TextView(this).apply {
            text = Strings.t.connecting
            setTextColor(0xFFFF8866.toInt())
            textSize = 16f
            gravity = Gravity.CENTER_HORIZONTAL
        }
        detailText = TextView(this).apply {
            setTextColor(0xFF999999.toInt())
            textSize = 12f
            gravity = Gravity.CENTER_HORIZONTAL
        }
        retryButton = Button(this).apply {
            text = Strings.t.retry
            visibility = android.view.View.GONE
            setOnClickListener {
                statusText?.text = Strings.t.connecting
                detailText?.text = ""
                visibility = android.view.View.GONE
                // The media item is still set — prepare() restarts the load.
                player?.prepare()
                player?.playWhenReady = true
            }
        }
        overlay.addView(statusText)
        overlay.addView(detailText)
        overlay.addView(retryButton)
        container.addView(overlay)

        // Exit/title bar: media3's stock controller has transport controls
        // + seek bar but NO close button, and gesture-back is undiscoverable
        // mid-video (owner report 2026-11-13: "no button to stop the video
        // and go back"). Since 2026-11-16 (owner) the bar AUTO-HIDES 10s
        // after playback actually runs and any touch brings it back;
        // paused/connecting/errored keeps it pinned, so a visible way out
        // always remains.
        val closeButton = TextView(this).apply {
            text = "✕"
            setTextColor(0xFFFFFFFF.toInt())
            textSize = 20f
            setPadding(24, 16, 24, 16)
            contentDescription = Strings.t.closePlayer
            // Still a generous tap target despite the compact glyph.
            minWidth = 72
            minHeight = 72
            gravity = Gravity.CENTER
            isClickable = true
            isFocusable = true
            setOnClickListener { finish() } // onDestroy releases the player.
        }
        val titleView = TextView(this).apply {
            text = title
            setTextColor(0xFFDDDDDD.toInt())
            textSize = 14f
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
            setPadding(0, 0, 16, 0)
        }
        val topBar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(8, 8, 16, 8)
            setBackgroundColor(0x66000000.toInt())
        }
        topBar.addView(closeButton)
        topBar.addView(titleView)
        // §13 #8: CC button — loads/toggles OpenSubtitles sidecar subtitles.
        val ccBtn = TextView(this).apply {
            text = "CC"
            setTextColor(if (subtitleFile != null) 0xFF7FD4FF.toInt() else 0xFFDDDDDD.toInt())
            textSize = 14f
            setPadding(20, 16, 20, 16)
            contentDescription = Strings.t.subtitlesSection
            isClickable = true
            isFocusable = true
            setOnClickListener { onCcClick() }
        }
        ccButton = ccBtn
        topBar.addView(ccBtn)
        // Rotation, aspect ratio, audio track (2026-11-24). The first two cycle;
        // the third appears only when the stream has more than one audio track.
        topBar.addView(topBarButton("⟳", Strings.t.playerRotation) { cycleRotation() })
        topBar.addView(topBarButton("⤢", Strings.t.playerAspect) { cycleAspect() })
        val audioBtn = topBarButton(Strings.t.playerAudioTrack, Strings.t.playerAudioTrack) { showAudioPicker() }
            .apply { visibility = android.view.View.GONE }
        audioButton = audioBtn
        topBar.addView(audioBtn)
        // The bar sits under the cutout and the (hidden) status bar now.
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(topBar) { v, insets ->
            val safe = insets.getInsets(
                androidx.core.view.WindowInsetsCompat.Type.systemBars() or
                    androidx.core.view.WindowInsetsCompat.Type.displayCutout(),
            )
            v.setPadding(8, safe.top + 8, 16, 8)
            insets
        }
        container.addView(
            topBar,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.TOP
            )
        )
        this.topBar = topBar
        setContentView(container)

        try {
            val httpFactory = DefaultHttpDataSource.Factory()
                // CRITICAL: allow cross-protocol redirects (HTTP→HTTPS, CDN 302s).
                // IPTV streams almost always redirect; without this, ExoPlayer
                // fails with InvalidResponseCodeException: 302.
                .setAllowCrossProtocolRedirects(true)
                .setConnectTimeoutMs(10_000)
                .setReadTimeoutMs(20_000)
                .setUserAgent(userAgent)

            val dataSourceFactory = DefaultDataSource.Factory(this, httpFactory)

            player = ExoPlayer.Builder(this)
                .setMediaSourceFactory(DefaultMediaSourceFactory(dataSourceFactory))
                // Audio focus: the stream yields to a call or another player and
                // takes over again after it — it used to play on top of both. And
                // pulling the headphones out pauses instead of switching to the speaker.
                .setAudioAttributes(
                    androidx.media3.common.AudioAttributes.Builder()
                        .setUsage(androidx.media3.common.C.USAGE_MEDIA)
                        .setContentType(androidx.media3.common.C.AUDIO_CONTENT_TYPE_MOVIE)
                        .build(),
                    /* handleAudioFocus = */ true,
                )
                .setHandleAudioBecomingNoisy(true)
                .build()
                .also { p ->
                    view.player = p
                    p.addListener(object : Player.Listener {
                        // Chrome auto-hide lifecycle: countdown (re)starts the
                        // moment playback actually runs; pausing pins the bar.
                        override fun onIsPlayingChanged(isPlaying: Boolean) {
                            if (isPlaying) scheduleTopBarHide() else showTopBar()
                        }

                        override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) {
                            if (playWhenReady) scheduleTopBarHide() else showTopBar()
                        }

                        override fun onPlaybackStateChanged(playbackState: Int) {
                            if (playbackState == Player.STATE_READY) {
                                everReady = true
                                // Keep a decode warning visible: READY fires
                                // even when a whole track type is unplayable.
                                statusText?.text = trackWarning
                                detailText?.text = ""
                                retryButton?.visibility = android.view.View.GONE
                                Log.i(TAG, "ready: duration=${p.duration}ms warning='$trackWarning'")
                                saveHandler.removeCallbacks(positionSaver)
                                saveHandler.postDelayed(positionSaver, 5_000)
                                if (p.duration > 0) maybeShowResumeBar(p.duration)
                            }
                            if (playbackState == Player.STATE_ENDED) {
                                // Finished all the way through — forget the position.
                                clearPosition()
                            }
                            // A stall after the stream had been playing. Before the first
                            // frame "Connecting…" stays what it says.
                            if (playbackState == Player.STATE_BUFFERING && everReady) {
                                statusText?.text = Strings.t.playerBuffering
                                detailText?.text = ""
                                retryButton?.visibility = android.view.View.GONE
                            }
                        }

                        override fun onTracksChanged(tracks: androidx.media3.common.Tracks) {
                            // Format coverage (plan §12 A4): the container can
                            // parse fine while THIS DEVICE has no decoder for a
                            // track (AC-3/DTS audio, some HEVC/AV1 video).
                            // ExoPlayer raises no error for that — it plays
                            // silent video or audio-only — so say it on screen.
                            var videoTracks = 0
                            var videoOk = 0
                            var audioTracks = 0
                            var audioOk = 0
                            var badVideo = ""
                            var badAudio = ""
                            for (g in tracks.groups) {
                                for (i in 0 until g.length) {
                                    val f = g.getTrackFormat(i)
                                    val ok = g.isTrackSupported(i)
                                    Log.i(
                                        TAG,
                                        "track type=${g.type} mime=${f.sampleMimeType} codecs=${f.codecs} " +
                                            "${f.width}x${f.height} ch=${f.channelCount} " +
                                            "supported=$ok selected=${g.isTrackSelected(i)}"
                                    )
                                    when (g.type) {
                                        androidx.media3.common.C.TRACK_TYPE_VIDEO -> {
                                            videoTracks++
                                            if (ok) videoOk++ else badVideo = codecLabel(f.sampleMimeType)
                                        }
                                        androidx.media3.common.C.TRACK_TYPE_AUDIO -> {
                                            audioTracks++
                                            if (ok) audioOk++ else badAudio = codecLabel(f.sampleMimeType)
                                        }
                                    }
                                }
                            }
                            trackWarning = when {
                                videoTracks > 0 && videoOk == 0 -> "${Strings.t.videoUnsupported} ($badVideo)"
                                audioTracks > 0 && audioOk == 0 -> "${Strings.t.audioUnsupported} ($badAudio)"
                                else -> ""
                            }
                            if (trackWarning.isNotBlank()) statusText?.text = trackWarning

                            // The audio picker lists every audio track, named by what it is.
                            audioChoices = tracks.groups.mapIndexedNotNull { gi, g ->
                                if (g.type != androidx.media3.common.C.TRACK_TYPE_AUDIO) {
                                    null
                                } else {
                                    (0 until g.length).map { ti ->
                                        val f = g.getTrackFormat(ti)
                                        AudioChoice(
                                            gi, ti,
                                            PlayerModes.audioLabel(ti + 1, f.language, f.sampleMimeType, f.channelCount),
                                            g.isTrackSelected(ti),
                                        )
                                    }
                                }
                            }.flatten()
                            audioButton?.visibility =
                                if (audioChoices.size > 1) android.view.View.VISIBLE else android.view.View.GONE
                            // A video opens in landscape — once per stream; after that the
                            // rotation button is the user's. Audio-only stays as it is.
                            if (videoTracks > 0 && !rotationChosen) {
                                rotationChosen = true
                                applyRotation(PlayerModes.Rotation.LANDSCAPE)
                            }
                        }

                        override fun onPlayerError(error: PlaybackException) {
                            // One-shot format fallback before surfacing the error:
                            // IPTV providers label anything "video/mp4" — a stream
                            // can be raw MPEG-TS behind an extension-less URL
                            // (tv123.me, 2026-11-14) or a real .m3u8 behind a
                            // redirect. Flip the container family once and retry.
                            if (tryFormatFallback(error)) return
                            // When the fallback guess also failed, the FIRST
                            // failure is the meaningful one — show that.
                            val shown = firstError ?: error
                            Log.e(TAG, "playback error: ${shown.errorCodeName} — ${shown.message}")
                            statusText?.text = Strings.t.playbackFailed
                            detailText?.text = readableCause(shown)
                            retryButton?.visibility = android.view.View.VISIBLE
                        }
                    })
                    val item = buildMediaItem(url, mimeType)
                    currentMime = mimeType
                    p.setMediaItem(item)
                    p.prepare()
                    p.playWhenReady = true
                }
        } catch (e: Exception) {
            Log.e(TAG, "player init failed: ${e.message}")
            statusText?.text = Strings.t.playerInitFailedPrefix + (e.message ?: "")
            retryButton?.visibility = android.view.View.VISIBLE
        }
        setTitle(title)
        streamTitle = title
        contentContainer = container
    }

    /**
     * One-shot container-family flip on parse errors. Returns true when the
     * error was consumed by a retry (caller must not show the error UI).
     *
     * HLS-forced + MANIFEST_MALFORMED → retry with no mime (extension-less
     * URLs then go progressive and the stream is sniffed — TsExtractor
     * recognizes the 0x47 sync byte pattern regardless of the server's
     * bogus video/mp4 content-type).
     *
     * No mime (URL gave no signal) + CONTAINER_MALFORMED/UNSUPPORTED → the
     * sniffer found nothing it knows; retry forced as HLS, since an
     * extension-less URL that redirects to a real playlist is the common
     * IPTV shape. URLs that DO carry a progressive extension (.mp4/.ts/…)
     * never take the HLS branch — a container failure there is real.
     */
    private fun tryFormatFallback(error: PlaybackException): Boolean {
        if (fellBackOnce || player == null) return false
        val url = intent.getStringExtra(EXTRA_URL) ?: ""
        val isHttp = url.startsWith("http://", true) || url.startsWith("https://", true)
        // The loopback stream server serves LOCAL files under an
        // extension-less URL — never an HLS playlist.
        val isLoopback = url.startsWith("http://127.0.0.1", true)
        return when {
            currentMime == MimeTypes.APPLICATION_M3U8 &&
                error.errorCode == PlaybackException.ERROR_CODE_PARSING_MANIFEST_MALFORMED -> {
                fellBackOnce = true
                Log.i(TAG, "HLS manifest malformed — retrying as progressive (sniffed)")
                statusText?.text = Strings.t.connecting
                detailText?.text = ""
                prepareWith(null)
                true
            }
            // Only for remote, extension-less URLs: a local file or one whose
            // name says ".mp4/.mkv/…" cannot turn out to be an HLS playlist,
            // and the A4 matrix showed the retry masking the real error
            // (WMV/MPEG-PS files surfaced as "HLS manifest malformed").
            currentMime == null && isHttp && !isLoopback && !hasKnownMediaExtension(url) &&
                (error.errorCode == PlaybackException.ERROR_CODE_PARSING_CONTAINER_MALFORMED ||
                    error.errorCode == PlaybackException.ERROR_CODE_PARSING_CONTAINER_UNSUPPORTED) -> {
                fellBackOnce = true
                firstError = error
                Log.i(TAG, "progressive sniff failed — retrying as HLS")
                statusText?.text = Strings.t.connecting
                detailText?.text = ""
                prepareWith(MimeTypes.APPLICATION_M3U8)
                true
            }
            else -> false
        }
    }

    private fun hasKnownMediaExtension(url: String): Boolean {
        val path = url.substringBefore('?').lowercase()
        return listOf(
            ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts",
            ".mpg", ".mpeg", ".wmv", ".3gp", ".m4a", ".mp3", ".flac",
            ".ogg", ".opus", ".wav", ".aac",
        ).any { path.endsWith(it) }
    }

    /** (Re)load the stream with an explicit container mime (null = sniff). */
    private fun prepareWith(mime: String?) {
        currentMime = mime
        val url = intent.getStringExtra(EXTRA_URL) ?: return
        val item = MediaItem.Builder()
            .setUri(url)
            .apply { if (mime != null) setMimeType(mime) }
            .build()
        player?.run {
            setMediaItem(item)
            prepare()
            playWhenReady = true
        }
    }

    /** Short human-readable cause chain for the dim detail line. */
    private fun readableCause(error: PlaybackException): String {
        var cause: Throwable? = error.cause
        var deepest = error.message
        var hops = 0
        while (cause != null && hops < 5) {
            deepest = cause.message ?: deepest
            cause = cause.cause
            hops++
        }
        // InvalidResponseCodeException hides the failing URL from the message —
        // surface the DataSpec URI so "which request failed" is answerable
        // from the screen (master playlist vs variant vs segment).
        var url = ""
        var c: Throwable? = error.cause
        var uhops = 0
        while (c != null && uhops < 5) {
            if (c is androidx.media3.datasource.HttpDataSource.InvalidResponseCodeException) {
                url = c.dataSpec.uri.toString()
                break
            }
            c = c.cause
            uhops++
        }
        val suffix = if (url.isNotBlank()) " [$url]" else ""
        return "${error.errorCodeName}${deepest?.let { " — $it" } ?: ""}$suffix".take(220)
    }

    override fun onStart() {
        super.onStart()
        player?.play()
    }

    override fun onStop() {
        super.onStop()
        player?.pause()
    }

    override fun dispatchTouchEvent(ev: android.view.MotionEvent): Boolean {
        // Any touch reveals the auto-hidden top bar (and restarts its
        // 10s timer while playing). Dispatch-level so it fires before any
        // child — media3's own controller toggle included — and covers the
        // whole surface, video area and bars alike.
        if (ev.actionMasked == android.view.MotionEvent.ACTION_DOWN) showTopBar()
        return super.dispatchTouchEvent(ev)
    }

    override fun onDestroy() {
        if (active === this) active = null
        saveHandler.removeCallbacksAndMessages(null)
        chromeHandler.removeCallbacksAndMessages(null)
        resumeBar?.let { (it.parent as? FrameLayout)?.removeView(it) }
        resumeBar = null
        audioDialog?.dismiss()
        audioDialog = null
        playerView = null
        // Final position save — the 5s tick misses the tail of a session.
        player?.let { p -> runCatching { if (p.duration > 0) savePosition(p.currentPosition, p.duration) } }
        try {
            player?.release()
        } catch (e: Exception) {
            Log.w(TAG, "release failed: ${e.message}")
        }
        player = null
        super.onDestroy()
    }

    private fun inferMimeType(url: String, provided: String?): String? {
        if (!provided.isNullOrBlank()) return provided
        val lower = url.lowercase()
        return when {
            lower.contains(".m3u8") || lower.contains("m3u8") -> MimeTypes.APPLICATION_M3U8
            lower.endsWith(".mpd") -> MimeTypes.APPLICATION_MPD
            lower.endsWith(".mp4") || lower.endsWith(".m4v") -> MimeTypes.VIDEO_MP4
            lower.endsWith(".webm") -> MimeTypes.VIDEO_WEBM
            lower.endsWith(".mkv") -> MimeTypes.VIDEO_MATROSKA
            lower.endsWith(".ts") -> MimeTypes.VIDEO_MP2T
            else -> null
        }
    }

    companion object {
        private const val TAG = "PlayerActivity"
        /** Top bar auto-hides this long after playback starts (owner 2026-11-16). */
        private const val TOP_BAR_AUTOHIDE_MS = 10_000L

        private const val STATE_ROTATION = "rotation"
        private const val STATE_ASPECT = "aspect"
        const val DEFAULT_USER_AGENT = "DeepFlux/1.0 Android"
        const val EXTRA_URL = "url"
        const val EXTRA_TITLE = "title"
        const val EXTRA_MIME = "mime"
        const val EXTRA_USER_AGENT = "user_agent"

        /** Local file path when the stream is a file we own (subtitle hash search). */
        const val EXTRA_FILE_PATH = "file_path"

        // Currently-open player instance, if any — lets the agent's
        // stop_iptv_playback tool close the video without touching the UI.
        // Cleared in onDestroy; never dereferenced beyond finish().
        @JvmStatic
        private var active: PlayerActivity? = null
        val isActive: Boolean get() = active != null
        fun finishActive(): Boolean = active?.let { it.finish(); true } ?: false
    }
}
