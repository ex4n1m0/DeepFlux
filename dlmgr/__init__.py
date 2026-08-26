"""IDM-style download manager package for DeepFlux."""
from __future__ import annotations

from .job import DownloadJob, JobStatus, SegmentState
from .engine import DownloadEngine
from .control_api import ControlAPI

__all__ = ["DownloadJob", "JobStatus", "SegmentState", "DownloadEngine", "ControlAPI"]
