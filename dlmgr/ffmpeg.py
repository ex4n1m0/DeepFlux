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
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlsplit

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


_NETWORK_STREAM_SCHEMES = {
    "http", "https", "rtmp", "rtmps", "rtsp", "rtsps", "udp", "tcp", "srt",
}
_URL_IN_DIAGNOSTIC_RE = re.compile(
    r"(?:https?|rtmps?|rtsps?|udp|tcp|srt)://[^\s\]\[()]+", re.IGNORECASE)
_SECRET_HEADER_RE = re.compile(
    r"(?im)^(authorization|cookie|proxy-authorization):\s*.*$")
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def is_network_stream_url(url: str) -> bool:
    """True only for explicit network-media schemes; local/file paths fail closed."""
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    return parsed.scheme.lower() in _NETWORK_STREAM_SCHEMES and bool(parsed.netloc)


def sanitize_recording_name(name: str) -> str:
    value = _INVALID_FILENAME_RE.sub("_", str(name or "")).strip(" ._")
    value = re.sub(r"\s+", " ", value)[:80].rstrip(" ._") or "IPTV Recording"
    if value.upper() in _WINDOWS_RESERVED_NAMES:
        value = "_" + value
    return value


def redact_recording_error(text: str) -> str:
    """Remove stream URLs and credential-bearing headers from diagnostics."""
    value = _URL_IN_DIAGNOSTIC_RE.sub("<stream URL>", str(text or ""))
    return _SECRET_HEADER_RE.sub(r"\1: <redacted>", value).strip()


def default_recording_dir() -> str:
    return str(Path.home() / "Videos" / "DeepFlux Recordings")


class StreamRecorder:
    """Lifecycle-safe FFmpeg stream-copy recorder for the playing network URL."""

    def __init__(
        self,
        ffmpeg_path: str,
        output_dir: str = "",
        on_status: Optional[Callable[[str, str], None]] = None,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.output_dir = output_dir or default_recording_dir()
        self.on_status = on_status
        self._popen = popen_factory
        self._process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._output_path = ""
        self._lock = threading.RLock()
        self._stopping = False

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def output_path(self) -> str:
        return self._output_path

    @staticmethod
    def build_command(
        ffmpeg_path: str,
        url: str,
        output_path: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        cmd = [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin"]
        clean_headers = {
            str(k).replace("\r", "").replace("\n", ""): str(v).replace("\r", "").replace("\n", "")
            for k, v in (headers or {}).items() if k and v
        }
        if clean_headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in clean_headers.items())]
        return cmd + ["-i", url, "-map", "0", "-c", "copy", "-f", "matroska", output_path]

    def _unique_output(self, title: str) -> str:
        os.makedirs(self.output_dir, exist_ok=True)
        stem = f"{sanitize_recording_name(title)} {datetime.now():%Y-%m-%d %H-%M-%S}"
        candidate = os.path.join(self.output_dir, stem + ".mkv")
        suffix = 2
        while os.path.exists(candidate):
            candidate = os.path.join(self.output_dir, f"{stem} ({suffix}).mkv")
            suffix += 1
        return candidate

    def _emit(self, state: str, message: str) -> None:
        if self.on_status is not None:
            self.on_status(state, message)

    def start(
        self, url: str, title: str = "", headers: Optional[Dict[str, str]] = None
    ) -> tuple[bool, str]:
        with self._lock:
            if self.is_recording:
                return False, "A recording is already in progress."
            if not self.ffmpeg_path or not os.path.isfile(self.ffmpeg_path):
                return False, "FFmpeg is unavailable; recording cannot start."
            if not is_network_stream_url(url):
                return False, "Only the currently playing network stream can be recorded."
            try:
                output = self._unique_output(title)
                cmd = self.build_command(self.ffmpeg_path, url, output, headers)
                # Never log ``cmd``: it may contain provider credentials in the
                # stream URL or request headers.
                process = self._popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=_CREATION_FLAGS,
                )
            except OSError as exc:
                message = redact_recording_error(str(exc)) or "Could not start FFmpeg."
                self._emit("error", message)
                return False, message
            self._process = process
            self._output_path = output
            self._stopping = False
            self._emit("recording", f"Recording to {os.path.basename(output)}")
            monitor = threading.Thread(
                target=self._monitor, args=(process,), daemon=True)
            self._monitor_thread = monitor
            monitor.start()
            return True, output

    def _monitor(self, process: subprocess.Popen) -> None:
        try:
            _stdout, stderr = process.communicate()
            returncode = process.returncode
        except Exception as exc:
            stderr, returncode = str(exc), -1
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        with self._lock:
            if process is not self._process:
                return
            stopped = self._stopping
            self._process = None
            self._stopping = False
        if stopped or returncode == 0:
            self._emit("stopped", f"Recording saved: {os.path.basename(self._output_path)}")
        else:
            detail = redact_recording_error(stderr or "FFmpeg stopped unexpectedly.")
            self._emit("error", detail[-500:])

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False
            self._stopping = True
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2.0)
            except subprocess.SubprocessError:
                pass
        except OSError:
            pass
        monitor = self._monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=2.0)
        return True

    def shutdown(self) -> None:
        self.stop()
        monitor = self._monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=2.0)


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

    def merge_av(self, video_path: str, audio_path: str, output_path: str) -> bool:
        """Mux separate video and audio tracks into one output without re-encoding."""
        if not self.available:
            return False
        cmd = [
            self._ffmpeg_path, "-y",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c", "copy",
            output_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=600,
                creationflags=_CREATION_FLAGS,
            )
            if result.returncode == 0:
                return True
            logger.error("FFmpeg A/V mux failed with return code %d", result.returncode)
            return False
        except Exception as exc:
            logger.error("FFmpeg A/V mux error: %s", exc)
            return False

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
