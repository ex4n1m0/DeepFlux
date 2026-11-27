package space.deepflux.android.player

import android.content.Context
import android.content.Intent
import android.content.pm.ActivityInfo
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.TextView
import androidx.activity.ComponentActivity
import org.videolan.libvlc.LibVLC
import org.videolan.libvlc.Media
import org.videolan.libvlc.MediaPlayer
import org.videolan.libvlc.util.VLCVideoLayout
import space.deepflux.android.ui.Strings

/**
 * The compatibility player: VLC's engine, for streams this device cannot decode with the
 * built-in one. [CompatPolicy] says exactly when [PlayerActivity] hands a stream over;
 * this class only plays it.
 *
 * 1.4.15. What it is here for: 10-bit H.265 films on phones without the hardware for
 * them (47 % of the films in the owner's TV catalogue; every one of them stopped with
 * "Playback failed" before). VLC decodes in software when the hardware cannot.
 *
 * Deliberately the basics — play/pause, seek, time, audio-track and subtitle pickers —
 * in the built-in player's look (landscape, hidden system bars, an orange status line,
 * a top bar that auto-hides while playing). What it does NOT have, and the built-in
 * player does: downloaded sidecar subtitles, resume-from-last-position across sessions,
 * rotation and aspect buttons, multi-view.
 *
 * Lifecycle is VLC's own sample's shape: views are attached in [onStart] and detached in
 * [onStop]; leaving the screen stops playback and remembers where it was, and coming back
 * starts the stream again from there.
 *
 * Not unit-tested: it needs VLC's native libraries, which a JVM test cannot load. What
 * IS tested is the hand-over rule ([CompatPolicy]) and where the two players are wired
 * together (source pins in `PlayerWiringTest`); the screen itself is run on the emulator.
 */
class CompatPlayerActivity : ComponentActivity() {

    private var libVLC: LibVLC? = null
    private var mediaPlayer: MediaPlayer? = null
    private var media: Media? = null
    private var videoLayout: VLCVideoLayout? = null

    private var topBar: LinearLayout? = null
    private var controls: LinearLayout? = null
    private var statusText: TextView? = null
    private var detailText: TextView? = null
    private var playPauseButton: TextView? = null
    private var seekBar: SeekBar? = null
    private var timeText: TextView? = null
    private var audioButton: TextView? = null
    private var spuButton: TextView? = null

    private var url: String = ""
    private var userAgent: String = PlayerActivity.DEFAULT_USER_AGENT
    /** Where to start the stream, ms — handed over by the built-in player, or where leaving the screen stopped it. */
    private var startAtMs: Long = 0L
    private var started = false
    private var reachedPlaying = false
    private var failed = false
    private var userSeeking = false
    private var lengthMs = 0L
    private var trackDialog: android.app.AlertDialog? = null

    private val handler = Handler(Looper.getMainLooper())
    private val hideChrome = Runnable { setChromeVisible(false) }
    private val startTimeout = Runnable {
        if (!reachedPlaying) fail(Strings.t.compatPlayerFailed, "no picture within ${START_TIMEOUT_MS / 1000} s")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        url = intent.getStringExtra(EXTRA_URL) ?: ""
        val title = intent.getStringExtra(EXTRA_TITLE) ?: "DeepFlux"
        if (url.isBlank()) {
            finish()
            return
        }
        active = this
        userAgent = intent.getStringExtra(EXTRA_USER_AGENT)?.takeIf { it.isNotBlank() } ?: PlayerActivity.DEFAULT_USER_AGENT
        startAtMs = (intent.getLongExtra(EXTRA_POSITION_MS, 0L)).coerceAtLeast(0L)
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        // Edge to edge under the system bars and the display cutout, the bars hidden — as the built-in player.
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
        // Host only: stream URLs carry session tokens and passwords.
        Log.i(TAG, "compat player: ${runCatching { java.net.URL(url).host }.getOrDefault("?")} " +
            "(ua=${if (userAgent == PlayerActivity.DEFAULT_USER_AGENT) "default" else "channel"}, title=$title)")

        val root = FrameLayout(this)
        val video = VLCVideoLayout(this).apply {
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT,
            )
        }
        root.addView(video)
        videoLayout = video

