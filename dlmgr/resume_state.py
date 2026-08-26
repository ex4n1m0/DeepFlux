"""Resume state persistence — saves and loads .dtresume files so interrupted
downloads can resume after app restart or network loss.

The .dtresume file is a JSON file stored alongside the download. It contains
the job metadata and per-segment byte-completion state, allowing the engine
to re-request only the missing byte ranges."""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from .job import DownloadJob

logger = logging.getLogger(__name__)


def save_resume_state(job: DownloadJob) -> None:
    """Atomically write the .dtresume file for a job.

    Writes to a .tmp file first, then renames to avoid corruption if the
    process is killed mid-write."""
    path = job.resume_file_path
    data = job.to_dict()
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        # On Windows, os.replace handles the atomic rename.
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("Failed to save resume state for job %s: %s", job.id, exc)
        # Clean up tmp file if it exists.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def load_resume_state(path: str) -> Optional[DownloadJob]:
    """Load a .dtresume file and return a DownloadJob, or None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return DownloadJob.from_dict(data)
    except Exception as exc:
        logger.warning("Failed to load resume state from %s: %s", path, exc)
        return None


def delete_resume_state(job: DownloadJob) -> None:
    """Remove the .dtresume file for a completed or cancelled job."""
    path = job.resume_file_path
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("Failed to delete resume state %s: %s", path, exc)


def scan_for_incomplete(folder: str) -> list:
    """Scan a folder for .dtresume files and return a list of incomplete jobs.

    Used at engine startup to re-queue downloads that were interrupted."""
    jobs = []
    if not os.path.isdir(folder):
        return jobs
    for entry in os.listdir(folder):
        if entry.endswith(".dtresume"):
            path = os.path.join(folder, entry)
            job = load_resume_state(path)
            if job is not None and not job.is_complete:
                # Reset to queued so the scheduler picks it up.
                from .job import JobStatus
                job.status = JobStatus.QUEUED
                job.speed_bps = 0
                job.eta_seconds = 0
                jobs.append(job)
                logger.info("Recovered incomplete download: %s -> %s", job.id, job.filename)
    return jobs
