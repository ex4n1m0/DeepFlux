"""Torrent state persistence — save and restore active torrents across restarts."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TorrentStateManager:
    """Saves and restores torrent state so downloads can resume after restart."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = Path(state_dir)
        self.state_file = self.state_dir / "torrents_state.json"
        self.torrents_dir = self.state_dir / "torrents"
        self.resume_dir = self.state_dir / "resume"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.torrents_dir.mkdir(parents=True, exist_ok=True)
        self.resume_dir.mkdir(parents=True, exist_ok=True)

    def save_state(self, torrents: List[Dict[str, Any]]) -> None:
        """Save the current list of torrents to disk.

        Each torrent entry should have: info_hash, name, save_path, category,
        progress, state, and optionally magnet_uri or torrent_file_path.
        Writes are atomic (temp + rename) and keep a .bak copy.
        """
        entries = []
        for t in torrents:
            entry = {
                "info_hash": t.get("info_hash", ""),
                "name": t.get("name", ""),
                "save_path": t.get("save_path", ""),
                "category": t.get("category", "Other"),
                "progress": t.get("progress", 0),
                "state": t.get("state", ""),
                "paused": t.get("paused", False),
                "magnet_uri": t.get("magnet_uri", ""),
                "torrent_file": t.get("torrent_file", ""),
                "saved_at": time.time(),
            }
            if entry["info_hash"]:
                entries.append(entry)

        try:
            self._write_json_atomic(self.state_file, entries)
            logger.info("Saved torrent state: %d torrent(s)", len(entries))
        except Exception as exc:
            logger.warning("Failed to save torrent state: %s", exc)

    @staticmethod
    def _write_json_atomic(path: Path, data: Any) -> None:
        """Write JSON atomically: temp file in same dir, then rename, then backup."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        bak = path.with_suffix(path.suffix + ".bak")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if path.is_file():
            try:
                shutil.copy2(path, bak)
            except Exception:
                pass
        tmp.replace(path)

    def load_state(self) -> List[Dict[str, Any]]:
        """Load saved torrent state from disk, falling back to .bak if needed."""
        bak = self.state_file.with_name(self.state_file.name + ".bak")
        paths = [self.state_file, bak]
        for p in paths:
            if not p.is_file():
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                logger.info("Loaded torrent state from %s: %d torrent(s)", p.name, len(entries))
                return entries
            except Exception as exc:
                logger.warning("Failed to load torrent state from %s: %s", p.name, exc)
        return []

    def store_torrent_file(self, source_path: str, info_hash: str) -> str:
        """Copy a .torrent file to the persistent torrents directory.

        Returns the path to the stored copy, or empty string on failure.
        """
        try:
            dest = self.torrents_dir / f"{info_hash}.torrent"
            if Path(source_path).resolve() != dest.resolve():
                shutil.copy2(source_path, dest)
            return str(dest)
        except Exception as exc:
            logger.warning("Failed to store torrent file: %s", exc)
            return ""

    def store_magnet_uri(self, info_hash: str, magnet_uri: str) -> str:
        """Persist a magnet URI to disk so a torrent can be restored after a crash."""
        try:
            dest = self.torrents_dir / f"{info_hash}.magnet"
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_text(magnet_uri, encoding="utf-8")
            tmp.replace(dest)
            return str(dest)
        except Exception as exc:
            logger.warning("Failed to store magnet URI: %s", exc)
            return ""

    def get_stored_magnet_uri(self, info_hash: str) -> Optional[str]:
        """Return a stored magnet URI, or None if not found."""
        dest = self.torrents_dir / f"{info_hash}.magnet"
        if dest.is_file():
            return dest.read_text(encoding="utf-8")
        return None

    def get_stored_torrent_path(self, info_hash: str) -> Optional[str]:
        """Return the path to a stored .torrent file, or None if not found."""
        dest = self.torrents_dir / f"{info_hash}.torrent"
        if dest.is_file():
            return str(dest)
        return None

    def clear_state(self) -> None:
        """Remove the state file (called after successful restore)."""
        try:
            if self.state_file.is_file():
                self.state_file.unlink()
        except Exception:
            pass

    def remove_torrent_file(self, info_hash: str) -> None:
        """Remove a stored .torrent file and magnet when the torrent is removed."""
        for suffix in (".torrent", ".magnet"):
            try:
                dest = self.torrents_dir / f"{info_hash}{suffix}"
                if dest.is_file():
                    dest.unlink()
            except Exception:
                pass

    def get_incomplete_torrents(self) -> List[Dict[str, Any]]:
        """Return only torrents that were not finished/seeding when saved."""
        entries = self.load_state()
        return [
            e for e in entries
            if e.get("state") not in ("finished", "seeding")
            and e.get("progress", 0) < 1.0
        ]

    # -- fast-resume data ----------------------------------------------------
    def save_resume_data(self, data: Dict[str, bytes]) -> None:
        """Persist per-torrent resume blobs (info_hash -> bencoded bytes).

        Files for hashes no longer present are removed. Writes are atomic."""
        try:
            for info_hash, blob in data.items():
                target = self.resume_dir / f"{info_hash}.resume"
                tmp = target.with_suffix(target.suffix + ".tmp")
                tmp.write_bytes(blob)
                tmp.replace(target)
            for f in self.resume_dir.glob("*.resume"):
                if f.stem not in data:
                    f.unlink()
        except Exception as exc:
            logger.warning("Failed to save resume data: %s", exc)

    def load_resume_data(self, info_hash: str) -> Optional[bytes]:
        """Return the saved resume blob for a torrent, or None."""
        try:
            p = self.resume_dir / f"{info_hash}.resume"
            if p.is_file():
                return p.read_bytes()
        except Exception:
            pass
        return None

    def remove_resume_data(self, info_hash: str) -> None:
        try:
            p = self.resume_dir / f"{info_hash}.resume"
            if p.is_file():
                p.unlink()
        except Exception:
            pass
