"""Queue-backed libtorrent-rasterbar wrapper.

TorrentEngine runs libtorrent in a dedicated thread and exposes methods via
a command queue so the caller never blocks on session I/O.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Tuple

import warnings

import libtorrent as lt

logger = logging.getLogger(__name__)


class EngineCommandError(Exception):
    """Raised when an engine command fails."""


def _hash_str(obj: Any) -> str:
    """Hex info-hash from a torrent_handle/torrent_info, preferring the
    libtorrent 2.x ``info_hashes()`` API over the deprecated ``info_hash()``."""
    try:
        return str(obj.info_hashes().v1)
    except AttributeError:
        return str(obj.info_hash())


class Priority(IntEnum):
    OFF = 0
    LOW = 1
    DEFAULT = 4
    HIGH = 7


@dataclass
class TorrentRecord:
    """Internal metadata for a managed torrent."""

    handle: lt.torrent_handle
    info_hash: str
    category: str = "Other"
    added_at: float = field(default_factory=time.time)
    file_progress: List[float] = field(default_factory=list)
    file_priorities: List[int] = field(default_factory=list)
    magnet_uri: str = ""
    torrent_file_path: str = ""


class TorrentEngine:
    """Wraps a libtorrent session in a queue-based command thread."""

    def __init__(
        self,
        listen_interfaces: Optional[List[str]] = None,
        dht_enabled: bool = True,
        lsd_enabled: bool = True,
        upnp_enabled: bool = True,
        natpmp_enabled: bool = True,
        download_limit_kb: int = 0,
        upload_limit_kb: int = 0,
        listen_port: int = 0,
        max_connections: int = 0,
        max_downloading_torrents: int = 5,
        max_seeding_torrents: int = 5,
        max_active_torrents: int = 10,
        max_queued_torrents: int = 0,
        auto_manage_interval_seconds: int = 30,
        seed_ratio_limit: float = 0.0,
        seed_time_limit_minutes: int = 0,
    ) -> None:
        self._settings = {
            "listen_interfaces": listen_interfaces or [f"0.0.0.0:{listen_port}"],
            "enable_dht": dht_enabled,
            "enable_lsd": lsd_enabled,
            "enable_upnp": upnp_enabled,
            "enable_natpmp": natpmp_enabled,
            "user_agent": "Deeptorrent/0.1.0",
            # libtorrent rate limits are bytes/s; 0 = unlimited.
            "download_rate_limit": max(0, download_limit_kb) * 1024,
            "upload_rate_limit": max(0, upload_limit_kb) * 1024,
            # Queue/concurrency settings (0 = libtorrent default/unlimited).
            "active_downloads": max(0, max_downloading_torrents) if max_downloading_torrents > 0 else 0,
            "active_seeds": max(0, max_seeding_torrents) if max_seeding_torrents > 0 else 0,
            "active_limit": max(0, max_active_torrents) if max_active_torrents > 0 else 0,
            "active_loaded_limit": max(0, max_queued_torrents) if max_queued_torrents > 0 else 0,
            "auto_manage_interval": max(1, auto_manage_interval_seconds),
            # libtorrent expects an integer in hundredths (200 = 2.0); 0 = disabled.
            "share_ratio_limit": int(max(0.0, float(seed_ratio_limit)) * 100) if seed_ratio_limit > 0 else 0,
            "seed_time_limit": max(0, seed_time_limit_minutes) * 60 if seed_time_limit_minutes > 0 else 0,
        }
        if max_connections > 0:
            self._settings["connections_limit"] = max_connections
        self._session: Optional[lt.session] = None
        self._torrents: Dict[str, TorrentRecord] = {}
        self._queue: queue.Queue[Dict[str, Any]] = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    def start(self) -> None:
        """Start the engine worker thread and libtorrent session."""
        if self._worker and self._worker.is_alive():
            raise EngineCommandError("Engine already running")

        self._worker = threading.Thread(target=self._run, name="torrent-engine", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Stop the engine worker and libtorrent session gracefully."""
        self._stop_event.set()
        try:
            self._enqueue(lambda: None)
        except Exception:
            pass
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout)

    def _run(self) -> None:
        """Main worker loop: create session and process commands."""
        try:
            settings = lt.default_settings()
            settings["listen_interfaces"] = ",".join(self._settings["listen_interfaces"])
            settings["enable_dht"] = self._settings["enable_dht"]
            settings["enable_lsd"] = self._settings["enable_lsd"]
            settings["enable_upnp"] = self._settings["enable_upnp"]
            settings["enable_natpmp"] = self._settings["enable_natpmp"]
            settings["user_agent"] = self._settings["user_agent"]
            settings["download_rate_limit"] = self._settings["download_rate_limit"]
            settings["upload_rate_limit"] = self._settings["upload_rate_limit"]
            if "connections_limit" in self._settings:
                settings["connections_limit"] = self._settings["connections_limit"]
            # Queue / concurrency settings.
            for key in ("active_downloads", "active_seeds", "active_limit",
                        "active_loaded_limit", "auto_manage_interval",
                        "share_ratio_limit", "seed_time_limit"):
                if self._settings.get(key):
                    settings[key] = self._settings[key]
            settings["alert_mask"] = int(lt.alert.category_t.all_categories)

            self._session = lt.session(settings)
            logger.info("libtorrent session created")

            last_drain = 0.0
            while not self._stop_event.is_set():
                try:
                    item = self._queue.get(timeout=0.2)
                    self._process_command(item)
                except queue.Empty:
                    pass
                # Drain the alert queue periodically (even under heavy command
                # traffic) so alerts don't accumulate for the whole session
                # lifetime (unbounded memory growth).
                now = time.monotonic()
                if now - last_drain >= 0.2:
                    self._drain_alerts()
                    last_drain = now
        except Exception as exc:
            logger.exception("Engine worker crashed: %s", exc)

    def _process_command(self, item: Dict[str, Any]) -> None:
        """Execute a queued command and resolve its Future."""
        fn = item["fn"]
        future: Future[Any] = item["future"]
        try:
            result = fn()
            future.set_result(result)
        except Exception as exc:
            logger.exception("Command failed")
            future.set_exception(EngineCommandError(str(exc)))

    def _enqueue(self, fn: Callable[[], Any], block: bool = True, timeout: Optional[float] = None) -> Future[Any]:
        """Submit a callable to the engine thread and return a Future."""
        future: Future[Any] = Future()
        self._queue.put({"fn": fn, "future": future}, block=block, timeout=timeout)
        return future

    def _wait(self, future: Future[Any], timeout: Optional[float] = None) -> Any:
        """Wait for a future and return its result, converting exceptions."""
        try:
            return future.result(timeout=timeout)
        except Exception as exc:
            if isinstance(exc, EngineCommandError):
                raise
            raise EngineCommandError(str(exc)) from exc

    def _drain_alerts(self) -> None:
        """Pop and discard pending alerts; log torrent errors."""
        if self._session is None:
            return
        try:
            for a in self._session.pop_alerts():
                what = a.what() if hasattr(a, "what") else type(a).__name__
                if "error" in what or "failed" in what:
                    # Skip bogus "failed" alerts carrying a success error code
                    # (e.g. torrent_delete_failed when no data existed on disk).
                    ec = getattr(a, "error", None)
                    if ec is not None and hasattr(ec, "value") and ec.value() == 0:
                        continue
                    logger.warning("libtorrent alert: %s", a.message() if hasattr(a, "message") else a)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_magnet(self, uri: str, save_path: str, category: str = "Other",
                   paused: bool = False, resume: Optional[bytes] = None) -> str:
        """Add a magnet URI and return the info-hash hex string."""
        future = self._enqueue(lambda: self._add_magnet(uri, save_path, category, paused, resume))
        return self._wait(future)

    def add_torrent_file(self, path: str, save_path: str, category: str = "Other",
                         paused: bool = False, resume: Optional[bytes] = None) -> str:
        """Add a .torrent file and return the info-hash hex string."""
        future = self._enqueue(lambda: self._add_torrent_file(path, save_path, category, paused, resume))
        return self._wait(future)

    def set_rate_limits(self, download_kb: int, upload_kb: int) -> None:
        """Apply session rate limits (KB/s, 0 = unlimited) at runtime."""
        future = self._enqueue(lambda: self._set_rate_limits(download_kb, upload_kb))
        return self._wait(future)

    def set_queue_settings(self, **kwargs: Any) -> None:
        """Apply queue/concurrency settings at runtime."""
        future = self._enqueue(lambda: self._set_queue_settings(kwargs))
        return self._wait(future)

    def export_resume_data(self, timeout: float = 15.0) -> Dict[str, bytes]:
        """Snapshot per-torrent resume data for fast restart (no re-hash)."""
        future = self._enqueue(lambda: self._export_resume_data(timeout))
        return self._wait(future, timeout=timeout + 5)

    def pause(self, info_hash: str) -> bool:
        """Pause a torrent by info-hash."""
        future = self._enqueue(lambda: self._pause(info_hash))
        return self._wait(future)

    def resume(self, info_hash: str) -> bool:
        """Resume a torrent by info-hash."""
        future = self._enqueue(lambda: self._resume(info_hash))
        return self._wait(future)

    def remove(self, info_hash: str, delete_files: bool = False) -> bool:
        """Remove a torrent, optionally deleting downloaded files."""
        future = self._enqueue(lambda: self._remove(info_hash, delete_files))
        return self._wait(future)

    def list_torrents(self, filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return a list of managed torrents, optionally filtered by state."""
        future = self._enqueue(lambda: self._list_torrents(filter))
        return self._wait(future)

    def get_torrent_status(self, info_hash: str) -> Dict[str, Any]:
        """Return detailed status for a single torrent."""
        future = self._enqueue(lambda: self._get_torrent_status(info_hash))
        return self._wait(future)

    def set_file_priority(self, info_hash: str, file_id: int, level: int) -> bool:
        """Set a file's priority (0=off, 1=low, 4=normal, 7=high)."""
        future = self._enqueue(lambda: self._set_file_priority(info_hash, file_id, level))
        return self._wait(future)

    def organize_torrent(
        self,
        info_hash: str,
        destination: str,
        category: str,
        file_renames: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        future = self._enqueue(
            lambda: self._organize_torrent(info_hash, destination, category, file_renames or []))
        return self._wait(future)

    def set_sequential_download(self, info_hash: str, on: bool = True) -> bool:
        """Toggle sequential piece download (needed to stream while downloading)."""
        future = self._enqueue(lambda: self._set_sequential_download(info_hash, on))
        return self._wait(future)

    def get_file_prefix(self, info_hash: str, file_index: int) -> Tuple[int, int]:
        """(verified contiguous prefix bytes, file size) for one file.

        file_progress() counts verified pieces anywhere in the file; for
        streaming we need the *gapless* prefix — the only region a player
        may safely read (libtorrent writes unverified blocks to disk, and
        pre-allocation fills the rest with zeros — both decode as garbage).
        """
        future = self._enqueue(lambda: self._file_prefix(info_hash, file_index))
        return self._wait(future)

    def set_stream_window(self, info_hash: str, first_byte: int, last_byte: int,
                          deadline_ms: int = 2000) -> bool:
        """Top-priority + deadline for pieces covering [first_byte, last_byte).

        Keeps the download frontier ahead of the player's read position so
        playback never outruns verified data."""
        future = self._enqueue(
            lambda: self._set_stream_window(info_hash, first_byte, last_byte, deadline_ms))
        return self._wait(future)

    def force_recheck(self, info_hash: str) -> bool:
        """Force a full hash re-check of the torrent's files on disk."""
        future = self._enqueue(lambda: self._force_recheck(info_hash))
        return self._wait(future)

    def force_reannounce(self, info_hash: str) -> bool:
        """Force an immediate tracker reannounce (helps stalled swarms)."""
        future = self._enqueue(lambda: self._force_reannounce(info_hash))
        return self._wait(future)

    def add_tracker(self, info_hash: str, url: str) -> bool:
        """Add a tracker to a torrent."""
        future = self._enqueue(lambda: self._add_tracker(info_hash, url))
        return self._wait(future)

    def get_swarm_stats(self, info_hash: str) -> Dict[str, Any]:
        """Return swarm health diagnostics."""
        future = self._enqueue(lambda: self._get_swarm_stats(info_hash))
        return self._wait(future)

    def get_session_stats(self) -> Dict[str, Any]:
        """Return aggregate session status (counts by state, totals, dht)."""
        future = self._enqueue(lambda: self._get_session_stats())
        return self._wait(future)

    # ------------------------------------------------------------------
    # Internal implementations (always run in engine thread)
    # ------------------------------------------------------------------

    def _add_magnet(self, uri: str, save_path: str, category: str,
                    paused: bool = False, resume: Optional[bytes] = None) -> str:
        params = self._resume_params(resume, save_path, paused)
        if params is None:
            params = lt.parse_magnet_uri(uri)
            params.save_path = save_path
            if paused:
                params.flags |= lt.torrent_flags.paused
                params.flags &= ~lt.torrent_flags.auto_managed
        # Reject duplicates BEFORE touching the session, so a failed add
        # never leaves an untracked torrent behind in libtorrent.
        try:
            info_hash = str(params.info_hashes.v1)
        except AttributeError:
            info_hash = str(params.info_hash) if hasattr(params, "info_hash") else ""
        with self._lock:
            if info_hash and info_hash in self._torrents:
                raise EngineCommandError("This torrent is already in the list")
        if not os.path.isdir(save_path):
            os.makedirs(save_path, exist_ok=True)
        handle = self._session.add_torrent(params)
        info_hash = _hash_str(handle)
        with self._lock:
            if info_hash in self._torrents:
                raise EngineCommandError("This torrent is already in the list")
        return self._register_handle(handle, category, magnet_uri=uri)

    def _add_torrent_file(self, path: str, save_path: str, category: str,
                          paused: bool = False, resume: Optional[bytes] = None) -> str:
        with open(path, "rb") as f:
            data = f.read()
        info = lt.torrent_info(data)
        # Reject duplicates BEFORE touching the session, so a failed add
        # never leaves an untracked torrent behind in libtorrent.
        info_hash = _hash_str(info)
        with self._lock:
            if info_hash in self._torrents:
                raise EngineCommandError("This torrent is already in the list")
        params = self._resume_params(resume, save_path, paused)
        if params is None:
            params = lt.add_torrent_params()
            params.save_path = save_path
            if paused:
                params.flags |= lt.torrent_flags.paused
                params.flags &= ~lt.torrent_flags.auto_managed
        # Always attach the metadata: resume blobs do not contain the info
        # dict, so without this a restored torrent sits in
        # "downloading_metadata" forever (fatal on private trackers).
        params.ti = info
        if not os.path.isdir(save_path):
            os.makedirs(save_path, exist_ok=True)
        handle = self._session.add_torrent(params)
        return self._register_handle(handle, category, torrent_file_path=path)

    @staticmethod
    def _resume_params(resume: Optional[bytes], save_path: str, paused: bool):
        """Build add_torrent_params from saved resume data, or None to fall back."""
        if not resume:
            return None
        try:
            params = lt.read_resume_data(resume)
            params.save_path = save_path
            if paused:
                params.flags |= lt.torrent_flags.paused
                params.flags &= ~lt.torrent_flags.auto_managed
            return params
        except Exception as exc:
            logger.warning("Invalid resume data, falling back to normal add: %s", exc)
            return None

    def _set_rate_limits(self, download_kb: int, upload_kb: int) -> None:
        self._session.apply_settings({
            "download_rate_limit": max(0, download_kb) * 1024,
            "upload_rate_limit": max(0, upload_kb) * 1024,
        })
        self._settings["download_rate_limit"] = max(0, download_kb) * 1024
        self._settings["upload_rate_limit"] = max(0, upload_kb) * 1024

    def _set_queue_settings(self, kwargs: Dict[str, Any]) -> None:
        """Apply queue/concurrency settings. 0 values are treated as "leave as configured"."""
        apply: Dict[str, Any] = {}
        mapping = {
            "max_downloading_torrents": "active_downloads",
            "max_seeding_torrents": "active_seeds",
            "max_active_torrents": "active_limit",
            "max_queued_torrents": "active_loaded_limit",
            "auto_manage_interval_seconds": "auto_manage_interval",
            "seed_ratio_limit": "share_ratio_limit",
            "seed_time_limit_minutes": "seed_time_limit",
        }
        for user_key, lt_key in mapping.items():
            value = kwargs.get(user_key)
            if value is None:
                continue
            if lt_key == "auto_manage_interval":
                value = max(1, int(value))
            elif lt_key == "seed_time_limit":
                value = max(0, int(value)) * 60
            elif lt_key == "share_ratio_limit":
                value = int(max(0.0, float(value)) * 100)
            else:
                value = max(0, int(value))
            apply[lt_key] = value
            self._settings[lt_key] = value
        if apply:
            self._session.apply_settings(apply)

    def _export_resume_data(self, timeout: float) -> Dict[str, bytes]:
        """Collect save_resume_data alerts for all managed torrents.

        Must run on the engine thread (it is enqueued like any command)."""
        with self._lock:
            handles = [r.handle for r in self._torrents.values()]
        outstanding = set()
        for h in handles:
            if h.is_valid():
                try:
                    h.save_resume_data()
                    outstanding.add(_hash_str(h))
                except Exception:
                    pass
        out: Dict[str, bytes] = {}
        deadline = time.time() + timeout
        while outstanding and time.time() < deadline:
            alert = self._session.wait_for_alert(500)
            if alert is None:
                continue
            for a in self._session.pop_alerts():
                try:
                    if isinstance(a, lt.save_resume_data_alert):
                        ih = _hash_str(a.handle)
                        out[ih] = lt.write_resume_data_buf(a.params)
                        outstanding.discard(ih)
                    elif isinstance(a, lt.save_resume_data_failed_alert):
                        outstanding.discard(_hash_str(a.handle))
                except Exception:
                    pass
        logger.info("Exported resume data for %d/%d torrents", len(out), len(handles))
        return out

    def _register_handle(self, handle: lt.torrent_handle, category: str, magnet_uri: str = "", torrent_file_path: str = "") -> str:
        info_hash = _hash_str(handle)
        record = TorrentRecord(handle=handle, info_hash=info_hash, category=category, magnet_uri=magnet_uri, torrent_file_path=torrent_file_path)
        with self._lock:
            self._torrents[info_hash] = record
        logger.info("Registered torrent %s in category %s", info_hash, category)
        return info_hash

    def _get_record(self, info_hash: str) -> TorrentRecord:
        with self._lock:
            record = self._torrents.get(info_hash)
        if record is None:
            raise EngineCommandError(f"Torrent {info_hash} not found")
        return record

    def _pause(self, info_hash: str) -> bool:
        record = self._get_record(info_hash)
        # An auto-managed torrent is resumed again by the session's
        # auto-manager, so pause() alone doesn't stick.
        record.handle.unset_flags(lt.torrent_flags.auto_managed)
        record.handle.pause()
        return True

    def _resume(self, info_hash: str) -> bool:
        record = self._get_record(info_hash)
        record.handle.set_flags(lt.torrent_flags.auto_managed)
        record.handle.resume()
        return True

    def _remove(self, info_hash: str, delete_files: bool) -> bool:
        record = self._get_record(info_hash)
        option = lt.options_t.delete_files if delete_files else 0
        self._session.remove_torrent(record.handle, option)
        with self._lock:
            del self._torrents[info_hash]
        logger.info("Removed torrent %s delete_files=%s", info_hash, delete_files)
        return True

    def _list_torrents(self, filter: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            records = list(self._torrents.values())
        results = []
        for record in records:
            status = self._status_dict(record)
            if filter and status.get("state") != filter:
                continue
            results.append(status)
        return results

    def _get_torrent_status(self, info_hash: str) -> Dict[str, Any]:
        record = self._get_record(info_hash)
        status = self._status_dict(record)
        status["files"] = self._file_list(record)
        return status

    def _set_file_priority(self, info_hash: str, file_id: int, level: int) -> bool:
        record = self._get_record(info_hash)
        ti = record.handle.torrent_file()
        if ti is None:
            raise EngineCommandError("Torrent metadata not yet available")
        if not (0 <= file_id < ti.files().num_files()):
            raise EngineCommandError(f"Invalid file_id {file_id}")
        if not (0 <= level <= 7):
            raise EngineCommandError(f"Invalid priority level {level}")
        record.handle.file_priority(file_id, level)
        return True

    def _organize_torrent(
        self,
        info_hash: str,
        destination: str,
        category: str,
        file_renames: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        record = self._get_record(info_hash)
        status = record.handle.status()
        if status.progress < 1.0:
            raise EngineCommandError("Torrent must be complete before it can be organized")
        ti = record.handle.torrent_file()
        if ti is None:
            raise EngineCommandError("Torrent metadata not yet available")
        destination = str(destination or "").strip()
        if not destination:
            raise EngineCommandError("Destination is required")
        destination = os.path.abspath(destination)
        renamed = []
        seen_ids = set()
        for item in file_renames:
            file_id = int(item["file_id"])
            new_path = str(item["new_path"]).strip().replace("\\", "/")
            parts = [part for part in new_path.split("/") if part]
            if (
                file_id in seen_ids or not 0 <= file_id < ti.files().num_files()
                or not new_path or new_path.startswith("/") or os.path.splitdrive(new_path)[0]
                or any(part in (".", "..") for part in parts)
            ):
                raise EngineCommandError(f"Invalid file rename: {item}")
            seen_ids.add(file_id)
            record.handle.rename_file(file_id, new_path)
            renamed.append({"file_id": file_id, "new_path": new_path})
        os.makedirs(destination, exist_ok=True)
        record.handle.move_storage(destination)
        record.category = category
        return {
            "success": True,
            "info_hash": info_hash,
            "category": category,
            "destination": destination,
            "file_renames": renamed,
            "note": "Storage move requested through libtorrent; completion continues asynchronously.",
        }

    def _set_sequential_download(self, info_hash: str, on: bool) -> bool:
        record = self._get_record(info_hash)
        if on:
            record.handle.set_flags(lt.torrent_flags.sequential_download)
        else:
            record.handle.unset_flags(lt.torrent_flags.sequential_download)
        return True

    def _file_prefix(self, info_hash: str, file_index: int) -> Tuple[int, int]:
        record = self._get_record(info_hash)
        ti = record.handle.torrent_file()
        if ti is None:
            return (0, 0)
        files = ti.files()
        if file_index < 0 or file_index >= files.num_files():
            return (0, 0)
        size = files.file_size(file_index)
        if size <= 0:
            return (0, size)
        piece_len = ti.piece_length()
        offset = files.file_offset(file_index)
        first = offset // piece_len
        last = (offset + size - 1) // piece_len
        p = first
        while p <= last and record.handle.have_piece(p):
            p += 1
        prefix = min(p * piece_len, offset + size) - offset
        return (max(0, prefix), size)

    def _set_stream_window(self, info_hash: str, first_byte: int, last_byte: int,
                           deadline_ms: int) -> bool:
        record = self._get_record(info_hash)
        ti = record.handle.torrent_file()
        if ti is None:
            return False
        piece_len = ti.piece_length()
        first = max(0, first_byte // piece_len)
        last = min(ti.num_pieces() - 1, max(0, last_byte - 1) // piece_len)
        for p in range(first, last + 1):
            if not record.handle.have_piece(p):
                record.handle.piece_priority(p, 7)
                record.handle.set_piece_deadline(p, deadline_ms)
        return True

    def _add_tracker(self, info_hash: str, url: str) -> bool:
        record = self._get_record(info_hash)
        record.handle.add_tracker({"url": url, "tier": 0})
        return True

    def _force_recheck(self, info_hash: str) -> bool:
        record = self._get_record(info_hash)
        record.handle.force_recheck()
        return True

    def _force_reannounce(self, info_hash: str) -> bool:
        record = self._get_record(info_hash)
        record.handle.force_reannounce()
        return True

    def _get_session_stats(self) -> Dict[str, Any]:
        with self._lock:
            records = list(self._torrents.values())
        counts: Dict[str, int] = {}
        total_down = 0
        total_up = 0
        for record in records:
            status = self._status_dict(record)
            state = status["state"]
            counts[state] = counts.get(state, 0) + 1
            total_down += status.get("download_rate", 0)
            total_up += status.get("upload_rate", 0)
        dht_nodes = 0
        try:
            dht_nodes = self._session.status().dht_nodes
        except Exception:
            pass
        return {
            "total": len(records),
            "counts": counts,
            "download_rate": total_down,
            "upload_rate": total_up,
            "dht_nodes": dht_nodes,
            "downloading": counts.get("downloading", 0),
            "seeding": counts.get("seeding", 0),
            "finished": counts.get("finished", 0),
            "queued_for_checking": counts.get("queued_for_checking", 0),
            "checking_files": counts.get("checking_files", 0),
            "downloading_metadata": counts.get("downloading_metadata", 0),
        }

    def _get_swarm_stats(self, info_hash: str) -> Dict[str, Any]:
        record = self._get_record(info_hash)
        handle = record.handle
        status = handle.status()

        peers: List[Dict[str, Any]] = []
        try:
            peer_info = handle.get_peer_info()
            for p in peer_info:
                peers.append(
                    {
                        "ip": f"{p.ip[0]}:{p.ip[1]}",
                        "client": p.client,
                        "flags": int(p.flags),
                        "source": int(p.source),
                        "down_speed": p.down_speed,
                        "up_speed": p.up_speed,
                        "progress": p.progress,
                        "connection_type": int(p.connection_type),
                    }
                )
        except Exception:
            pass

        trackers: List[Dict[str, Any]] = []
        try:
            for t in handle.trackers():
                trackers.append(
                    {
                        "url": t.url,
                        "tier": t.tier,
                        "verified": t.verified,
                        "message": getattr(t, "message", ""),
                        "last_error": t.last_error.value() if t.last_error else None,
                        "fails": t.fails,
                        "fail_limit": t.fail_limit,
                        "source": int(t.source) if hasattr(t, "source") else 0,
                    }
                )
        except Exception:
            pass

        availability = []
        try:
            availability = handle.piece_availability()
        except Exception:
            pass

        dht_nodes = 0
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                dht_nodes = self._session.status().dht_nodes
        except Exception:
            pass

        return {
            "info_hash": info_hash,
            "name": status.name,
            "state": self._state_name(status.state),
            "progress": status.progress,
            "download_rate": status.download_rate,
            "upload_rate": status.upload_rate,
            "num_seeds": status.num_seeds,
            "num_peers": status.num_peers,
            "num_complete": status.num_complete,
            "num_incomplete": status.num_incomplete,
            "list_peers": status.list_peers,
            "list_seeds": status.list_seeds,
            "connect_candidates": status.connect_candidates,
            "dht_nodes": dht_nodes,
            "trackers": trackers,
            "peers": peers,
            "piece_availability_histogram": self._availability_histogram(availability),
            "error": status.errc.message() if status.errc else None,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _status_dict(self, record: TorrentRecord) -> Dict[str, Any]:
        status = record.handle.status()
        ti = record.handle.torrent_file()
        total_size = status.total_wanted if status.total_wanted else 0
        if not total_size and ti:
            total_size = ti.total_size()

        eta: Optional[int] = None
        remaining = status.total_wanted - status.total_wanted_done
        if status.download_rate > 0 and remaining > 0:
            eta = int(remaining / status.download_rate)

        # libtorrent sets queue_position to -1 for torrents that are not queued.
        queue_position = int(getattr(status, "queue_position", -1))
        is_queued = queue_position >= 0 and status.state in (0, 3)  # queued_for_checking or downloading
        all_time_download = getattr(status, "all_time_download", 0)
        all_time_upload = getattr(status, "all_time_upload", 0)
        ratio = round(all_time_upload / all_time_download, 4) if all_time_download > 0 else 0.0
        return {
            "eta": eta,
            "info_hash": record.info_hash,
            "name": status.name,
            "category": record.category,
            "save_path": status.save_path if status.save_path else "",
            "state": self._state_name(status.state),
            "progress": round(status.progress, 4),
            "download_rate": status.download_rate,
            "upload_rate": status.upload_rate,
            "num_seeds": status.num_seeds,
            "num_peers": status.num_peers,
            "total_done": status.total_done,
            "total_size": total_size,
            "added_at": record.added_at,
            # Auto-managed torrents are internally flagged paused while queued
            # or checking; only report "paused" when the user actually paused it
            # (paused AND not auto-managed).
            "paused": bool(status.flags & lt.torrent_flags.paused)
            and not bool(status.flags & lt.torrent_flags.auto_managed),
            "auto_managed": bool(status.flags & lt.torrent_flags.auto_managed),
            "queue_position": queue_position,
            "is_queued": is_queued,
            "ratio": ratio,
            "all_time_download": all_time_download,
            "all_time_upload": all_time_upload,
            "seeding_time": getattr(status, "seeding_time", 0),
            "finished_time": getattr(status, "finished_time", 0),
            "magnet_uri": record.magnet_uri,
            "torrent_file": record.torrent_file_path,
        }

    def _file_list(self, record: TorrentRecord) -> List[Dict[str, Any]]:
        ti = record.handle.torrent_file()
        if ti is None:
            return []
        files = ti.files()
        priorities = record.handle.get_file_priorities() if record.handle.is_valid() else []
        try:
            progress = record.handle.file_progress() if record.handle.is_valid() else []
        except Exception:
            progress = []
        result = []
        for i in range(files.num_files()):
            path = files.file_path(i)
            size = files.file_size(i)
            priority = priorities[i] if i < len(priorities) else 4
            result.append(
                {
                    "file_id": i,
                    "path": path,
                    "size": size,
                    "downloaded": progress[i] if i < len(progress) else 0,
                    "priority": priority,
                    "priority_name": self._priority_name(priority),
                }
            )
        return result

    def _availability_histogram(self, availability: List[int]) -> Dict[str, Any]:
        if not availability:
            return {"bins": [], "max": 0, "mean": 0.0, "zeros": 0}
        bins = [0] * 10
        for a in availability:
            idx = min(a, 9)
            bins[idx] += 1
        zeros = availability.count(0)
        mean = sum(availability) / len(availability)
        return {
            "bins": bins,
            "max": max(availability),
            "mean": round(mean, 2),
            "zeros": zeros,
            "total_pieces": len(availability),
        }

    @staticmethod
    def _state_name(state: int) -> str:
        names = {
            0: "queued_for_checking",
            1: "checking_files",
            2: "downloading_metadata",
            3: "downloading",
            4: "finished",
            5: "seeding",
            6: "allocating",
            7: "checking_resume_data",
        }
        return names.get(state, "unknown")

    @staticmethod
    def _priority_name(level: int) -> str:
        if level == 0:
            return "off"
        if level <= 2:
            return "low"
        if level <= 5:
            return "normal"
        return "high"

    def __enter__(self) -> "TorrentEngine":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
