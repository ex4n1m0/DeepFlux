package space.deepflux.android.iptv

import android.content.Context
import android.net.Uri
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory

/**
 * Multiview (plan §13 #15) — up to four simultaneous IPTV streams in a 2×2
 * grid, ONE audio track (the first filled slot; the rest are muted — the
 * desktop multiview convention). Owns the ExoPlayers; the UI disposes it
 * wholesale when the grid closes, so no player can outlive its surface.
 */
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
class MultiViewController(context: Context) {

    private val appContext = context.applicationContext
    private val players = arrayOfNulls<ExoPlayer>(4)

    /** Stream URL per slot; null = empty slot. */
    val urls = arrayOfNulls<String>(4)
    val names = arrayOfNulls<String>(4)
    val userAgents = arrayOfNulls<String>(4)

    /** Create-or-reuse the player for a filled slot (main thread only). */
    fun playerFor(slot: Int): ExoPlayer? {
        val url = urls[slot] ?: return null
        if (players[slot] == null) {
            val http = DefaultHttpDataSource.Factory()
                // IPTV streams almost always redirect (CDN 302s).
                .setAllowCrossProtocolRedirects(true)
                .setConnectTimeoutMs(10_000)
                .setReadTimeoutMs(20_000)
                .apply { userAgents[slot]?.let { setUserAgent(it) } }
            players[slot] = ExoPlayer.Builder(appContext)
                .setMediaSourceFactory(DefaultMediaSourceFactory(DefaultDataSource.Factory(appContext, http)))
                .build()
                .apply {
                    setMediaItem(MediaItem.Builder().setUri(Uri.parse(url)).build())
                    repeatMode = Player.REPEAT_MODE_OFF
                    playWhenReady = true
                    volume = 0f
                }
            players[slot]?.prepare()
            applyAudio()
        }
        return players[slot]
    }

    fun setSlot(slot: Int, url: String?, name: String?, userAgent: String? = null) {
        releaseSlot(slot)
        urls[slot] = url
        names[slot] = name
        userAgents[slot] = userAgent
    }

    fun releaseSlot(slot: Int) {
        players[slot]?.release()
        players[slot] = null
    }

    /** Only the first filled slot is audible. */
    private fun applyAudio() {
        val audible = urls.indices.firstOrNull { urls[it] != null }
        players.forEachIndexed { i, p -> p?.volume = if (i == audible) 1f else 0f }
    }

    fun releaseAll() {
        players.forEachIndexed { i, _ -> releaseSlot(i) }
        urls.fill(null)
        names.fill(null)
        userAgents.fill(null)
    }
}
