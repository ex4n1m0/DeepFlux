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
from typing import Callable, List, Optional

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
) -> List[dict]:
    """Stream-parse an XMLTV file into a list of program dicts."""
    programs: List[dict] = []
    count = 0
    # iterparse lets us drop cleared elements to keep memory bounded.
    for _ev, elem in ET.iterparse(path, events=("end",)):
        if is_cancelled and is_cancelled():
            break
        if elem.tag != "programme":
            # Free elements we don't need to keep memory low — but NEVER
            # clear <title>/<desc>: their end events fire before the parent
            # <programme>'s, and clearing them would wipe the text the
            # programme extraction below is about to read.
            if elem.tag not in ("title", "desc"):
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
    if on_progress:
        on_progress(count, None)
    return programs


class EPGManager:
    """Downloads, parses, and caches an XMLTV guide for a source."""

    def __init__(self, cache: IPTVCache) -> None:
        self.cache = cache

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
                r = requests.get(url, headers=hdrs, timeout=60, stream=True)
                r.raise_for_status()
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
                programs = parse_xmltv(tmp_path, on_progress=on_progress)
                self.cache.save_epg(url, programs)
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
