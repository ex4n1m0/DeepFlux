package space.deepflux.android.iptv

import android.content.Context
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * Metadata + artwork for the Live TV list (1.3.5; reworked 1.4.2 per
 * ANDROID_STUDY.md §14) — the Android port of the desktop's providers
 * (iptv/metadata.py):
 *
 *  * [IptvOrgLogos] — channel logos from the iptv-org database (KEYLESS).
 *    The LIVE logos.json is keyed by channel ID and carries no names, so it
 *    is JOINED against channels.json into a name→logo index (the API's old
 *    {name, logo} schema died before 1.4.2 — parsing it built an empty map
 *    and the keyless lane resolved nothing). The built index is cached on
 *    disk (versioned, weekly, conditional GETs; a stale index is served
 *    while a refresh fails).
 *  * [Covers] — movie/series/adult cover art: TMDb (keyed) → TVmaze
 *    (keyless, series); adult: TPDB → StashDB. Suspending, paced by
 *    injectable [PaceGate]s (5/s token bucket shared by the keyed/keyless
 *    APIs, a 250 ms min-interval only for TPDB), distinguishes
 *    authoritative misses from transient failures, disk cache with a 48 h
 *    negative TTL.
 *
 * All entry points are suspend and must run OFF the main thread (the
 * ArtworkResolver workers own them).
 */
object Metadata {

    /** Tri-state lookup outcome (§14: a miss must never be confused with a failure). */
    sealed class ArtResult {
        data class Found(val url: String) : ArtResult()

        /** Providers answered: there is no artwork. Cacheable. */
        object Miss : ArtResult()

        /** Transient (network/HTTP/rate): not cached, retried with backoff. */
        object Failed : ArtResult()
    }

    // ------------------------------------------------------------------ channel logos (keyless)

    /**
     * Channel-name → logo URL from the iptv-org database (keyless). The join
     * of logos.json (channel id → best logo, raster/in-use/main-feed scored)
     * with channels.json (id → names/alt_names/country) is built when the
     * on-disk index is stale (weekly) and cached as a fast TSV; per-process
     * lookups then hit an immutable in-memory map. (Deviation from the
     * desktop: an in-memory index is kept for the PROCESS lifetime — the
     * weekly refresh re-runs on the next cold start, which on Android is
     * within the day.) Index unavailable = [ArtResult.Failed] (never
     * memoized as a miss — an iptv-org outage must not poison names, desktop
     * parity) with a 5-minute backoff.
     */
    class IptvOrgLogos(private val context: Context) {

        private val dir = File(context.cacheDir, "iptvorg")

        @Volatile
        private var idx: Map<String, String>? = null

        @Volatile
        private var unavailableUntil = 0L
        private val resolved = ConcurrentHashMap<String, String>() // query → url ("" = miss)
        private val loadMu = Mutex()

        /** Blocking-ish suspend; call off the main thread. */
        suspend fun logoFor(channelName: String): ArtResult {
            if (channelName.isBlank()) return ArtResult.Miss
            resolved[channelName]?.let { return if (it.isEmpty()) ArtResult.Miss else ArtResult.Found(it) }
            val index = indexOrNull() ?: return ArtResult.Failed
            // Coil cannot decode SVG — an SVG-only channel is a miss so the
            // resolver's favicon chain can supply a PNG instead.
            val url = lookupIn(index, channelName)
                ?.takeUnless { it.substringBefore('?').endsWith(".svg", ignoreCase = true) }
            if (resolved.size > 10_000) resolved.clear()
            resolved[channelName] = url ?: ""
            return if (url == null) ArtResult.Miss else ArtResult.Found(url)
        }

        /** Load (or refresh) the index; safe to call repeatedly. */
        suspend fun warmup() {
            indexOrNull()
        }

        private suspend fun indexOrNull(): Map<String, String>? {
            idx?.let { return it }
            if (System.currentTimeMillis() < unavailableUntil) return null
            return loadMu.withLock {
                idx?.let { return it }
                // Re-check the backoff under the lock: at cold start against
                // an outage every worker queues here — without this each runs
                // a full (timeout-bound) retry round after the first failure.
                if (System.currentTimeMillis() < unavailableUntil) return null
                val loaded = withContext(Dispatchers.IO) { loadIndexBlocking() }
                if (loaded == null) unavailableUntil = System.currentTimeMillis() + BACKOFF_MS else idx = loaded
                loaded
            }
        }

