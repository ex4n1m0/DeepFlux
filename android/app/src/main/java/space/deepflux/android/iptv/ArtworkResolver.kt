package space.deepflux.android.iptv

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import okhttp3.Request
import space.deepflux.android.data.EmbeddedKey
import space.deepflux.android.iptv.Metadata.ArtResult
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * App-scoped artwork resolver (ANDROID_STUDY.md §14 P1+P2, 1.4.2) — ONE
 * instance lives on DeepFluxApp so the cover/logo caches survive tab
 * switches (AppRoot's `when` disposes IPTVScreen; the 1.4.0 `remember`ed
 * resolvers died with it and re-parsed everything per visit).
 *
 * Cells subscribe to a per-key [StateFlow] ([flowFor]); lookups run on a
 * small worker pool fed by a PRIORITY queue — visible items LIFO (newest
 * first), prefetch FIFO — the desktop `_BoundedExecutor` semantics. This
 * replaces per-cell LaunchedEffects serialized behind one lock+sleep,
 * which parked a Dispatchers.IO thread per cell at ~4 lookups/s.
 *
 * Live channels: iptv-org index (keyless) → favicon fallbacks (Google S2,
 * DuckDuckGo — desktop ChannelLogoChain). VOD/local files: the full cover
 * chain (TMDb/TVmaze + adult TPDB/StashDB). Failures never persist and
 * retry after [RETRY_MS]; the cover disk cache flushes debounced + on
 * Activity onStop ([flushNow]).
 */
class ArtworkResolver(context: Context) {

    sealed class State {
        object Loading : State()

        data class Ready(val url: String) : State()

        /** No artwork. [failed] = transient failure (retryable, never persisted). */
        data class Empty(val failed: Boolean) : State()
    }

    private val appCtx = context.applicationContext
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val logos = Metadata.IptvOrgLogos(appCtx)
    private val covers = Metadata.Covers(
        tmdbKey = EmbeddedKey.tmdbKey(),
        tpdbKey = EmbeddedKey.tpdbKey(),
        stashKey = EmbeddedKey.stashdbKey(),
        cacheDir = appCtx.cacheDir,
    )

    // ---- state exposed to cells ----

    private val flows = ConcurrentHashMap<String, MutableStateFlow<State>>()
    private val failures = ConcurrentHashMap<String, Long>()
    private val logoMemo = ConcurrentHashMap<String, String>() // live name → url ("" = miss)
    private val faviconGate = MinIntervalGate(100)

    /** Kick the heavy loads (iptv-org index, cover disk cache) at app start so Live TV opens warm. */
    fun warmup() {
        scope.launch { runCatching { logos.warmup() } }
        scope.launch { runCatching { covers.prewarm() } }
    }

    /**
     * Per-channel artwork state. Cheap + idempotent: a map hit returns the
     * existing flow, the first request creates it and queues the lookup.
     * Same-key channels (same title / same live name) dedupe onto ONE flow.
     */
    fun flowFor(
        channel: M3UParser.Channel,
        isLocal: Boolean,
        lowPriority: Boolean = false,
    ): StateFlow<State> {
        val key = artKey(channel, isLocal)
        val existing = flows[key]
        if (existing != null) {
            val st = existing.value
            if (st is State.Empty && st.failed &&
                System.currentTimeMillis() - failures.getOrDefault(key, 0L) > RETRY_MS
            ) {
                existing.value = State.Loading
                enqueue(key, channel, isLocal, lowPriority)
            }
            return existing
        }
        val flow = MutableStateFlow<State>(State.Loading)
        flows[key] = flow
        enqueue(key, channel, isLocal, lowPriority)
        return flow
    }

    /** Dedupe/join key: VOD+local share the cover cache key; live = the channel name. */
    fun artKey(channel: M3UParser.Channel, isLocal: Boolean): String {
        val vod = isLocal || Metadata.VOD_GROUP_HINT.containsMatchIn(channel.groupId)
        if (!vod) return "l:" + channel.name.trim().lowercase()
        val (title, year, series) = Metadata.titleFrom(channel.name)
        return "c:" + Metadata.coverKey(title, year, series, isAdult(channel))
    }

    private fun isAdult(channel: M3UParser.Channel): Boolean =
        Metadata.ADULT_HINT.containsMatchIn(channel.groupId) ||
            Metadata.ADULT_HINT.containsMatchIn(channel.name)

    // ---- priority dispatcher (visible/newest-first, prefetch FIFO) ----

    private class Task(
        val key: String,
        val channel: M3UParser.Channel,
        val isLocal: Boolean,
    )

    private val qLock = ReentrantLock()
    private val qHigh = ArrayDeque<Task>() // visible cells: LIFO (newest first)
    private val qLow = ArrayDeque<Task>() // prefetch: FIFO
    private val qSignal = qLock.newCondition()

    init {
        repeat(WORKERS) { scope.launch { workerLoop() } }
    }

