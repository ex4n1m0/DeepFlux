"""Voice input for the agent box: mic capture (QtMultimedia) + local
faster-whisper transcription.

Fully offline: the whisper model is downloaded once from HuggingFace into
~/.deeptorrent/models/ and cached there. The recorder lives on the GUI
thread (QAudioSource); transcription runs on a daemon thread and reports
back through Qt signals, mirroring the _AgentSignals pattern.
"""
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Signal
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSource, QMediaDevices

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # whisper's native sample rate
MIN_SECONDS = 0.4    # shorter clips are treated as accidental taps


def pcm_to_whisper_audio(pcm: bytes, rate: int, channels: int,
                         sample_format: QAudioFormat.SampleFormat):
    """Raw PCM bytes -> float32 mono 16 kHz numpy array for faster-whisper."""
    import numpy as np

    fmt = QAudioFormat.SampleFormat
    if sample_format == fmt.Int16:
        data = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_format == fmt.Int32:
        data = np.frombuffer(pcm, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sample_format == fmt.UInt8:
        data = (np.frombuffer(pcm, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:  # Float
        data = np.frombuffer(pcm, dtype=np.float32).copy()
    if channels > 1 and len(data):
        data = data[: len(data) - len(data) % channels].reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and len(data) > 1:
        n = max(1, int(round(len(data) * SAMPLE_RATE / rate)))
        data = np.interp(np.linspace(0.0, len(data) - 1, n),
                         np.arange(len(data)), data).astype(np.float32)
    return data


class VoiceRecorder(QObject):
    """Captures the default microphone as raw PCM via QAudioSource."""

    failed = Signal(str)

    def __init__(self, parent: QObject = None) -> None:
        super().__init__(parent)
        self._source: QAudioSource = None
        self._buffer: QBuffer = None
        self._bytes = QByteArray()

    @property
    def recording(self) -> bool:
        return self._source is not None

    def start(self) -> bool:
        """Begin recording; emits failed() and returns False without a mic."""
        if self._source is not None:
            return True
        dev = QMediaDevices.defaultAudioInput()
        if dev.isNull():
            self.failed.emit("No microphone found.")
            return False
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not dev.isFormatSupported(fmt):
            fmt = dev.preferredFormat()
        self._bytes.clear()
        self._buffer = QBuffer(self._bytes, self)
        self._buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self._source = QAudioSource(dev, fmt, self)
        self._source.start(self._buffer)
        if self._source.error() != QAudio.Error.NoError:
            err = self._source.error()
            self._discard()
            self.failed.emit(f"Could not open the microphone (error {int(err)}).")
            return False
        return True

    def stop(self) -> bytes:
        """Stop recording; returns the captured PCM bytes (may be empty)."""
        if self._source is None:
            return b""
        self._source.stop()
        pcm = bytes(self._bytes)
        self._discard()
        return pcm

    def format_info(self):
        """(rate, channels, sample_format) of the last start() — valid while recording."""
        if self._source is None:
            return SAMPLE_RATE, 1, QAudioFormat.SampleFormat.Int16
        f = self._source.format()
        return f.sampleRate(), f.channelCount(), f.sampleFormat()

    def _discard(self) -> None:
        if self._source is not None:
            self._source.deleteLater()
            self._source = None
        if self._buffer is not None:
            self._buffer.close()
            self._buffer.deleteLater()
            self._buffer = None


class VoiceTranscriber(QObject):
    """Lazy-loads a faster-whisper model and transcribes on a worker thread."""

    status = Signal(str)   # human-readable status (model download/load)
    done = Signal(str)     # transcribed text ("" = nothing recognized)
    failed = Signal(str)

    def __init__(self, model_size: str = "base", language: str = "",
                 device: str = "auto", parent: QObject = None) -> None:
        super().__init__(parent)
        self.model_size = model_size or "base"
        self.language = (language or "").strip()
        self.device = device or "auto"
        self._model = None
        self._model_lock = threading.Lock()
        self._busy = threading.Event()

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def transcribe_async(self, audio) -> None:
        """Transcribe a float32 16 kHz numpy array; drops calls while busy."""
        if self._busy.is_set():
            return
        self._busy.set()
        threading.Thread(target=self._worker, args=(audio,), daemon=True).start()

    def _worker(self, audio) -> None:
        try:
            model = self._ensure_model()
            kwargs = {"vad_filter": True}
            if self.language:
                kwargs["language"] = self.language
            segments, _info = model.transcribe(audio, **kwargs)
            text = " ".join(seg.text.strip() for seg in segments).strip()
            self.done.emit(text)
        except Exception as exc:
            logger.exception("Voice transcription failed")
            self.failed.emit(str(exc))
        finally:
            self._busy.clear()

    def _ensure_model(self):
        with self._model_lock:
            if self._model is None:
                first = not self._model_dir().exists() or not any(self._model_dir().iterdir())
                self.status.emit(
                    "Downloading the voice model (first use only)…" if first
                    else "Loading the voice model…")
                from faster_whisper import WhisperModel
                self._model = WhisperModel(
                    self.model_size, device=self.device,
                    download_root=str(self._model_dir()))
            return self._model

    @staticmethod
    def _model_dir() -> Path:
        return Path.home() / ".deeptorrent" / "models"
