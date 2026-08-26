# DeepFlux Search & Download Strategy Study

August 13, 2026

---

## 1. Architecture Overview

DeepFlux uses a **multi-tier search cascade** that tries increasingly broad sources until results are found:

```
User Query
  │
  ├─ 1. Jackett (private indexers → public batches)
  │     ├─ Query variants: original → strip quality tags → strip year
  │     ├─ Private tier: ALL private indexers in parallel (ThreadPoolExecutor, 6 workers)
  │     │   └─ Early exit if best private result has ≥ min_seeders (default 10)
  │     └─ Public tier: popularity-ordered batches of 5
  │         └─ Early exit when min_results (5) or min_seeders (10) reached
  │
  ├─ 2. Source-specific web search (user-configured torrent sites)
  │     ├─ Private sources first (sorted by popularity)
  │     ├─ Public sources second
  │     └─ Quick mode: single most popular source → auto-escalates to full sweep
  │
  ├─ 3. RSS feed matching (subscribed feeds, parallel)
  │
  └─ 4. Generic web search (DDG → Brave → Perplexity)
        └─ Query + " torrent" suffix
```

## 2. Jackett Integration — The Primary Path

### How It Works

Jackett is a torrent indexer aggregator that proxies search queries to dozens of trackers. DeepFlux treats it as the **preferred search path** because:

1. **Private trackers first**: Private indexers typically have better-seeded, higher-quality content
2. **Parallel queries**: All private indexers are queried simultaneously (max 6 workers)
3. **Smart early exit**: If the best private result has ≥10 seeders, public trackers are skipped entirely
4. **Batch public tier**: Public indexers are queried in batches of 5, stopping when enough results are found

### Query Optimization

```
" Inception 2010 1080p x264 BluRay "
  → "Inception 2010 1080p x264 BluRay"     (original)
  → "Inception 2010"                         (stripped quality tags)
  → "Inception"                              (stripped year)
```

Quality tags stripped: `2160p, 1080p, 720p, 480p, x264, x265, h264, h265, hevc, bluray, blu-ray, web-dl, webrip, hdrip, dvdrip, proper, repack, remux, hdr, dts, aac`

### Failure Resilience

- **Per-indexer failures**: Indexers that timeout or error are marked "dead" and skipped for ALL remaining query variants + auto deep sweep
- **Total failure**: When EVERY indexer fails → explicit error returned to agent (NOT silently treated as "0 results")
- **Failures never cached**: So retries always re-attempt
- **Auto deep sweep**: Quick pass (top source only) that finds nothing → automatically runs full sweep

### Key Parameters

| Parameter | Default | Location |
|---|---|---|
| Jackett timeout | 60s | `config.py:97` |
| Private workers | 6 parallel | `tools.py:1094` |
| Public batch size | 5 | `config.py` |
| Min seeders (private) | 10 | `config.py:251` |
| Min results (public) | 5 | `config.py:250` |
| Inter-request throttle | 0.15s | `tools.py:2047` |
| Search cache TTL | 300s (5 min) | `tools.py:686` |

## 3. Web Search Chain

### Provider Cascade

```
DuckDuckGo (always, keyless)
  → Brave (only if brave_api_key is set)
    → Perplexity (only if api_key is set)
```

**First non-empty provider wins.** `used_fallback=true` marks backup responses.

### Rate Limiting

- **With Brave key**: 1.0s spacing (Brave free tier = 1 qps)
- **Without Brave key**: 0.2s spacing
- Thread-safe via `threading.Lock`

### Perplexity Bonus

Perplexity results include a synthesized `answer` field (LLM summary of search results). DeepFlux also extracts magnet links from Perplexity answers.

### Generic Web Search for Torrents

When all other paths fail, DeepFlux appends `" torrent"` to the query and runs it through the web search chain. This catches torrents indexed by general search engines but not by Jackett.

## 4. Web Fetch — Content Extraction

`web_fetch(url)` fetches a page and extracts actionable content from raw HTML (before tag stripping):

### Extraction Patterns

| Type | Regex Pattern | Limit |
|---|---|---|
| **Magnet links** | `magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^\"\s<>']*` | 10 |
| **Torrent URLs** | `href=["']([^"']+\.torrent[^"']*)["']` | 10 |
| **Download links** | `.zip, .rar, .7z, .iso, .img, .exe, .msi, .apk, .dmg, .pkg, .deb, .rpm, .tar, .gz, .xz, .mp4, .mkv, .avi, .mov, .mp3, .flac, .wav, .pdf, .epub, .m3u8, .mpd` | 10 |

All URLs are resolved against the page's base URL via `urljoin()`.

### Stream Detection