    private fun enqueue(key: String, channel: M3UParser.Channel, isLocal: Boolean, low: Boolean) {
        qLock.withLock {
            // Bounded queues: a fast fling through a huge list must not
            // enqueue an unbounded background sweep over mobile data.
            // A dropped task's flow flips to Empty(failed) so the next
            // compose re-enqueues it (a Loading flow must never be orphaned).
            if (low) {
                qLow.addLast(Task(key, channel, isLocal))
                if (qLow.size > LOW_CAP) qLow.removeFirst()?.let(::abandon)
            } else {
                qHigh.addLast(Task(key, channel, isLocal))
                if (qHigh.size > HIGH_CAP) qHigh.removeFirst()?.let(::abandon) // stale, scrolled past
            }
            qSignal.signalAll()
        }
    }

    /** The queue dropped this task: mark its flow retryable instead of stuck-Loading. */
    private fun abandon(task: Task) {
        failures[task.key] = System.currentTimeMillis()
        flows[task.key]?.value = State.Empty(true)
    }

    private suspend fun workerLoop() {
        while (true) {
            val task = qLock.withLock {
                while (qHigh.isEmpty() && qLow.isEmpty()) {
                    try {
                        qSignal.await()
                    } catch (_: InterruptedException) {
                    }
                }
                qHigh.removeLastOrNull() ?: qLow.removeFirstOrNull()
            } ?: continue
            runCatching { process(task) }
                .onFailure { Log.w(TAG, "artwork task failed: ${it.message}") }
        }
    }

    private suspend fun process(task: Task) {
        val vod = task.isLocal || Metadata.VOD_GROUP_HINT.containsMatchIn(task.channel.groupId)
        val result: ArtResult = if (vod) {
            val (title, year, series) = Metadata.titleFrom(task.channel.name)
            if (title.length < 2) {
                ArtResult.Miss
            } else if (task.isLocal) {
                covers.cover(title, year, series, adult = isAdult(task.channel))
            } else {
                covers.cover(title, year, series, rawName = task.channel.name, adult = isAdult(task.channel))
            }
        } else {
            liveLogo(task.channel.name)
        }
        val state = when (result) {
            is ArtResult.Found -> State.Ready(result.url)
            ArtResult.Miss -> State.Empty(false)
            ArtResult.Failed -> {
                if (failures.size > 4000) failures.clear()
                failures[task.key] = System.currentTimeMillis()
                State.Empty(true)
            }
        }
        flows[task.key]?.value = state
        if (result != ArtResult.Failed) scheduleFlush()
        if (flows.size > FLOW_CAP) evictTerminal()
    }

    /** iptv-org first, then the keyless favicon fallbacks; memoized per name. */
    private suspend fun liveLogo(name: String): ArtResult {
        logoMemo[name]?.let { return if (it.isEmpty()) ArtResult.Miss else ArtResult.Found(it) }
        var r = logos.logoFor(name)
        if (r is ArtResult.Miss) r = faviconFallback(name)
        if (r != ArtResult.Failed && logoMemo.size < 6000) {
            logoMemo[name] = if (r is ArtResult.Found) r.url else ""
        }
        return r
    }

    /** Desktop ChannelLogoChain sources 2-3: Google S2 favicon (PNG, 128 px), then DuckDuckGo. */
    private suspend fun faviconFallback(name: String): ArtResult {
        val key = Metadata.splitCountry(name).first
        if (key.length < 3) return ArtResult.Miss
        val google = "https://www.google.com/s2/favicons?domain=$key.com&sz=128"
        faviconGate.acquire()
        if (urlOk(google)) return ArtResult.Found(google)
        val ddg = "https://icons.duckduckgo.com/ip3/$key.com.ico"
        faviconGate.acquire()
        if (urlOk(ddg)) return ArtResult.Found(ddg)
        return ArtResult.Miss
    }

    /** HEAD-check a favicon URL: 200 + image-ish content type (desktop _url_ok). */
    private fun urlOk(url: String): Boolean = runCatching {
        Metadata.http.newCall(Request.Builder().url(url).head().build()).execute().use { resp ->
            if (resp.code != 200) return@runCatching false
            val ct = (resp.header("Content-Type") ?: "").lowercase()
            ct.contains("image") || ct.contains("icon") || ct.contains("octet-stream")
        }
    }.getOrDefault(false)

    /** Drop terminal flows so a 131k-list scroll can't grow the map without bound. */
    private fun evictTerminal() {
        val it = flows.entries.iterator()
        var toRemove = flows.size - FLOW_TARGET
        while (toRemove > 0 && it.hasNext()) {
            val e = it.next()
            val st = e.value.value
            if (st is State.Ready || st is State.Empty) {
                it.remove()
                toRemove--
            }
        }
    }

    // ---- cover cache persistence ----

    private var flushJob: Job? = null

    private fun scheduleFlush() {
        flushJob?.cancel()
        flushJob = scope.launch {
            delay(FLUSH_DEBOUNCE_MS)
            runCatching { covers.flush() }
        }
    }

    /** Persist the cover cache NOW (Activity onStop — the process may die next). */
    fun flushNow() {
        flushJob?.cancel()
        flushJob = null
        scope.launch { runCatching { covers.flush() } }
    }

    private companion object {
        const val TAG = "ArtworkResolver"
        const val WORKERS = 6
        const val HIGH_CAP = 400
        const val LOW_CAP = 200
        const val RETRY_MS = 30_000L
        const val FLOW_CAP = 2500
        const val FLOW_TARGET = 2000
        const val FLUSH_DEBOUNCE_MS = 30_000L
    }
}