        private fun loadIndexBlocking(): Map<String, String>? {
            runCatching { dir.mkdirs() }
            // 1.4.0-era weekly file for the dead {name, logo} schema — dead
            // weight (5.8 MB) next to the new join; reclaim it once.
            runCatching { File(context.cacheDir, "iptvorg_logos.json").delete() }
            val meta = readMeta()
            val idxFile = File(dir, "index.tsv")
            val now = System.currentTimeMillis()
            // Fast path: the built index is fresh → parse the TSV (no JSON,
            // no network) and go.
            if (meta != null && meta.optInt("v", 0) == INDEX_VERSION && idxFile.isFile &&
                now - meta.optLong("updated", 0L) < WEEK_MS
            ) {
                val parsed = runCatching { parseIndexText(idxFile.readText()) }.getOrNull()
                if (!parsed.isNullOrEmpty()) return parsed
            }
            // Weekly refresh: conditional GETs keep an unchanged pair at
            // ~zero bytes (GitHub Pages answers 304 to If-Modified-Since).
            val logos = fetchConditional(
                LOGOS_URL, File(dir, "logos.json"),
                meta?.optString("logos_lm").orEmpty(),
            )
            val chans = fetchConditional(
                CHANNELS_URL, File(dir, "channels.json"),
                meta?.optString("ch_lm").orEmpty(),
            )
            if (logos != null && chans != null) {
                val built = runCatching { buildIndex(logos.first, chans.first) }.getOrNull()
                if (!built.isNullOrEmpty()) {
                    writeIndex(idxFile, built)
                    writeMeta(now, logos.second, chans.second)
                    return built
                }
            }
            // Network failed (or the build came out empty): serve whatever
            // is already on disk — stale logos beat no logos.
            val stale = runCatching { if (idxFile.isFile) parseIndexText(idxFile.readText()) else null }.getOrNull()
            if (!stale.isNullOrEmpty()) return stale
            val rawL = File(dir, "logos.json").takeIf { it.isFile }
                ?.let { runCatching { it.readText() }.getOrNull() }
            val rawC = File(dir, "channels.json").takeIf { it.isFile }
                ?.let { runCatching { it.readText() }.getOrNull() }
            if (rawL != null && rawC != null) {
                val built = runCatching { buildIndex(rawL, rawC) }.getOrNull()
                if (!built.isNullOrEmpty()) {
                    writeIndex(idxFile, built)
                    writeMeta(now, meta?.optString("logos_lm").orEmpty(), meta?.optString("ch_lm").orEmpty())
                    return built
                }
            }
            return null // → 5-minute backoff in indexOrNull
        }

        private fun lookupIn(index: Map<String, String>, channelName: String): String? {
            val (key, country) = splitCountry(channelName)
            if (country.isNotEmpty()) index[key + country]?.let { return it }
            index[key]?.let { return it }
            if (key.length >= 5) {
                // Prefix match ('bbconenorthwest' → 'bbcone'); the longest
                // matching key wins so 'skysportsf1' beats 'sky'.
                var best = ""
                for (k in index.keys) {
                    if (k.length >= 5 && k.length > best.length &&
                        (key.startsWith(k) || k.startsWith(key))
                    ) {
                        best = k
                    }
                }
                if (best.isNotEmpty()) return index[best]
            }
            return null
        }

        /** logos.json: best logo per channel id (desktop _logo_score). */
        private fun logoScore(o: JSONObject): Int {
            var score = 0
            if (o.optString("format").uppercase() in RASTER_FORMATS) score += 10
            if (o.optBoolean("in_use")) score += 4
            if (o.optString("feed").isBlank()) score += 2 // main feed, not a variant
            return score
        }

        /** The desktop join: logos.json (id→url) × channels.json (id→names). */
        private fun buildIndex(logosText: String, channelsText: String): Map<String, String> {
            val best = HashMap<String, Pair<Int, String>>()
            val logos = JSONArray(logosText)
            for (i in 0 until logos.length()) {
                val o = logos.optJSONObject(i) ?: continue
                val cid = o.optString("channel")
                val url = o.optString("url")
                if (cid.isBlank() || !url.startsWith("http")) continue
                val score = logoScore(o)
                val prev = best[cid]
                if (prev == null || score > prev.first) best[cid] = score to url
            }
            val idx = HashMap<String, String>()
            val channels = JSONArray(channelsText)
            for (i in 0 until channels.length()) {
                val ch = channels.optJSONObject(i) ?: continue
                val hit = best[ch.optString("id")]?.second ?: continue
                val country = ch.optString("country").lowercase()
                val names = ArrayList<String>()
                ch.optString("name").takeIf { it.isNotBlank() }?.let(names::add)
                val alt = ch.optJSONArray("alt_names")
                if (alt != null) for (j in 0 until alt.length()) names.add(alt.optString(j))
                for (nm in names) {
                    val k = normalizeLogoName(nm)
                    if (k.isEmpty()) continue
                    if (!idx.containsKey(k)) idx[k] = hit
                    if (country.isNotEmpty() && !idx.containsKey(k + country)) idx[k + country] = hit
                }
                val stem = normalizeLogoName(ch.optString("id").substringBefore('.'))
                if (stem.isNotEmpty() && !idx.containsKey(stem)) idx[stem] = hit
            }
            return idx
        }

        // ---- index file (TSV: fast to parse on every launch) ----

        private fun writeIndex(f: File, m: Map<String, String>) {
            runCatching {
                val sb = StringBuilder(m.size * 40)
                sb.append(INDEX_VERSION).append('\n')
                m.forEach { (k, v) -> sb.append(k).append('\t').append(v).append('\n') }
                val tmp = File(f.path + ".tmp")
                tmp.writeText(sb.toString())
                if (!tmp.renameTo(f)) {
                    f.delete()
                    tmp.renameTo(f)
                }
            }
        }

