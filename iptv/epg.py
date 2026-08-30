"""EPG (XMLTV) download, parse, and cache.

XMLTV is a verbose XML format. We parse it with ``xml.etree.ElementTree``
iterparse so large guides stream without huge memory use, and we only keep
``<programme>`` rows (channel_id, start, stop, title, desc) in the SQLite
cache — the full XML is discarded after parsing.

Download + parse run in a worker thread; progress is reported via a callback.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from typing import Callable, List, Optional, Tuple

import requests

from .cache import IPTVCache

logger = logging.getLogger(__name__)

ProgressFn = Callable[[int, Optional[int]], None]


def _parse_xmltv_date(s: str) -> int:
    """XMLTV dates look like '20240101123000 +0000' -> epoch seconds.

    The timezone offset is honored; without it the timestamp would be
    interpreted as local time and programs would be shifted by the user's
    UTC offset. A missing offset is treated as UTC."""
    if not s:
        return 0
    from datetime import datetime, timezone
    parts = s.strip().split()
    try:
        dt = datetime.strptime(parts[0], "%Y%m%d%H%M%S")
        if len(parts) > 1:
            try:
                dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d%H%M%S %z")
            except ValueError:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, OverflowError):
        return 0


def parse_xmltv(
    path: str,
    on_progress: Optional[ProgressFn] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
    with_channels: bool = False,
):
    """Stream-parse an XMLTV file into a list of program dicts.

    With ``with_channels=True`` returns ``(programs, channels)`` where
    channels is ``[(channel_id, display_name)]`` from the guide's own
    ``<channel>`` directory — the surface for name-based tvg-id fallback
    matching.

    Two real-world provider faults are tolerated (verified against
    epg.mybunny.tv, 2026-08):

    * **Truncated downloads** — the server closes a chunked/gzipped response
      mid-document, so iterparse dies with ``ParseError: no element found``.
      A 100k-line partial guide beats none: keep every programme parsed so
      far instead of failing the whole update.
    * **Lying encoding declarations** — the header says UTF-8 but the payload
      holds latin-1 bytes (mojibake in titles otherwise). On a decode error
      we re-parse with an explicit latin-1 text wrapper; for text-mode input
      ElementTree ignores the XML declaration's encoding.
    """
    channels: List[tuple] = []
    programs, err = _parse_stream(ET.iterparse(path, events=("end",)),
                                  on_progress, is_cancelled, path,
                                  channels if with_channels else None)
    # Expat reports bytes that are invalid for the DECLARED encoding as a
    # generic ParseError "not well-formed (invalid token)" — not a
    # UnicodeDecodeError. That means the provider lied about UTF-8 (the
    # payload is latin-1), so retry through a latin-1 text wrapper (for
    # text-mode input ElementTree ignores the declaration) and keep whichever
    # pass got further. A truncation ("no element found") never retries.
    if err is not None and (isinstance(err, UnicodeDecodeError)
                            or "invalid token" in str(err)):
        import io
        logger.info("XMLTV payload is not valid for its declared encoding — "
                    "re-parsing as latin-1: %s", path)
        channels2: List[tuple] = []
        with io.open(path, "r", encoding="latin-1") as f:
            programs2, _err2 = _parse_stream(
                ET.iterparse(f, events=("end",)), on_progress, is_cancelled,
                path, channels2 if with_channels else None)
        if len(programs2) > len(programs):
            programs, channels = programs2, channels2
    if with_channels:
        return programs, channels
    return programs


def _parse_stream(
    events,
    on_progress: Optional[ProgressFn],
    is_cancelled: Optional[Callable[[], bool]],
    path: str,
    channels: Optional[List[tuple]] = None,
) -> Tuple[List[dict], Optional[Exception]]:
    programs: List[dict] = []
    count = 0
    err: Optional[Exception] = None
    try:
        # iterparse lets us drop cleared elements to keep memory bounded.
        for _ev, elem in events:
            if is_cancelled and is_cancelled():
                break
            if elem.tag == "channel" and channels is not None:
                cid = elem.get("id", "")
                dn = elem.find("display-name")
                if cid:
                    channels.append((cid, (dn.text or "") if dn is not None else ""))
                elem.clear()
                continue
            if elem.tag != "programme":
                # Free elements we don't need to keep memory low — but NEVER
                # clear <title>/<desc>/<display-name>: their end events fire
                # before the parent <programme>/<channel>'s, and clearing them
                # would wipe the text the extraction below is about to read.
                if elem.tag not in ("title", "desc", "display-name"):
                    elem.clear()
                continue
            ch = elem.get("channel", "")
            start = _parse_xmltv_date(elem.get("start", ""))
            end = _parse_xmltv_date(elem.get("stop", ""))
            title_el = elem.find("title")
            desc_el = elem.find("desc")
            programs.append(
                {
                    "channel_id": ch,
                    "start": start,
                    "end": end,
                    "title": (title_el.text or "") if title_el is not None else "",
                    "desc": (desc_el.text or "") if desc_el is not None else "",
                }
            )
            count += 1
            if on_progress and count % 1000 == 0:
                on_progress(count, None)
            elem.clear()
    except (ET.ParseError, UnicodeDecodeError) as exc:
        # Truncated/malformed tail — keep the partial guide (see docstring).
        err = exc
        logger.warning("XMLTV parse stopped early in %s (%s) — keeping %d programmes",
                       path, exc, len(programs))
    if on_progress:
        on_progress(count, None)
    return programs, err


class EPGManager:
    """Downloads, parses, and caches an XMLTV guide for a source."""

    def __init__(self, cache: IPTVCache) -> None:
        self.cache = cache
        # Bumped after every save — the manager's name->id map rebuilds on change.
        self.channels_version = 0

    def update_async(
        self,
        url: str,
        headers: Optional[dict] = None,
        on_done: Optional[Callable[[bool, int], None]] = None,
        on_progress: Optional[ProgressFn] = None,
    ) -> threading.Thread:
        """Download + parse ``url`` in a background thread.

        ``on_done(success, program_count)`` is invoked off-thread.
        """

        def _worker() -> None:
            ok = False
            count = 0
            tmp_path: Optional[str] = None
            try:
                hdrs = {"User-Agent": "DeepFlux-IPTV/1.0"}
                if headers:
                    hdrs.update(headers)
                # EPG servers are as flaky as playlist servers (connection
                # resets mid-download) — same 4-attempt backoff as _download_m3u.
                r = None
                for attempt in range(4):
                    try:
                        r = requests.get(url, headers=hdrs, timeout=60, stream=True)
                        r.raise_for_status()
                        break
                    except Exception as exc:
                        logger.warning("EPG download failed for %s (attempt %d/4): %s",
                                       url, attempt + 1, exc)
                        r = None
                        if attempt < 3:
                            time.sleep((2, 5, 15)[attempt])
                if r is None:
                    raise ValueError("EPG download failed after 4 attempts")
                fd, tmp_path = tempfile.mkstemp(suffix=".xmltv")
                with os.fdopen(fd, "wb") as f:
                    for chunk in r.iter_content(65536):
                        if chunk:
                            f.write(chunk)
                # Some providers serve gzipped XMLTV without a Content-Encoding
                # header — detect the gzip magic bytes and decompress.
                with open(tmp_path, "rb") as f:
                    magic = f.read(2)
                if magic == b"\x1f\x8b":
                    import gzip
                    gz_path = tmp_path + ".xml"
                    with gzip.open(tmp_path, "rb") as src, open(gz_path, "wb") as dst:
                        dst.write(src.read())
                    os.remove(tmp_path)
                    tmp_path = gz_path
                programs, channels = parse_xmltv(tmp_path, on_progress=on_progress,
                                                 with_channels=True)
                if not programs:
                    # An empty parse (broken/foreign payload) must never wipe
                    # a previously good guide — save_epg replaces per-url rows.
                    raise ValueError("XMLTV parse produced no programmes")
                self.cache.save_epg(url, programs, channels)
                self.channels_version += 1
                ok = True
                count = len(programs)
            except Exception as exc:
                logger.warning("EPG update failed for %s: %s", url, exc)
            finally:
                if tmp_path and os.path.isfile(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                if on_done:
                    on_done(ok, count)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    def now_next(self, tvg_id: str) -> dict:
        if not tvg_id:
            return {"now": "", "next": ""}
        return self.cache.epg_now_next(tvg_id)
