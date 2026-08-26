"""Local metadata assistant for renaming and categorizing completed torrents."""
from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class RenameProposal:
    info_hash: str
    current_path: str
    proposed_path: str
    category: str
    file_moves: List[Dict[str, str]]


class Organizer:
    """Suggests and applies clean filenames and category folders for completed torrents."""

    CATEGORIES = ["Movies", "TV", "Software", "Other"]

    def __init__(self, base_path: str) -> None:
        self.base_path = Path(base_path)

    def gather_metadata(self, status: Dict[str, Any]) -> Dict[str, Any]:
        """Collect file list and metadata for a completed torrent."""
        files = status.get("files", [])
        return {
            "name": status.get("name"),
            "info_hash": status.get("info_hash"),
            "current_category": status.get("category", "Other"),
            "save_path": status.get("save_path"),
            "files": [
                {
                    "file_id": f["file_id"],
                    "path": f["path"],
                    "size": f["size"],
                    "priority": f["priority"],
                }
                for f in files
            ],
            "total_size": sum(f["size"] for f in files),
        }

    def apply_proposal(self, proposal: RenameProposal) -> Dict[str, Any]:
        """Physically move files into the proposed category folder and rename top-level items."""
        try:
            dest_dir = self.base_path / proposal.category
            dest_dir.mkdir(parents=True, exist_ok=True)
            for move in proposal.file_moves:
                src = Path(move["src"])
                dst = dest_dir / Path(move["dst"]).name
                if src.exists() and not dst.exists():
                    shutil.move(str(src), str(dst))
            return {"success": True, "new_path": str(dest_dir), "moves": proposal.file_moves}
        except Exception as exc:
            logger.exception("Failed to apply proposal")
            return {"success": False, "error": str(exc)}

    @staticmethod
    def detect_category(name: str, files: List[Dict[str, Any]]) -> str:
        """Simple rule-based category detection to seed LLM proposals."""
        text = " ".join([name] + [f["path"] for f in files]).lower()
        video_extensions = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v"}
        is_video = any(text.endswith(ext) for ext in video_extensions) or any(
            any(f["path"].lower().endswith(ext) for ext in video_extensions) for f in files
        )
        if re.search(r"\b(s\d{1,2}e\d{1,2}|season|episode|tv[- ]?show)\b", text):
            return "TV"
        if is_video and re.search(r"\b(20\d{2}|19\d{2})\b", text):
            return "Movies"
        if re.search(r"\b(setup\.exe|\.msi|\.dmg|\.pkg|\.zip|\.tar\.gz|\.7z)\b", text):
            return "Software"
        return "Other"