        private fun parseIndexText(text: String): Map<String, String>? {
            val lines = text.split('\n')
            if (lines.isEmpty() || lines[0].trim().toIntOrNull() != INDEX_VERSION) return null
            val out = HashMap<String, String>(lines.size)
            for (i in 1 until lines.size) {
                val l = lines[i]
                if (l.isEmpty()) continue
                val tab = l.indexOf('\t')
                if (tab <= 0) continue
                out[l.substring(0, tab)] = l.substring(tab + 1)
            }
            return out
        }

        private fun metaFile() = File(dir, "meta.json")

        private fun readMeta(): JSONObject? = runCatching {
            val f = metaFile()
            if (f.isFile) JSONObject(f.readText()) else null
        }.getOrNull()

        private fun writeMeta(updated: Long, logosLm: String, chLm: String) {
            runCatching {
                val o = JSONObject()
                    .put("v", INDEX_VERSION)
                    .put("updated", updated)
                    .put("logos_lm", logosLm)
                    .put("ch_lm", chLm)
                val f = metaFile()
                val tmp = File(f.path + ".tmp")
                tmp.writeText(o.toString())
                if (!tmp.renameTo(f)) {
                    f.delete()
                    tmp.renameTo(f)
                }
            }
        }

        /** GET with If-Modified-Since; 304 reuses the cached raw file. Returns (body, Last-Modified) or null on failure. */
        private fun fetchConditional(url: String, file: File, lastModified: String): Pair<String, String>? =
            runCatching {
                val b = Request.Builder().url(url)
                if (lastModified.isNotBlank()) b.header("If-Modified-Since", lastModified)
                http.newCall(b.build()).execute().use { resp ->
                    when {
                        resp.code == 304 -> {
                            val text = if (file.isFile) runCatching { file.readText() }.getOrNull() else null
                            if (text != null) Pair(text, lastModified) else null
                        }
                        resp.isSuccessful -> {
                            val text = resp.body?.string() ?: return@runCatching null
                            runCatching { file.parentFile?.mkdirs(); file.writeText(text) }
                            Pair(text, resp.header("Last-Modified") ?: "")
                        }
                        else -> null
                    }
                }
            }.getOrNull()

        private companion object {
            const val INDEX_VERSION = 2
            const val WEEK_MS = 7L * 24 * 3600_000L
            const val BACKOFF_MS = 5L * 60_000L
        }
    }

    // ------------------------------------------------------------------ movie / series / adult covers