        // Status overlay: Connecting… while opening; an error if VLC cannot play it either.
        val overlay = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(24, 0, 24, 0)
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
        overlay.addView(statusText)
        overlay.addView(detailText)
        root.addView(
            overlay,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.CENTER,
            ),
        )

        buildTopBar(title, root)
        buildControls(root)

        try {
            val vlc = LibVLC(this)
            libVLC = vlc
            val player = MediaPlayer(vlc)
            mediaPlayer = player
            player.setEventListener { event -> onEvent(event) }
            // Best fit: the whole picture, letterboxed — the built-in player's default.
            player.setVideoScale(MediaPlayer.ScaleType.SURFACE_BEST_FIT)
        } catch (e: Throwable) {
            // UnsatisfiedLinkError & co.: an ABI VLC has no libraries for. Nothing to fall back to.
            Log.e(TAG, "compat player could not start: $e")
            fail(Strings.t.compatPlayerUnavailable, e.toString())
        }

        setContentView(root)
    }

    private fun buildTopBar(title: String, root: FrameLayout) {
        val closeButton = TextView(this).apply {
            text = "✕"
            setTextColor(0xFFFFFFFF.toInt())
            textSize = 20f
            setPadding(24, 16, 24, 16)
            contentDescription = Strings.t.closePlayer
            minWidth = 72
            minHeight = 72
            gravity = Gravity.CENTER
            isClickable = true
            isFocusable = true
            setOnClickListener { finish() }
        }
        val titleView = TextView(this).apply {
            text = title
            setTextColor(0xFFDDDDDD.toInt())
            textSize = 14f
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
            setPadding(0, 0, 16, 0)
        }
        val audio = barButton(Strings.t.playerAudioTrack, Strings.t.playerAudioTrack) { showTrackPicker(TRACK_AUDIO) }
            .apply { visibility = View.GONE }
        val spu = barButton(Strings.t.subtitlesSection, Strings.t.subtitlesSection) { showTrackPicker(TRACK_SPU) }
            .apply { visibility = View.GONE }
        audioButton = audio
        spuButton = spu
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(8, 8, 16, 8)
            setBackgroundColor(0x66000000.toInt())
            addView(closeButton)
            addView(titleView, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            addView(spu)
            addView(audio)
        }
        // The bar sits under the cutout and the (hidden) status bar.
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(bar) { v, insets ->
            val safe = insets.getInsets(
                androidx.core.view.WindowInsetsCompat.Type.systemBars() or
                    androidx.core.view.WindowInsetsCompat.Type.displayCutout(),
            )
            v.setPadding(8, safe.top + 8, 16, 8)
            insets
        }
        topBar = bar
        root.addView(
            bar,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.TOP,
            ),
        )
    }

    private fun buildControls(root: FrameLayout) {
        val playPause = barButton("▶", Strings.t.compatPlayer) { togglePlayPause() }
        val bar = SeekBar(this).apply {
            max = 1000
            isEnabled = false
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onStartTrackingTouch(seekBar: SeekBar) { userSeeking = true }
                override fun onProgressChanged(seekBar: SeekBar, progress: Int, fromUser: Boolean) = Unit
                override fun onStopTrackingTouch(seekBar: SeekBar) {
                    userSeeking = false
                    val len = lengthMs
                    val p = mediaPlayer ?: return
                    val targetMs = seekBar.progress * len / 1000
                    val seekable = p.isSeekable
                    Log.i(TAG, "seek to ${CompatPolicy.clock(targetMs)} of ${CompatPolicy.clock(len)} (seekable=$seekable, paused=${!p.isPlaying})")
                    if (len > 0 && seekable) {
                        p.setTime(targetMs)
                        // VLC reports no time while paused, so the clock would stay on the old
                        // value until the film runs again: show where the seek was asked for.
                        syncTime(targetMs, len)
                    }
                }
            })
        }
        val time = TextView(this).apply {
            text = ""
            setTextColor(0xFFDDDDDD.toInt())
            textSize = 13f
            setPadding(16, 8, 16, 8)
        }
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(8, 0, 0, 0)
            setBackgroundColor(0x66000000.toInt())
            addView(playPause)
            addView(bar, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            addView(time)
        }
        // Above the (hidden) navigation bar.
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(row) { v, insets ->
            val safe = insets.getInsets(androidx.core.view.WindowInsetsCompat.Type.systemBars() or
                androidx.core.view.WindowInsetsCompat.Type.displayCutout())
            v.setPadding(8, 0, 0, safe.bottom)
            insets
        }
        playPauseButton = playPause
        seekBar = bar
        timeText = time
        controls = row
        root.addView(
            row,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM,
            ),
        )
    }

    private fun barButton(label: String, description: String, onClick: () -> Unit) = TextView(this).apply {
        text = label
        setTextColor(0xFFDDDDDD.toInt())
        textSize = 14f
        setPadding(20, 16, 20, 16)
        contentDescription = description
        isClickable = true
        isFocusable = true
        setOnClickListener { onClick() }
    }

    // ------------------------------------------------------------------ playback

    private fun startPlayback() {
        val vlc = libVLC ?: return
        val player = mediaPlayer ?: return
        media?.release()
        val m = Media(vlc, Uri.parse(url))
        // Hardware decoding when the device can do it, software when it cannot (force = false):
        // software is the reason this player exists, so it must never be locked out.
        m.setHWDecoderEnabled(true, false)
        // The playlist's own User-Agent, as the built-in player sends it (some servers 403 others).
        m.addOption(":http-user-agent=$userAgent")
        if (startAtMs > 0) m.addOption(":start-time=${startAtMs / 1000}")
        media = m
        player.setMedia(m)
        player.play()
        started = true
        handler.removeCallbacks(startTimeout)
        handler.postDelayed(startTimeout, START_TIMEOUT_MS)
    }

    private fun togglePlayPause() {
        val p = mediaPlayer ?: return
        if (p.isPlaying) {
            p.pause()
        } else {
            p.play()
            handler.postDelayed(hideChrome, CHROME_AUTOHIDE_MS)
        }
    }

    private fun onEvent(event: MediaPlayer.Event) {
        // VLC delivers its events on the main thread; runOnUiThread keeps that true whatever changes.
        runOnUiThread {
            val p = mediaPlayer
            when (event.type) {
                MediaPlayer.Event.Playing -> {
                    reachedPlaying = true
                    handler.removeCallbacks(startTimeout)
                    hideStatus()
                    syncPlayPause(true)
                    showChrome()
                }
                MediaPlayer.Event.Paused -> {
                    syncPlayPause(false)
                    showChrome()
                }
                MediaPlayer.Event.Buffering ->
                    // Only a stall after the picture was running; the opening buffer is "Connecting…".
                    if (reachedPlaying) {
                        if (event.buffering >= 100f) hideStatus() else showStatus(Strings.t.playerBuffering)
                    }
                MediaPlayer.Event.TimeChanged -> syncTime(event.timeChanged, p?.length ?: lengthMs)
                MediaPlayer.Event.LengthChanged -> syncTime(p?.time ?: 0L, event.lengthChanged)
                MediaPlayer.Event.SeekableChanged -> seekBar?.isEnabled = event.seekable
                MediaPlayer.Event.Vout -> Log.i(TAG, "video output running (${event.voutCount})")
                MediaPlayer.Event.ESAdded, MediaPlayer.Event.ESDeleted, MediaPlayer.Event.ESSelected -> syncTrackButtons()
                MediaPlayer.Event.EndReached -> {
                    Log.i(TAG, "ended")
                    setResult(RESULT_OK)
                    finish()
                }
                MediaPlayer.Event.EncounteredError -> fail(Strings.t.compatPlayerFailed, "VLC: error while playing")
            }
        }
    }

    private fun fail(message: String, detail: String) {
        if (failed) return
        failed = true
        handler.removeCallbacks(startTimeout)
        Log.w(TAG, "compat player failed: $detail")
        releasePlayback()
        showStatus(message, detail)
        showChrome()
        // The built-in player shows its own failure when this comes back cancelled.
        setResult(RESULT_CANCELED, Intent().putExtra(EXTRA_FAILURE, message))
    }

    private fun syncPlayPause(playing: Boolean) {
        playPauseButton?.text = if (playing) "❚❚" else "▶"
    }

    private fun syncTime(timeMs: Long, lenMs: Long) {
        if (lenMs > 0) lengthMs = lenMs
        val len = lengthMs
        timeText?.text = if (len > 0) {
            "${CompatPolicy.clock(timeMs)} / ${CompatPolicy.clock(len)}"
        } else {
            CompatPolicy.clock(timeMs)
        }
        if (!userSeeking && len > 0) seekBar?.progress = (timeMs * 1000 / len).toInt().coerceIn(0, 1000)
    }

    /**
     * The pickers appear only when there is something to choose. VLC lists "Disable" (id -1) as
     * the first entry of both lists: with one audio track there is no choice to offer, with
     * one subtitle track there is (on or off).
     */
    private fun syncTrackButtons() {
        val p = mediaPlayer ?: return
        val audio = p.audioTracks?.size ?: 0
        val spu = p.spuTracks?.size ?: 0
        Log.i(TAG, "tracks: audio=$audio, subtitles=$spu (VLC's lists, 'Disable' included)")
        // Two or more audio tracks to choose between; at least one subtitle track to switch.
        audioButton?.visibility = if (audio >= 3) View.VISIBLE else View.GONE
        spuButton?.visibility = if (spu >= 2) View.VISIBLE else View.GONE
    }

    private fun showTrackPicker(kind: Int) {
        val p = mediaPlayer ?: return
        val tracks = (if (kind == TRACK_AUDIO) p.audioTracks else p.spuTracks) ?: return
        if (tracks.size <= 1) return
        val current = if (kind == TRACK_AUDIO) p.audioTrack else p.spuTrack
        trackDialog?.dismiss()
        trackDialog = android.app.AlertDialog.Builder(this)
            .setTitle(if (kind == TRACK_AUDIO) Strings.t.playerAudioTrack else Strings.t.subtitlesSection)
            .setSingleChoiceItems(tracks.map { if (it.id < 0) Strings.t.playerTracksOff else it.name ?: "" }.toTypedArray(), tracks.indexOfFirst { it.id == current }) { d, which ->
                val id = tracks[which].id
                val taken = if (kind == TRACK_AUDIO) p.setAudioTrack(id) else p.setSpuTrack(id)
                if (!taken) Log.w(TAG, "track change refused (kind=$kind)")
                d.dismiss()
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    // ------------------------------------------------------------------ chrome

    private fun showStatus(text: String, detail: String = "") {
        statusText?.text = text
        detailText?.text = detail
        statusText?.visibility = View.VISIBLE
        detailText?.visibility = View.VISIBLE
    }

    private fun hideStatus() {
        statusText?.visibility = View.GONE
        detailText?.visibility = View.GONE
    }

    private fun showChrome() {
        setChromeVisible(true)
        handler.removeCallbacks(hideChrome)
        // While the picture runs any touch brings the bars back; paused or failed they stay.
        if (mediaPlayer?.isPlaying == true) handler.postDelayed(hideChrome, CHROME_AUTOHIDE_MS)
    }

    private fun setChromeVisible(visible: Boolean) {
        topBar?.visibility = if (visible) View.VISIBLE else View.GONE
        controls?.visibility = if (visible) View.VISIBLE else View.GONE
    }

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        if (ev.actionMasked == MotionEvent.ACTION_DOWN) showChrome()
        return super.dispatchTouchEvent(ev)
    }

    // ------------------------------------------------------------------ lifecycle

    override fun onStart() {
        super.onStart()
        val player = mediaPlayer ?: return
        val video = videoLayout ?: return
        if (failed) return
        runCatching { player.attachViews(video, null, /* enableSubtitles = */ true, /* useTextureView = */ false) }
            .onFailure { Log.w(TAG, "video views not attached: ${it.message}") }
        if (!started) startPlayback()
    }

    override fun onStop() {
        super.onStop()
        handler.removeCallbacks(hideChrome)
        handler.removeCallbacks(startTimeout)
        val player = mediaPlayer ?: return
        if (failed) return
        // Leaving the screen ends playback where it is; coming back starts again from there —
        // also when the film was paused (the user's pause point, not where the film began).
        val at = if (reachedPlaying) player.time else startAtMs
        runCatching { player.stop() }
        runCatching { player.detachViews() }
        started = false
        reachedPlaying = false
        startAtMs = at
        lengthMs = 0L
        hideStatus()
        showStatus(Strings.t.connecting)
    }

    override fun onDestroy() {
        if (active === this) active = null
        handler.removeCallbacksAndMessages(null)
        trackDialog?.dismiss()
        trackDialog = null
        releasePlayback()
        runCatching { libVLC?.release() }
        libVLC = null
        super.onDestroy()
    }

    /** Stops and frees the media and the player. Never called from inside a VLC event. */
    private fun releasePlayback() {
        val player = mediaPlayer
        mediaPlayer = null
        if (player != null) {
            runCatching { player.setEventListener(null) }
            runCatching { player.stop() }
            runCatching { player.detachViews() }
            runCatching { player.release() }
        }
        runCatching { media?.release() }
        media = null
        started = false
    }

    companion object {
        private const val TAG = "CompatPlayer"
        private const val CHROME_AUTOHIDE_MS = 6_000L
        /** No picture at all after this: say so rather than sit on "Connecting…" for good. */
        private const val START_TIMEOUT_MS = 45_000L
        private const val TRACK_AUDIO = 1
        private const val TRACK_SPU = 2

        const val EXTRA_URL = "url"
        const val EXTRA_TITLE = "title"
        const val EXTRA_USER_AGENT = "user_agent"
        const val EXTRA_POSITION_MS = "position_ms"
        /** On a cancelled result: why the compatibility player could not play it. */
        const val EXTRA_FAILURE = "failure"

        fun intent(context: Context, url: String, title: String, userAgent: String?, positionMs: Long): Intent =
            Intent(context, CompatPlayerActivity::class.java)
                .putExtra(EXTRA_URL, url)
                .putExtra(EXTRA_TITLE, title)
                .putExtra(EXTRA_USER_AGENT, userAgent)
                .putExtra(EXTRA_POSITION_MS, positionMs)

        /**
         * Whether VLC's classes are in this build. Says nothing about the device: an ABI
         * without VLC's libraries shows itself when the engine starts, and the screen
         * then says so.
         */
        fun available(): Boolean = runCatching { Class.forName("org.videolan.libvlc.LibVLC") }.isSuccess

        // The open compatibility player, if any. The built-in player stays alive underneath
        // it, so "is something playing" is answered by PlayerActivity; closing goes through
        // here, or the agent's stop tool would close the screen beneath and leave this one.
        private var active: CompatPlayerActivity? = null
        fun finishActive(): Boolean = active?.let { it.finish(); true } ?: false
    }
}
