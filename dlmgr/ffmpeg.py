"""FFmpeg wrapper — remuxes downloaded stream segments into a single file.

Uses stream-copy (no re-encode) by default for speed. Optionally transcodes.

FFmpeg is located via:
1. config.download.ffmpeg_path (explicit user setting)
2. Bundled ffmpeg.exe alongside the app executable (sys._MEIPASS or exe dir)
3. System PATH
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# Hide the console window FFmpeg would otherwise pop up on Windows when
# spawned from a windowed (PyInstaller --noconsole) app. 0 on other platforms.
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Container extensions FFmpeg can infer a muxer from.
KNOWN_CONTAINER_EXTS = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".m2ts",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav",
}


def find_ffmpeg(config_ffmpeg_path: str = "") -> str:
    """Locate the FFmpeg executable.

    Search order:
    1. Explicit path from config
    2. Bundled alongside the app (PyInstaller _MEIPASS or exe directory)
    3. System PATH
    """
    # 1. Explicit config path.
    if config_ffmpeg_path and os.path.isfile(config_ffmpeg_path):
        return config_ffmpeg_path

    # 2. Bundled alongside the app.
    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    search_dirs = []

    # PyInstaller onedir: exe is in the same directory as the app.
    if hasattr(sys, "_MEIPASS"):
        search_dirs.append(sys._MEIPASS)
        search_dirs.append(os.path.join(sys._MEIPASS, "ffmpeg"))

    # Also check the directory of the main executable and _internal subdir.
    if sys.executable:
        exe_dir = os.path.dirname(sys.executable)
        search_dirs.append(exe_dir)
        search_dirs.append(os.path.join(exe_dir, "ffmpeg"))
        # PyInstaller onedir: datas go to _internal/
        search_dirs.append(os.path.join(exe_dir, "_internal", "ffmpeg"))

    # Dev runs (python main.py): the repo keeps the shipped binary in
    # packaging/ffmpeg/ — match the frozen build's layout.
    search_dirs.append(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "packaging", "ffmpeg"))

    for d in search_dirs:
        candidate = os.path.join(d, exe_name)
        if os.path.isfile(candidate):
            logger.info("Found bundled FFmpeg: %s", candidate)
            return candidate

    # 3. System PATH.
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        logger.info("Found system FFmpeg: %s", found)
        return found

    logger.warning("FFmpeg not found")
    return ""


def find_ffprobe(ffmpeg_path: str = "") -> str:
    """Locate the ffprobe executable — it normally ships next to ffmpeg."""
    exe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    if ffmpeg_path:
        candidate = os.path.join(os.path.dirname(ffmpeg_path), exe_name)
        if os.path.isfile(candidate):
            return candidate
    import shutil
    return shutil.which("ffprobe") or ""


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def probe_duration(path: str, ffprobe_path: str = "", timeout: float = 10.0) -> float:
    """Return the media duration in seconds, or 0.0 if unreadable.

    Prefers ffprobe; falls back to parsing `ffmpeg -i` stderr so the bundled
    ffmpeg-only builds work too. Works on partial files when the container
    header carries the duration (MKV/WebM, faststart MP4); a truncated
    non-faststart MP4 just returns 0.0 and the caller can retry once more
    data has arrived."""
    if not os.path.isfile(path):
        return 0.0
    ffprobe = ffprobe_path or find_ffprobe(find_ffmpeg())
    if ffprobe:
        cmd = [ffprobe, "-v", "error",
               "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                    creationflags=_CREATION_FLAGS)
            return float(result.stdout.decode(errors="replace").strip() or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0.0
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return 0.0
    # `ffmpeg -i` exits nonzero ("no output specified") but prints the
    # Duration line to stderr first — that's all we need.
    cmd = [ffmpeg, "-hide_banner", "-i", path]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                creationflags=_CREATION_FLAGS)
        m = _DURATION_RE.search(result.stderr.decode(errors="replace"))
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except (OSError, subprocess.SubprocessError):
        pass
    return 0.0


class FFmpegWrapper:
    """Wraps FFmpeg subprocess calls for remuxing and transcoding."""

    def __init__(self, ffmpeg_path: str = "") -> None:
        self._ffmpeg_path = ffmpeg_path

    @property
    def available(self) -> bool:
        return bool(self._ffmpeg_path and os.path.isfile(self._ffmpeg_path))

    def remux(
        self,
        segment_files: List[str],
        output_path: str,
        init_segment: str = "",
        transcode: bool = False,
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> bool:
        """Remux segment files into a single output file.

        Uses stream-copy (no re-encode) by default for maximum speed.
        Set ``transcode=True`` to re-encode to H.264/AAC.

        For HLS (.ts segments): uses concat protocol.
        For DASH (fMP4 with init segment): uses concat demuxer.

        Args:
            segment_files: List of segment file paths in order.
            output_path: Output file path (e.g. output.mp4).
            init_segment: Path to init segment (for fMP4/DASH).
            transcode: If True, transcode instead of stream-copy.
            on_progress: Callback receiving 0.0–1.0 progress.

        Returns True on success, False on failure.
        """
        if not self.available:
            logger.error("FFmpeg not available — cannot remux")
            return False

        if not segment_files:
            logger.error("No segment files to remux")
            return False

        # Build a concat list file.
        list_path = output_path + ".concat.txt"
        try:
            with open(list_path, "w", encoding="utf-8") as f:
                if init_segment and os.path.isfile(init_segment):
                    # Escape single quotes in filenames for the concat format.
                    f.write(f"file '{self._escape_path(init_segment)}'\n")
                for seg in segment_files:
                    if os.path.isfile(seg):
                        f.write(f"file '{self._escape_path(seg)}'\n")
        except OSError as exc:
            logger.error("Failed to write concat list: %s", exc)
            return False

        # FFmpeg infers the container format from the output extension —
        # extensionless paths fail, so default to MP4. Note: titles contain
        # stray dots ("...good girl. Minami..."), so a naive splitext check
        # sees a bogus "extension" — only accept real container extensions.
        if os.path.splitext(output_path)[1].lower() not in KNOWN_CONTAINER_EXTS:
            output_path += ".mp4"

        # Build FFmpeg command.
        if transcode:
            cmd = [
                self._ffmpeg_path, "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_path,
                "-c:v", "libx264",
                "-c:a", "aac",
                "-preset", "fast",
                output_path,
            ]
        else:
            cmd = [
                self._ffmpeg_path, "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_path,
                "-c", "copy",
                # TS segments carry ADTS AAC; the MP4 container needs the
                # annex-b → avcc conversion (no-op for non-ADTS audio).
                "-bsf:a", "aac_adtstoasc",
                output_path,
            ]

        logger.info("FFmpeg remux: %s -> %s (%d segments, transcode=%s)",
                     segment_files[0], output_path, len(segment_files), transcode)

        try:
            proc = subprocess.Popen(
                cmd,
                stderr=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATION_FLAGS,
            )

            # Parse progress from stderr.
            duration = 0.0
            current_time = 0.0
            for line in proc.stderr:
                line = line.strip()
                # Parse Duration.
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", line)
                if m:
                    h, mi, s = m.groups()
                    duration = int(h) * 3600 + int(mi) * 60 + float(s)
                # Parse progress time.
                m = re.search(r"time=\s*(\d+):(\d+):(\d+\.\d+)", line)
                if m and duration > 0:
                    h, mi, s = m.groups()
                    current_time = int(h) * 3600 + int(mi) * 60 + float(s)
                    if on_progress:
                        on_progress(min(1.0, current_time / duration))

            proc.wait()
            if proc.returncode == 0:
                logger.info("FFmpeg remux complete: %s", output_path)
                if on_progress:
                    on_progress(1.0)
                return True
            else:
                logger.error("FFmpeg failed with return code %d", proc.returncode)
                return False

        except Exception as exc:
            logger.error("FFmpeg execution error: %s", exc)
            return False
        finally:
            # Clean up concat list.
            try:
                if os.path.exists(list_path):
                    os.remove(list_path)
            except OSError:
                pass

    def remux_ts_to_mp4(
        self,
        ts_file: str,
        output_path: str,
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> bool:
        """Remux a single .ts file to .mp4 via stream-copy."""
        if not self.available:
            return False
        cmd = [
            self._ffmpeg_path, "-y",
            "-i", ts_file,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            output_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300,
                                    creationflags=_CREATION_FLAGS)
            if result.returncode == 0:
                if on_progress:
                    on_progress(1.0)
                return True
            logger.error("FFmpeg ts->mp4 failed: %s", result.stderr.decode(errors="replace")[:500])
            return False
        except Exception as exc:
            logger.error("FFmpeg ts->mp4 error: %s", exc)
            return False

    @staticmethod
    def _escape_path(path: str) -> str:
        """Escape a file path for the FFmpeg concat format.

        Backslashes (Windows paths) are escape characters in the concat
        demuxer and must be doubled; single quotes get the standard
        shell-style wrapping."""
        return path.replace("\\", "\\\\").replace("'", r"'\''")