    /**
     * Cover URL for movies, series AND adult content — the desktop provider
     * chain (iptv/metadata.py) in compact form:
     *
     *  * normal titles: TMDb (keyed) → TVmaze (keyless, series)
     *  * adult titles:  TPDB (keyed, parse mode + JAV shortcut) → StashDB
     *    (keyed, GraphQL, combined-scored) — wrong posters are worse than
     *    none, so every hit is verified locally like the desktop does.
     *
     * Cached in memory and on disk (v2 format with per-entry timestamps:
     * hits live forever, misses expire after 48 h). Failures are never
     * persisted. The disk file is loaded lazily OFF the main thread (the
     * 1.4.0 `init {}` forced the parse during composition — jank the moment
     * a flush call site existed). Call [flush] to persist (the resolver
     * debounces; Activity onStop forces one).
     */
    class Covers(
        private val tmdbKey: String,
        private val tpdbKey: String = "",
        private val stashKey: String = "",
        cacheDir: java.io.File? = null,
        private val apiGate: PaceGate = TokenBucket(5.0),
        private val tpdbGate: PaceGate = MinIntervalGate(250),
    ) {

        private data class DiskEntry(val url: String, val ts: Long) {
            val result: ArtResult
                get() = if (url.isBlank()) ArtResult.Miss else ArtResult.Found(url)
        }

        private val cache = ConcurrentHashMap<String, ArtResult>()
        private val failures = ConcurrentHashMap<String, Long>()
        private var diskDirty = false
        private var diskLoaded = false
        private val diskFile: java.io.File? = cacheDir?.resolve("cover_cache.json")
        private val diskLock = Any()
        private val diskCache = HashMap<String, DiskEntry>()

        /** Blocking; call off the main thread (resolver workers / flushNow). */
        fun prewarm() {
            synchronized(diskLock) { ensureDiskLoadedLocked() }
        }

        /** Blocking persist of the disk cache. */
        fun flush() {
            synchronized(diskLock) {
                ensureDiskLoadedLocked()
                if (!diskDirty) return
                runCatching {
                    diskFile?.let { f ->
                        val entries = JSONObject()
                        diskCache.forEach { (k, v) ->
                            entries.put(k, JSONArray().put(v.url).put(v.ts))
                        }
                        val o = JSONObject().put("_v", 2).put("e", entries)
                        val tmp = java.io.File(f.path + ".tmp")
                        tmp.writeText(o.toString())
                        if (!tmp.renameTo(f)) {
                            f.delete()
                            tmp.renameTo(f)
                        }
                        diskDirty = false
                    }
                }
            }
        }

        /** Suspends on the pacing gates. Call off the main thread. */
        suspend fun cover(
            title: String,
            year: String,
            isSeries: Boolean,
            rawName: String = "",
            adult: Boolean = false,
        ): ArtResult {
            val key = coverKey(title, year, isSeries, adult)
            cache[key]?.let { return it }
            val failedAt = failures[key]
            val now = System.currentTimeMillis()
            if (failedAt != null && now - failedAt < FAIL_RETRY_MS) return ArtResult.Failed
            synchronized(diskLock) {
                ensureDiskLoadedLocked()
                diskCache[key]?.let {
                    cache[key] = it.result
                    return it.result
                }
            }
            if (failedAt != null) failures.remove(key) // retrying a past failure
            val result = lookup(title, year, isSeries, rawName.ifBlank { title }, adult)
            when (result) {
                is ArtResult.Found, ArtResult.Miss -> {
                    cache[key] = result
                    synchronized(diskLock) {
                        if (diskCache.size < 8000) {
                            diskCache[key] = DiskEntry(
                                if (result is ArtResult.Found) result.url else "",
                                System.currentTimeMillis(),
                            )
                            diskDirty = true
                        }
                    }
                }
                ArtResult.Failed -> {
                    if (failures.size > 4000) failures.clear()
                    failures[key] = System.currentTimeMillis()
                }
            }
            return result
        }

        /** Load the disk cache once, off the main thread (holds diskLock). */
        private fun ensureDiskLoadedLocked() {
            if (diskLoaded) return
            diskLoaded = true
            runCatching {
                diskFile?.takeIf { it.isFile }?.let { f ->
                    val o = JSONObject(f.readText())
                    if (o.optInt("_v", 1) >= 2) {
                        val e = o.optJSONObject("e") ?: return@runCatching
                        val now = System.currentTimeMillis()
                        e.keys().forEach { k ->
                            val arr = e.optJSONArray(k) ?: return@forEach
                            val url = arr.optString(0)
                            val ts = arr.optLong(1)
                            // Misses expire (provider data changes); hits are stable URLs.
                            if (url.isNotBlank() || now - ts < NEG_TTL_MS) {
                                diskCache[k] = DiskEntry(url, ts)
                            }
                        }
                    } else {
                        // v1 legacy (flat key→url; never actually written —
                        // 1.4.0's flush had no call site): keep only hits.
                        o.keys().forEach { k ->
                            o.optString(k).takeIf { it.isNotBlank() }?.let { diskCache[k] = DiskEntry(it, 0L) }
                        }
                    }
                }
            }
        }

        private suspend fun lookup(title: String, year: String, isSeries: Boolean, raw: String, adult: Boolean): ArtResult {
            var anyFailed = false
            if (adult) {
                if (tpdbKey.isNotBlank()) {
                    tpdbGate.acquire() // TPDB resets connections on rapid sequential requests
                    when (val r = tpdbCover(raw, title, year)) {
                        is ArtResult.Found -> return r
                        ArtResult.Failed -> anyFailed = true
                        ArtResult.Miss -> {}
                    }
                }
                if (stashKey.isNotBlank()) {
                    apiGate.acquire()
                    when (val r = stashCover(raw, title, year)) {
                        is ArtResult.Found -> return r
                        ArtResult.Failed -> anyFailed = true
                        ArtResult.Miss -> {}
                    }
                }
            } else {
                if (tmdbKey.isNotBlank()) {
                    apiGate.acquire()
                    when (val r = tmdbCover(title, year, isSeries)) {
                        is ArtResult.Found -> return r
                        ArtResult.Failed -> anyFailed = true
                        ArtResult.Miss -> {}
                    }
                }
                if (isSeries) {
                    apiGate.acquire()
                    when (val r = tvmazeCover(title)) {
                        is ArtResult.Found -> return r
                        ArtResult.Failed -> anyFailed = true
                        ArtResult.Miss -> {}
                    }
                }
            }
            return if (anyFailed) ArtResult.Failed else ArtResult.Miss
        }

        private suspend fun tmdbCover(title: String, year: String, isSeries: Boolean): ArtResult {
            return runCatching {
                val path = if (isSeries) "tv" else "movie"
                val url = "https://api.themoviedb.org/3/search/$path" +
                    "?api_key=$tmdbKey&query=${enc(title)}" +
                    (if (year.isNotBlank()) "&year=${enc(year)}" else "")
                val body = http.newCall(Request.Builder().url(url).build()).execute().use { resp ->
                    if (!resp.isSuccessful) {
                        return@runCatching if (resp.code in TRANSIENT_CODES) ArtResult.Failed else ArtResult.Miss
                    }
                    resp.body?.string() ?: return@runCatching ArtResult.Failed
                }
                val poster = JSONObject(body)
                    .optJSONArray("results")?.optJSONObject(0)?.optString("poster_path") ?: ""
                if (poster.isNotBlank()) ArtResult.Found("https://image.tmdb.org/t/p/w342$poster") else ArtResult.Miss
            }.getOrDefault(ArtResult.Failed)
        }

        private suspend fun tvmazeCover(title: String): ArtResult = runCatching {
            val url = "https://api.tvmaze.com/singlesearch/shows?q=${enc(title)}"
            val body = http.newCall(Request.Builder().url(url).build()).execute().use { resp ->
                if (resp.code == 404) return@runCatching ArtResult.Miss // singlesearch's "not found"
                if (!resp.isSuccessful) {
                    return@runCatching if (resp.code in TRANSIENT_CODES) ArtResult.Failed else ArtResult.Miss
                }
                resp.body?.string() ?: return@runCatching ArtResult.Failed
            }
            val img = JSONObject(body).optJSONObject("image")?.optString("original") ?: ""
            if (img.isBlank()) ArtResult.Miss else ArtResult.Found(img)
        }.getOrDefault(ArtResult.Failed)

        // ---- TPDB (theporndb.net) — adult movies/scenes/JAV ----

        private suspend fun tpdbCover(raw: String, title: String, year: String): ArtResult {
            // JAV code shortcut: an exact catalogue key, trusted as-is.
            javCode(raw)?.let { code ->
                tpdbRows("/jav", "q=$code")
                    ?.firstOrNull { it.optJSONObject("posters") != null || it.optString("poster").isNotBlank() }
                    ?.let { row -> tpdbPoster(row)?.let { return ArtResult.Found(it) } }
            }
            // Parse mode: built for studio-led "<Studio> <scene>" names.
            val rows = tpdbRows("/scenes", "parse=${enc(raw)}") ?: return ArtResult.Failed
            val verified = rows.filter { tpdbVerify(raw, it) }
            val url = bestByYear(verified, year)?.let { tpdbPoster(it) }
            return if (url.isNullOrBlank()) ArtResult.Miss else ArtResult.Found(url)
        }

        private fun tpdbRows(path: String, query: String): List<JSONObject>? = runCatching {
            val url = "https://api.theporndb.net$path?$query"
            val req = Request.Builder().url(url)
                .header("Authorization", "Bearer $tpdbKey")
                .header("Accept", "application/json")
                .build()
            val body = http.newCall(req).execute().use { resp ->
                if (resp.code == 401 || resp.code == 403) return@runCatching null
                if (!resp.isSuccessful) return@runCatching null
                resp.body?.string() ?: return@runCatching null
            }
            val arr = JSONObject(body).optJSONArray("data") ?: return@runCatching null
            (0 until arr.length().coerceAtMost(12)).mapNotNull { arr.optJSONObject(it) }
        }.getOrNull()

        private fun tpdbVerify(name: String, row: JSONObject): Boolean {
            val rowTitle = row.optString("title")
            return titleContainment(name, rowTitle) >= 0.7 || titleSimilarity(name, rowTitle) >= 0.5
        }

        private fun tpdbPoster(row: JSONObject): String? {
            row.optJSONObject("posters")?.let { p ->
                for (size in listOf("large", "full", "medium", "small")) {
                    p.optString(size).takeIf { it.isNotBlank() }?.let { return it }
                }
            }
            return row.optString("poster").takeIf { it.isNotBlank() }
        }

        // ---- StashDB — community adult DB (GraphQL) ----

        private suspend fun stashCover(raw: String, title: String, year: String): ArtResult {
            val rows = stashScenes(title) ?: return ArtResult.Failed
            val scored = rows
                .map { it to stashScore(raw, year, it) }
                .filter { it.second >= 0.5 }
                .sortedByDescending { it.second }
            val url = scored.firstOrNull()?.first?.let { row ->
                row.optJSONArray("images")?.optJSONObject(0)?.optString("url")?.takeIf { it.startsWith("http") }
            }
            return if (url == null) ArtResult.Miss else ArtResult.Found(url)
        }

        private fun stashScenes(term: String): List<JSONObject>? = runCatching {
            val gql = JSONObject()
                .put(
                    "query",
                    "query(\$term: String!) { searchScenes(term: \$term) { scenes { title release_date " +
                        "images { url } studio { name } } } }",
                )
                .put("variables", JSONObject().put("term", term))
            val body = http.newCall(
                Request.Builder().url("https://stashdb.org/graphql")
                    .header("ApiKey", stashKey)
                    .post(
                        gql.toString()
                            .toRequestBody("application/json; charset=utf-8".toMediaType()),
                    )
                    .build(),
            ).execute().use { resp ->
                if (!resp.isSuccessful) return@runCatching null
                resp.body?.string() ?: return@runCatching null
            }
            val scenes = JSONObject(body).optJSONObject("data")?.optJSONObject("searchScenes")
                ?.optJSONArray("scenes") ?: return@runCatching null
            (0 until scenes.length().coerceAtMost(10)).mapNotNull { scenes.optJSONObject(it) }
        }.getOrNull()

        /** 0.5*title + 0.3*year + 0.2*studio (desktop _combined_score). */
        private fun stashScore(name: String, year: String, row: JSONObject): Double {
            val t = titleSimilarity(name, row.optString("title"))
            val y = if (year.isNotBlank() && row.optString("release_date").startsWith(year)) 1.0 else 0.0
            val studio = row.optJSONObject("studio")?.optString("name") ?: ""
            val s = if (studio.isNotBlank() && name.lowercase().contains(studio.lowercase())) 1.0 else 0.0
            return 0.5 * t + 0.3 * y + 0.2 * s
        }

        private companion object {
            const val FAIL_RETRY_MS = 30_000L
            const val NEG_TTL_MS = 48L * 3600_000L
            val TRANSIENT_CODES = setOf(408, 429, 500, 502, 503, 504)
        }
    }

