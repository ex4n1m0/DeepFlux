"""Download job model and state machine for the segmented download engine."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class JobStatus(Enum):
    """Lifecycle states for a download job."""
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"  # segments done — remuxing/merging
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


class SegmentStatus(Enum):
    """Lifecycle states for an individual segment worker."""
    PENDING = "pending"
    ACTIVE = "active"
    STALLED = "stalled"
    DONE = "done"
    ERROR = "error"


@dataclass
class SegmentState:
    """Tracks the download progress of a single byte-range segment.

    The engine writes directly into the pre-allocated destination file at
    ``start_byte + completed_bytes``, so no merge step is needed."""
    index: int
    start_byte: int
    end_byte: int
    completed_bytes: int = 0
    status: SegmentStatus = SegmentStatus.PENDING
    speed_bps: int = 0
    error: str = ""
    retries: int = 0  # transient-error retry attempts so far

    @property
    def total_bytes(self) -> int:
        return self.end_byte - self.start_byte + 1

    @property
    def remaining_bytes(self) -> int:
        return self.total_bytes - self.completed_bytes

    @property
    def progress(self) -> float:
        if self.total_bytes <= 0:
            return 1.0
        return self.completed_bytes / self.total_bytes

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "completed_bytes": self.completed_bytes,
            "status": self.status.value,
            "speed_bps": self.speed_bps,
            "error": self.error,
            "retries": self.retries,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SegmentState":
        return cls(
            index=data["index"],
            start_byte=data["start_byte"],
            end_byte=data["end_byte"],
            completed_bytes=data.get("completed_bytes", 0),
            status=SegmentStatus(data.get("status", "pending")),
            speed_bps=0,
            error=data.get("error", ""),
            retries=data.get("retries", 0),
        )


@dataclass
class DownloadJob:
    """A single download job managed by the engine.

    For segmented downloads, the file is pre-allocated and each segment
    writes directly to its byte offset. For non-resumable servers, a
    single segment covering the entire file is used with sequential
    resume-from-last-byte support."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    url: str = ""
    filename: str = ""
    save_path: str = ""
    file_size: int = 0
    downloaded: int = 0
    status: JobStatus = JobStatus.QUEUED
    speed_bps: int = 0
    eta_seconds: int = 0
    source_url: str = ""
    job_type: str = "file"  # "file" | "hls" | "dash"
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: str = ""
    referrer: str = ""
    supports_ranges: bool = False
    etag: str = ""
    last_modified: str = ""
    segments: List[SegmentState] = field(default_factory=list)
    error_message: str = ""
    created_at: float = field(default_factory=time.time)
    priority: int = 0
    category: str = ""

    @property
    def progress(self) -> float:
        if self.file_size <= 0:
            return 0.0
        return min(1.0, self.downloaded / self.file_size)

    @property
    def is_active(self) -> bool:
        return self.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED, JobStatus.PROCESSING)

    @property
    def is_complete(self) -> bool:
        return self.status == JobStatus.COMPLETED

    @property
    def resume_file_path(self) -> str:
        """Path to the .dtresume state file alongside the download."""
        import os
        return os.path.splitext(self.save_path)[0] + ".dtresume"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "filename": self.filename,
            "save_path": self.save_path,
            "file_size": self.file_size,
            "downloaded": self.downloaded,
            "status": self.status.value,
            "speed_bps": self.speed_bps,
            "eta_seconds": self.eta_seconds,
            "source_url": self.source_url,
            "job_type": self.job_type,
            "supports_ranges": self.supports_ranges,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "segments": [s.to_dict() for s in self.segments],
            "error_message": self.error_message,
            "created_at": self.created_at,
            "priority": self.priority,
            "category": self.category,
            "progress": self.progress,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DownloadJob":
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            url=data.get("url", ""),
            filename=data.get("filename", ""),
            save_path=data.get("save_path", ""),
            file_size=data.get("file_size", 0),
            downloaded=data.get("downloaded", 0),
            status=JobStatus(data.get("status", "queued")),
            speed_bps=0,
            eta_seconds=0,
            source_url=data.get("source_url", ""),
            job_type=data.get("job_type", "file"),
            headers=data.get("headers", {}),
            cookies=data.get("cookies", ""),
            referrer=data.get("referrer", ""),
            supports_ranges=data.get("supports_ranges", False),
            etag=data.get("etag", ""),
            last_modified=data.get("last_modified", ""),
            segments=[SegmentState.from_dict(s) for s in data.get("segments", [])],
            error_message=data.get("error_message", ""),
            created_at=data.get("created_at", time.time()),
            priority=data.get("priority", 0),
            category=data.get("category", ""),
        )