`.m3u8` (HLS) and `.mpd` (DASH) are included in download link extraction. These are routed to `add_download` → `engine.add_stream_job()` which downloads and remuxes segments via FFmpeg.

### Text Extraction

- Script/style blocks removed
- HTML tags stripped
- Entities unescaped
- Whitespace collapsed
- Truncated to `max_chars` (default 8000)

## 5. Download Pipeline

### Magnet/Torrent Downloads

```
add_magnet(uri) → engine.add_magnet()
  → libtorrent session
  → DHT + tracker announces
  → sequential download
```

```
add_torrent_file(path_or_url) → if URL: download .torrent → parse → engine.add_magnet()
  → persist to ~/.deeptorrent/torrents/{hash}.torrent for resume
```

### Direct Downloads (HTTP)

```
add_download(url) → DownloadEngine.add_job()
  → HEAD probe (checks Accept-Ranges, Content-Length)
  → If ranges supported & file > 1MB:
      → split into N segments (max 8 connections, 512KB each)
      → parallel SegmentWorker threads
      → global bandwidth limiter (token bucket)
  → If no ranges or small file:
      → single GET stream
```

### Stream Downloads (HLS/DASH)

```
add_download(url) → .m3u8/.mpd detection → DownloadEngine.add_stream_job()
  → parse manifest
  → download segments in parallel
  → FFmpeg remux to MP4
  → cleanup temp segments
```

### Key Download Parameters

| Parameter | Default |
|---|---|
| Max concurrent jobs | 3 |
| Max connections per job | 8 |
| Segment threshold | 1 MB |
| Rebalance interval | 2s |
| Save interval | 5s |
| Max segment retries | 3 |

## 6. Search Strategy Recommendations

### For the Agent System Prompt

The current system prompt already guides the agent well (lines 81-97 of `loop.py`). Key behaviors to preserve:

1. **Specific queries → search_indexers directly** (skip web_search)
2. **Vague queries → web_search first** to identify exact title, then search_indexers
3. **Always offer web_fetch** on search results — the agent should tell the user "use web_fetch on result #3 to extract the magnet"
4. **Zero results → retry with deep=true** (the auto-escalation handles this, but the agent should know)

### Potential Improvements

1. **Add more file extensions to web_fetch**: Consider adding `.AppImage`, `.snap`, `.flatpakref` for Linux software downloads
2. **Brave API key**: Strongly recommend users add one — it's free (2000 queries/month) and doubles search coverage
3. **Perplexity for ambiguous queries**: When the user describes content vaguely ("that movie with the spinning top"), Perplexity's synthesized answer often identifies the title better than keyword search
4. **Cache warming**: Pre-fetch popular searches during idle time to reduce first-query latency
5. **Dead indexer recovery**: Currently dead indexers are skipped for the entire search session — consider a cooldown-based recovery (e.g., retry after 5 minutes)

## 7. Direct Download Discovery Strategy

### Current Approach

The `web_fetch` tool extracts download links from HTML by matching file extensions in `href` attributes. This works well for:

- **File hosting sites** (MediaFire, Mega, Google Drive shared links)
- **Software download pages** (SourceForge, GitHub Releases, official sites)
- **Streaming sites** (HLS .m3u8, DASH .mpd manifests)

### Limitations

1. **JavaScript-rendered links**: Many modern sites load download buttons via JS — `web_fetch` only sees raw HTML
2. **CAPTCHA/redirect gates**: Sites that require human verification before serving the download URL
3. **Obfuscated URLs**: Some sites encode or split URLs to prevent scraping
4. **Rate limiting**: Aggressive fetching can trigger IP bans

### Recommended Strategy for the Agent

When searching for direct downloads:

1. **Check official sources first** (project homepage, GitHub Releases)
2. **Then check known mirrors** (SourceForge, FossHub, FileHorse)
3. **Use web_search** with `"direct download" OR "DDL"` qualifiers
4. **web_fetch** promising pages to extract links
5. **Verify checksums** when available (SHA256/MD5 from official site)
6. **Fall back to torrents** if no direct download is available

## 8. Key Files Reference

| Component | File | Key Lines |
|---|---|---|
| Search cascade | `agent/tools.py` | 890-1010 |
| Jackett tiered search | `agent/tools.py` | 1046-1210 |
| Query variants | `agent/tools.py` | 872-888 |
| Web search chain | `agent/tools.py` | 1822-1869 |
| Web fetch extraction | `agent/tools.py` | 1443-1493 |
| Download engine | `dlmgr/engine.py` | 212-398 |
| Jackett sync | `infra/jackett.py` | 125-237 |
| Agent system prompt | `agent/loop.py` | 64-145 |
| Tool classification | `agent/loop.py` | 20-56 |
| LLM provider presets | `config.py` | 30-74 |
| Source config | `config.py` | 241-252 |