    // ------------------------------------------------------------------ matching helpers (desktop metadata.py ports)

    /** Normalized JAV code ("abp00123" → "ABP-123"), else null. Desktop
     *  _JAV_CODE_RE: letters(2-6) + optional sep + digits(2-5), years skipped. */
    fun javCode(name: String): String? {
        val cleaned = JAV_SUFFIX_RE.replace(name, " ")
        for (m in Regex("\\b([A-Za-z]{2,6})[-\\s_]?(\\d{2,5})\\b").findAll(cleaned)) {
            val digits = m.groupValues[2]
            if (Regex("(?:19|20)\\d{2}").matches(digits)) continue
            return "${m.groupValues[1].uppercase()}-$digits"
        }
        return null
    }

    /** Token-set Jaccard similarity, 0..1 (desktop title_similarity). */
    fun titleSimilarity(a: String, b: String): Double {
        val ta = tokens(a)
        val tb = tokens(b)
        if (ta.isEmpty() || tb.isEmpty()) return 0.0
        val inter = ta.intersect(tb).size.toDouble()
        return inter / ta.union(tb).size
    }

    /** Fraction of b's tokens present in a (desktop title_containment). */
    fun titleContainment(a: String, b: String): Double {
        val tb = tokens(b)
        if (tb.isEmpty()) return 0.0
        val ta = tokens(a)
        return tb.count { it in ta }.toDouble() / tb.size
    }

