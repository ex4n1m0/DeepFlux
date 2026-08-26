"""Voice input tests: PCM -> whisper audio conversion (pure numpy, no mic)
and the voice config round-trip. The QAudioSource/whisper paths need real
hardware + model downloads, so they are not exercised here."""
import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtMultimedia import QAudioFormat

from gui.voice_input import SAMPLE_RATE, pcm_to_whisper_audio


def _sine_pcm16(seconds: float, rate: int = SAMPLE_RATE, channels: int = 1) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    tone = np.sin(2 * np.pi * 440 * t)
    frames = np.repeat(tone[:, None], channels, axis=1).reshape(-1)
    return (frames * 20000).astype(np.int16).tobytes()


def test_int16_mono_passthrough():
    pcm = _sine_pcm16(1.0)
    audio = pcm_to_whisper_audio(pcm, SAMPLE_RATE, 1, QAudioFormat.SampleFormat.Int16)
    assert audio.dtype == np.float32
    assert len(audio) == SAMPLE_RATE
    assert np.abs(audio).max() == pytest.approx(20000 / 32768, rel=1e-3)


def test_stereo_is_downmixed_to_mono():
    pcm = _sine_pcm16(0.5, channels=2)
    audio = pcm_to_whisper_audio(pcm, SAMPLE_RATE, 2, QAudioFormat.SampleFormat.Int16)
    assert len(audio) == SAMPLE_RATE // 2


def test_48khz_is_resampled_to_16khz():
    pcm = _sine_pcm16(1.0, rate=48000)
    audio = pcm_to_whisper_audio(pcm, 48000, 1, QAudioFormat.SampleFormat.Int16)
    assert len(audio) == pytest.approx(SAMPLE_RATE, rel=0.01)


def test_silence_stays_silent():
    pcm = b"\x00\x00" * SAMPLE_RATE
    audio = pcm_to_whisper_audio(pcm, SAMPLE_RATE, 1, QAudioFormat.SampleFormat.Int16)
    assert np.abs(audio).max() == 0.0


def test_voice_config_roundtrip(tmp_path):
    from config import DeeptorrentConfig

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "voice": {"model": "small", "language": "en", "auto_send": False,
                  "stale_key": "dropped"},
    }), encoding="utf-8")
    cfg = DeeptorrentConfig.from_file(str(cfg_file))
    assert cfg.voice.model == "small"
    assert cfg.voice.language == "en"
    assert cfg.voice.auto_send is False
    assert not hasattr(cfg.voice, "stale_key")

    cfg.to_file(str(cfg_file))
    reloaded = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert reloaded["voice"]["model"] == "small"


def test_voice_config_defaults_when_missing(tmp_path):
    from config import DeeptorrentConfig

    cfg = DeeptorrentConfig.from_file(str(tmp_path / "missing.json"))
    assert cfg.voice.enabled is True
    assert cfg.voice.model == "base"
    assert cfg.voice.language == ""