    private fun tokens(s: String): Set<String> =
        s.lowercase().split(Regex("[^a-z0-9]+")).filter { it.length > 1 && it !in STOPWORDS }.toSet()

    private fun bestByYear(rows: List<JSONObject>, year: String): JSONObject? {
        if (rows.isEmpty()) return null
        if (year.isNotBlank()) rows.firstOrNull { it.optString("date").startsWith(year) }?.let { return it }
        return rows.firstOrNull { it.optJSONObject("posters") != null } ?: rows.first()
    }

    // ------------------------------------------------------------------ shared helpers

    /**
     * Filename → (title, year, isSeries) — full desktop `clean_title` parity
     * (verified live 1.4.2: the old light version left "(2013) -LAMA [ ] [ ]
     * [WEB]" in the query and TMDb answered nothing). Strips audio-channel
     * layouts, SxxEyy/season markers, country prefixes, quality/codec tags,
     * bracketed release-tag groups, the parenthesized YEAR (TMDb returns no
     * results for "(2006)" in the query — the year rides its own parameter)
     * and trailing release-group suffixes, iteratively.
     */
    fun titleFrom(name: String): Triple<String, String, Boolean> {
        var stem = name.substringBeforeLast('.')
        val series = SE_EP.containsMatchIn(stem)
        stem = AUDIO_CH_RE.replace(stem, " ") // "5.1" before dots become spaces
        stem = stem.replace("[._]", " ")
        stem = stem.replaceFirst(COUNTRY_PREFIX, "")
        stem = SE_EP.replace(stem, " ").replace(SEASON_TAG, " ")
        stem = QUALITY_RE.replace(stem, " ")
        stem = stripTagBrackets(stem)
        val year = Regex("\\b(19\\d{2}|20\\d{2})\\b").find(stem)?.groupValues?.get(1) ?: ""
        stem = PAREN_YEAR_RE.replace(stem, " ")
        stem = stem.replace(Regex("\\s+"), " ").trim()
        while (true) { // "-LAMA" → gone; "A -B -C" needs iterations
            val next = GROUP_SUFFIX_RE.replace(stem, " ").trim()
            if (next == stem) break
            stem = next
        }
        return Triple(stem.trim(' ', '-', '_'), year, series)
    }

    /**
     * Drop […] groups that contain only release tags ("[1080p] [WEBRip]
     * [WEB]" → gone); inside brackets bare web/dvd/hd are safe to treat as
     * tags (desktop _strip_tag_brackets). Stray bracket chars removed after.
     */
    private fun stripTagBrackets(t: String): String {
        var s = t
        while (true) {
            val next = BRACKET_GROUP_RE.replace(s) { m ->
                val inner = BRACKET_TAG_RE.replace(m.groupValues[1], "").trim(' ', '-', '_', '.', ',')
                if (inner.isEmpty()) "" else m.value
            }
            if (next == s) break
            s = next
        }
        return s.replace('[', ' ').replace(']', ' ')
    }

    /** Cache/join key for a cover lookup (mirrors the disk cache keys). */
    fun coverKey(title: String, year: String, isSeries: Boolean, adult: Boolean): String =
        "${if (adult) "a" else if (isSeries) "s" else "m"}|$title|$year"

    // ---- logo name normalization (desktop _normalize / _split_country) ----

    /** 'BBC One HD' / 'bbc-one' → 'bbcone' (decorations dropped). */
    fun normalizeLogoName(name: String): String {
        var s = name.lowercase().replace("&", "and")
        s = BRACKETED_RE.replace(s, " ")
        val sb = StringBuilder()
        for (m in TOKEN_RE.findAll(s)) {
            if (m.value !in LOGO_NOISE_TOKENS) sb.append(m.value)
        }
        return sb.toString()
    }

    /**
     * Strip playlist decorations: '60 UK: Sky News FHD' → ("skynews", "uk").
     * Only recognised country words / short codes are stripped, so
     * 'Discovery Channel' keeps its first word (desktop _split_country).
     */
    fun splitCountry(nameRaw: String): Pair<String, String> {
        var s = nameRaw.lowercase()
        s = BRACKETED_RE.replace(s, " ") // (Meo), [VIP], (D)
        s = LEADING_NUM_RE.replaceFirst(s, " ") // leading channel number
        var country = ""
        var rounds = 0
        while (rounds < 2) { // e.g. "10 UK FHD  TNT Sport 1"
            rounds++
            var sep = SEP_PREFIX_RE.find(s)
            if (sep != null) {
                val tok = sep.groupValues[1]
                if (!(tok.length <= 3 || COUNTRY_WORDS.containsKey(tok))) sep = null
            }
            var m = sep
            if (m == null) {
                val ws = WS_PREFIX_RE.find(s)
                if (ws != null && !COUNTRY_WORDS.containsKey(ws.groupValues[1])) break
                m = ws
            }
            if (m == null) break
            val tok = m.groupValues[1]
            s = m.groupValues[2]
            if (country.isEmpty()) country = COUNTRY_WORDS[tok] ?: if (tok.length <= 3) tok else ""
        }
        return Pair(normalizeLogoName(s), country)
    }

    private fun enc(s: String): String = java.net.URLEncoder.encode(s, "UTF-8")

    /** Group names whose entries are movies/series (cover lookup territory). */
    val VOD_GROUP_HINT = Regex("(?i)vod|movie|series|cinema")

    /** Adult group/name markers → the TPDB/StashDB provider chain. */
    val ADULT_HINT = Regex("(?i)\\bxxx\\b|adult|porn|18\\+|jav")

    /** Shared HTTP lane for every provider call (also used by the resolver's favicon checks). */
    internal val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()

    private val STOPWORDS = setOf(
        "the", "a", "an", "and", "of", "in", "on", "for", "to", "hd", "4k",
        "fhd", "uhd", "xxx", "porn", "vol", "part", "uncensored", "censored",
    )

    private val LOGO_NOISE_TOKENS = setOf(
        "hd", "hdtv", "fhd", "uhd", "sd", "2k", "4k", "8k", "hq", "lq", "raw",
        "hevc", "h264", "h265", "1080p", "1080i", "1080", "720p", "720",
        "480p", "2160p", "2160", "vip", "backup", "alt", "multi",
    )

    private val COUNTRY_WORDS = mapOf(
        "uk" to "uk", "gb" to "uk", "england" to "uk", "unitedkingdom" to "uk",
        "us" to "us", "usa" to "us", "unitedstates" to "us", "ca" to "ca", "canada" to "ca",
        "pt" to "pt", "portugal" to "pt", "fr" to "fr", "france" to "fr",
        "es" to "es", "spain" to "es", "espana" to "es", "de" to "de", "germany" to "de",
        "deutschland" to "de", "it" to "it", "italy" to "it", "italia" to "it",
        "nl" to "nl", "netherlands" to "nl", "be" to "be", "belgium" to "be",
        "br" to "br", "brazil" to "br", "brasil" to "br", "tr" to "tr", "turkey" to "tr",
        "pl" to "pl", "poland" to "pl", "ro" to "ro", "romania" to "ro",
        "ru" to "ru", "russia" to "ru", "in" to "in", "india" to "in",
        "id" to "id", "indonesia" to "id", "au" to "au", "australia" to "au",
        "ie" to "ie", "ireland" to "ie", "ch" to "ch", "at" to "at", "se" to "se",
        "no" to "no", "dk" to "dk", "fi" to "fi", "gr" to "gr", "greece" to "gr",
        "mx" to "mx", "mexico" to "mx", "ar" to "ar", "argentina" to "ar",
    )

    private val RASTER_FORMATS = setOf("PNG", "WEBP", "JPEG", "JPG", "GIF")

    private val JAV_SUFFIX_RE = Regex(
        "(?i)[-_.\\s]+(?:leak|uncensored|uncut|uc|c|ch|hd|fhd|uhd|4k|part\\s*\\d+|p\\d+|cd\\d+|disc\\d+)",
    )

    private val SE_EP = Regex("[Ss]\\d{1,2}[ ._-]?[Ee]\\d{1,3}")
    private val COUNTRY_PREFIX = Regex("^(\\[|\\(|\\|)?[A-Z]{2}(\\]|\\)|\\|)?[:\\s\\-]+")
    private val SEASON_TAG = Regex("(?i)\\bseason[ ._]?\\d{1,2}\\b")

    /** Desktop _QUALITY_TAGS verbatim (broader than the 1.3.7 port). */
    private const val QUALITY_ALTERNATION =
        "\\b(480|720|1080|2160)[pi]?\\b" +
            "|\\b(x264|x265|h264|h265|hevc|av1|vp9|divx|xvid)\\b" +
            "|\\b(hdtv|web[-]?dl|webrip|bluray|blu-ray|bdrip|brrip|dvdrip|cam|ts|tc)\\b" +
            "|\\b(remux|atmos|truehd|dts|ddp|dd|aac\\d?|mp3|flac)\\b" +
            "|\\b(multi|dual|subbed|subs|internal|proper|repack)\\b" +
            "|\\b(10bit|8bit|sdr|hdr|hdr10|dolby)\\b" +
            "|\\b(nordic|vostfr|dubbed|amzn|dsnp|hmax|atvp)\\b"

    private val QUALITY_RE = Regex(QUALITY_ALTERNATION, RegexOption.IGNORE_CASE)

    /** Bracket-safe extra tags (bare web/dvd/hd are tags only inside […]). */
    private val BRACKET_TAG_RE = Regex(
        "$QUALITY_ALTERNATION|\\b(web|dvd|bd|uhd|4k|fhd|hd|sd|hdrip|hdts|x265|10-bit|5\\.1)\\b",
        RegexOption.IGNORE_CASE,
    )
    private val BRACKET_GROUP_RE = Regex("\\[([^\\]]*)\\]")

    /** "5.1", "DDP5 1", "DTS 7.1" (desktop _AUDIO_CH_RE). */
    private val AUDIO_CH_RE = Regex("\\b(?:ddp?|dts|aac|eac3|ac3)?\\s?[2578][.\\s][01]\\b", RegexOption.IGNORE_CASE)

    /** "(2013)" — the year rides its own API parameter (desktop rule). */
    private val PAREN_YEAR_RE = Regex("\\((?:19|20)\\d{2}\\)")

    /** Trailing release-group tag, "-LAMA"/"–WORLD" (desktop _GROUP_SUFFIX_RE). */
    private val GROUP_SUFFIX_RE = Regex("\\s[-–][A-Za-z0-9]{2,15}\\s*$")

    private val BRACKETED_RE = Regex("\\([^)]*\\)|\\[[^\\]]*\\]")
    private val LEADING_NUM_RE = Regex("^[\\s|:.\\-]*\\d{1,4}\\b")
    private val SEP_PREFIX_RE = Regex("^\\s*([a-z]{2,14})\\s*[|:]+\\s*(\\S.*)")
    private val WS_PREFIX_RE = Regex("^\\s*([a-z]{2,14})\\s+(\\S.*)")
    private val TOKEN_RE = Regex("[a-z0-9]+")

    private const val LOGOS_URL = "https://iptv-org.github.io/api/logos.json"
    private const val CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
    private const val TAG = "IptvMetadata"
}

// ---------------------------------------------------------------------- pacing gates (§14 P2)

/** Pluggable pacing for provider calls (unit-testable without delays). */
interface PaceGate {
    suspend fun acquire()
}

/** Token bucket: at most [ratePerSecond] acquisitions/s (desktop _RateLimiter). */
class TokenBucket(private val ratePerSecond: Double) : PaceGate {
    private val mu = Mutex()
    private var tokens = 1.0
    private var lastNs = System.nanoTime()

    override suspend fun acquire() {
        while (true) {
            val waitMs = mu.withLock {
                val now = System.nanoTime()
                tokens = minOf(ratePerSecond, tokens + (now - lastNs) / 1e9 * ratePerSecond)
                lastNs = now
                if (tokens >= 1.0) {
                    tokens -= 1.0
                    return
                }
                (((1.0 - tokens) / ratePerSecond) * 1000.0).toLong().coerceIn(1, 5000)
            }
            delay(waitMs)
        }
    }
}

/** At least [minIntervalMs] between acquisitions (TPDB's connection-reset quirk). */
class MinIntervalGate(private val minIntervalMs: Long) : PaceGate {
    private val mu = Mutex()
    private var nextAtMs = 0L

    override suspend fun acquire() {
        while (true) {
            val waitMs = mu.withLock {
                val now = System.currentTimeMillis()
                if (now >= nextAtMs) {
                    nextAtMs = now + minIntervalMs
                    return
                }
                nextAtMs - now
            }
            delay(waitMs)
        }
    }
}
