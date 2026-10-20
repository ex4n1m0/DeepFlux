"""Local AI (6.0): llama.cpp runtime + uncensored GGUF ladder + provider.

Covers the manifest provenance pins, the MSP-ported hardware fit matrix,
the resumable sha256-verified downloader, the server argv contract, the
ensure_running gating and the installer step's provider-switch heuristic.
Everything runs against tmp dirs (conftest redirects DF_MODELS_DIR /
DF_LLAMA_DIR); no test may touch the real ~/.deeptorrent/models (the
whisper weights live there) or spawn a real llama-server.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

import pytest

from config import DeeptorrentConfig, LLM_PROVIDER_PRESETS
# Heap-fragility guard (see AGENTS.md single-process AV note):
# import the heavy agent package EARLY, before the run's
# allocations pile up — a late first import can AV on
# fragile heaps once local_model.py's module shape changed
# (2026-10-20; the crash site was _compile_bytecode inside
# agent/__init__, code paths never executed).
import agent.llm  # noqa: F401
from infra import local_model, local_model_setup


# ---------------------------------------------------------------------------
# Manifest integrity
# ---------------------------------------------------------------------------

def test_manifest_pins_are_complete():
    models = local_model.load_manifest()
    assert len(models) >= 6
    ids = {m["id"] for m in models}
    assert len(ids) == len(models)
    for m in models:
        assert len(m["revision"]) == 40 and all(c in "0123456789abcdef" for c in m["revision"])
        assert len(m["sha256"]) == 64
        assert m["size"] > 1_000_000
        assert m["file"].endswith(".gguf")
        assert m["license"] == "apache-2.0"


def test_manifest_agrees_with_preset_default():
    """config.py's preset model id and the module default must be manifest
    ids (three copies of one literal — this pins them together)."""
    models = local_model.load_manifest()
    ids = {m["id"] for m in models}
    assert local_model.DEFAULT_MODEL_ID in ids
    assert LLM_PROVIDER_PRESETS["local"]["models"][0] in ids
    assert LLM_PROVIDER_PRESETS["local"]["base_url"] == local_model.DEFAULT_BASE_URL


def test_manifest_has_both_uncensored_families():
    families = {m["family"] for m in local_model.load_manifest()}
    assert "qwen3.5-9b-uncensored" in families   # HauhauCS primary
    assert "qwen3.5-4b-abliterated" in families  # CPU/small-VRAM fallback


# ---------------------------------------------------------------------------
# Hardware fit matrix (MSP numbers: 90% gates, ceil-128MB RAM estimate)
# ---------------------------------------------------------------------------

def _entry(q: str) -> dict:
    return next(m for m in local_model.load_manifest() if m["quant"] == q)


def test_ram_estimate_formula():
    # 2.52 GiB entry (2707514688 B = 2582 MB) + 384 MB -> ceil to 128 -> 3072
    est = local_model.ram_estimate_mb(2_707_514_688)
    assert est == 3072


def test_fit_tier_vram_thresholds():
    big = local_model.HardwareInfo(cpu_cores=8, ram_mb=32768,
                                   gpu_name="Test GPU", vram_mb=6144)
    # Q4_K_M 9B = 5363 MB weights; 90% of 6 GB = 5529 -> full offload.
    assert local_model.fit_tier(big, _entry("Q4_K_M")) == "gpu_full"
    # Q8_0 9B = 9085 MB > 5529 -> partial.
    assert local_model.fit_tier(big, _entry("Q8_0")) == "gpu_partial"
    # No GPU -> CPU tier.
    cpu = local_model.HardwareInfo(cpu_cores=8, ram_mb=32768)
    assert local_model.fit_tier(cpu, _entry("Q4_K_M")) == "ram_only"


def test_fit_tier_ram_gate_blocks():
    hw = local_model.HardwareInfo(cpu_cores=4, ram_mb=2048)  # 90% = 1843 MB
    for m in local_model.load_manifest():
        assert local_model.fit_tier(hw, m) is None


def test_recommend_big_gpu_prefers_largest_fitting_quant():
    hw = local_model.HardwareInfo(cpu_cores=8, ram_mb=32768,
                                  gpu_name="RTX", vram_mb=16384)
    entry, reason = local_model.recommend(hw)
    assert entry["quant"] == "Q8_0" and entry["params_b"] == 9
    assert "fits your GPU" in reason


def test_recommend_six_gb_gpu_gets_9b_q4():
    hw = local_model.HardwareInfo(cpu_cores=8, ram_mb=16384,
                                  gpu_name="6GB", vram_mb=6144)
    entry, _ = local_model.recommend(hw)
    assert entry["id"] == "qwen35-9b-uncensored-q4km"


def test_recommend_cpu_only_prefers_smallest_model():
    """Deliberate deviation from MSP's 8-GiB-peak rule: on CPU the smaller
    model wins — agent loops are multi-turn and decode speed is king."""
    hw = local_model.HardwareInfo(cpu_cores=8, ram_mb=32768)
    entry, reason = local_model.recommend(hw)
    assert entry["id"] == "huihui-qwen35-4b-abliterated-q4km"
    assert "runs on the CPU" in reason


def test_recommend_weak_machine_returns_none_with_reason():
    hw = local_model.HardwareInfo(cpu_cores=2, ram_mb=2048)
    entry, reason = local_model.recommend(hw)
    assert entry is None and reason


def test_device_parser_ranks_discrete_over_bigger_integrated():
    """Live on this dev machine (2026-10-19): an Intel iGPU reports 18275 MiB
    of SHARED memory and would outrank the real RTX 5080 (16003 MiB) —
    integrated GPUs must rank last regardless of reported size."""
    text = ("Available devices:\n"
            "  Vulkan0: NVIDIA GeForce RTX 5080 Laptop GPU (16003 MiB, 15235 MiB free)\n"
            "  Vulkan1: Intel(R) Graphics (18275 MiB, 17507 MiB free)\n")
    ranked = local_model._parse_devices(text)
    assert ranked[0] == ("NVIDIA GeForce RTX 5080 Laptop GPU", 16003)
    assert ranked[1][0].startswith("Intel")
    # AMD APU graphics are integrated too; RX dGPUs stay discrete.
    assert local_model._is_integrated_gpu("AMD Radeon(TM) Graphics")
    assert not local_model._is_integrated_gpu("AMD Radeon RX 7900 XTX")
    assert not local_model._is_integrated_gpu("NVIDIA GeForce RTX 5080")
    assert local_model._is_integrated_gpu("Intel(R) UHD Graphics 770")


# ---------------------------------------------------------------------------
# Downloader (mocked HTTP; Range resume + sha256 verify)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self._pos = 0

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        while self._pos < len(self._payload):
            chunk = self._payload[self._pos:self._pos + chunk_size]
            self._pos += len(chunk)
            yield chunk

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _entry_with(size: int, payload: bytes) -> dict:
    return {
        "id": "test-model", "repo": "t/m", "revision": "0" * 40,
        "file": "test.gguf", "size": size,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "quant": "Q4_K_M", "params_b": 1, "family": "t",
        "license": "apache-2.0", "label": "Test",
    }


def test_download_verifies_sha_and_lands(tmp_path, monkeypatch):
    payload = os.urandom(300_000)
    entry = _entry_with(len(payload), payload)
    monkeypatch.setattr(local_model.requests, "get",
                        lambda *a, **k: _FakeResponse(payload))
    progress = []
    path = local_model.download_model(entry, on_progress=lambda d, t: progress.append((d, t)))
    assert os.path.getsize(path) == len(payload)
    assert not os.path.exists(path + ".part")
    assert progress and progress[-1] == (len(payload), len(payload))
    assert local_model.is_installed(entry)


def test_download_sha_mismatch_fails_and_cleans_part(tmp_path, monkeypatch):
    payload = os.urandom(200_000)
    entry = _entry_with(len(payload), payload)
    entry["sha256"] = "0" * 64  # wrong pin
    monkeypatch.setattr(local_model.requests, "get",
                        lambda *a, **k: _FakeResponse(payload))
    with pytest.raises(RuntimeError, match="sha256"):
        local_model.download_model(entry)
    assert not os.path.exists(local_model.model_path(entry))
    assert not os.path.exists(local_model.model_path(entry) + ".part")


def test_download_resumes_from_part(tmp_path, monkeypatch):
    payload = os.urandom(400_000)
    entry = _entry_with(len(payload), payload)
    part = local_model.model_path(entry) + ".part"
    os.makedirs(os.path.dirname(part), exist_ok=True)
    with open(part, "wb") as fh:  # first half already on disk
        fh.write(payload[:200_000])
    served = {"headers": None}

    def fake_get(url, stream=True, timeout=None, headers=None, allow_redirects=True):
        served["headers"] = headers
        rng = (headers or {}).get("Range", "")
        if rng == f"bytes={200_000}-":
            return _FakeResponse(payload[200_000:], status=206,
                                 headers={"Content-Range": f"bytes {200_000}-{len(payload)-1}/{len(payload)}"})
        return _FakeResponse(payload)

    monkeypatch.setattr(local_model.requests, "get", fake_get)
    path = local_model.download_model(entry)
    assert served["headers"]["Range"] == f"bytes={200_000}-"
    assert open(path, "rb").read() == payload  # resumed bytes match exactly


def test_download_cancel_keeps_part(tmp_path, monkeypatch):
    # >2 chunks: the transfer is interrupted mid-stream, after real bytes.
    payload = os.urandom(2_500_000)
    entry = _entry_with(len(payload), payload)

    class _CancelMidStream(_FakeResponse):
        def iter_content(self, chunk_size):
            for i, chunk in enumerate(self._yield_chunks(chunk_size)):
                if i == 1:
                    cancel.set()
                yield chunk

        def _yield_chunks(self, chunk_size):
            while self._pos < len(self._payload):
                chunk = self._payload[self._pos:self._pos + chunk_size]
                self._pos += len(chunk)
                yield chunk

    monkeypatch.setattr(local_model.requests, "get",
                        lambda *a, **k: _CancelMidStream(payload))
    cancel = threading.Event()
    with pytest.raises(RuntimeError, match="cancelled"):
        local_model.download_model(entry, cancel=cancel)
    # The partial stays on disk — an interrupted multi-GB download must
    # resume, not restart.
    assert os.path.exists(local_model.model_path(entry) + ".part")


def test_remove_model(tmp_path, monkeypatch):
    payload = b"x" * 1000
    entry = _entry_with(1000, payload)
    monkeypatch.setattr(local_model.requests, "get",
                        lambda *a, **k: _FakeResponse(payload))
    path = local_model.download_model(entry)
    assert local_model.remove_model(entry)
    assert not os.path.exists(path)


# ---------------------------------------------------------------------------
# Server argv + lifecycle gating
# ---------------------------------------------------------------------------

def test_server_args_contract():
    hw = local_model.HardwareInfo(cpu_cores=16, ram_mb=32768,
                                  gpu_name="RTX", vram_mb=16384)
    gguf = r"C:\models\m.gguf"
    args = local_model.build_server_args(gguf, "mid", hw, "gpu_full", "vulkan")
    assert args[0] == "llama-server"
    joined = " ".join(args)
    assert "--host 127.0.0.1" in joined and "--port 8199" in joined
    assert f"-m {gguf}" in joined and "--alias mid" in joined
    assert "-c 32768" in joined and "--no-webui" in joined
    assert "--reasoning-format deepseek" in joined  # thinking -> reasoning_content
    assert "-ngl all" in joined          # weights fit -> full offload
    assert "-t 8" in joined               # half the logical cores
    # CPU variant / partial tier: no -ngl (b11727 defaults to auto-fit).
    args2 = local_model.build_server_args(gguf, "mid", hw, "gpu_partial", "vulkan")
    assert "-ngl" not in args2
    args3 = local_model.build_server_args(gguf, "mid", hw, "ram_only", "cpu")
    assert "-ngl" not in args3


def test_start_server_adopts_stateless_healthy_server(monkeypatch, tmp_path):
    """A healthy server whose state file is gone (spawned outside
    start_server) must be ADOPTED, not replaced — the replacement would
    lose the port race and both would be useless (live lesson 2026-10-19)."""
    entry = dict(next(iter(local_model.load_manifest())))
    gguf = local_model.model_path(entry)
    os.makedirs(os.path.dirname(gguf), exist_ok=True)
    with open(gguf, "wb") as fh:
        fh.write(b"x" * 8)
    monkeypatch.setattr(local_model, "is_running", lambda timeout=2.5: True)
    spawns = []
    monkeypatch.setattr(local_model, "_spawn", lambda exe, args: spawns.append(exe) or 111)
    # No state file at all -> adopt.
    assert local_model.start_server(entry) is True
    assert spawns == []
    # State naming a DIFFERENT model -> deliberate switch, replace it.
    other = dict(entry)
    other["id"] = "another-model"
    local_model._write_state({"pid": 4711, "port": 8199, "model_id": "another-model",
                              "started": 1.0})
    monkeypatch.setattr(local_model, "stop_server", lambda: True)
    monkeypatch.setattr(local_model, "find_server_exe",
                        lambda variant="vulkan": f"C:/ll/{variant}/llama-server.exe")
    assert local_model.start_server(entry) is True
    assert len(spawns) == 1  # the replacement spawn DID happen


def test_start_server_waits_on_in_flight_load(monkeypatch, tmp_path):
    """A same-model load already in progress (spawned moments ago, port not
    bound yet) must be WAITED ON, not double-spawned onto the same port —
    the double spawn raced both servers to death in live use (2026-10-19)."""
    import time as _time
    entry = dict(next(iter(local_model.load_manifest())))
    gguf = local_model.model_path(entry)
    os.makedirs(os.path.dirname(gguf), exist_ok=True)
    with open(gguf, "wb") as fh:
        fh.write(b"x" * 8)
    monkeypatch.setattr(local_model, "is_running", lambda timeout=2.5: False)
    monkeypatch.setattr(local_model, "_pid_is_our_server", lambda pid, exe=None: True)
    monkeypatch.setattr(local_model, "_load_budget", lambda hw=None: 600.0)
    spawns = []
    monkeypatch.setattr(local_model, "_spawn", lambda exe, args: spawns.append(exe) or 111)
    local_model._write_state({"pid": 4711, "port": 8199, "model_id": entry["id"],
                              "started": _time.time() - 5, "exe": "x"})
    hw = local_model.HardwareInfo(cpu_cores=8, ram_mb=32768, vram_mb=16384)
    assert local_model.start_server(entry, hw=hw) is True
    assert spawns == []  # waited on the in-flight load instead


def test_start_server_prefers_vulkan_even_without_gpu_info(monkeypatch, tmp_path):
    """The hardware probe is ADVISORY: a transient --list-devices failure
    (vram_mb == 0) must not force the CPU build — the vulkan binary runs
    CPU-only when no device exists, so it stays first."""
    entry = dict(next(iter(local_model.load_manifest())))
    gguf = local_model.model_path(entry)
    os.makedirs(os.path.dirname(gguf), exist_ok=True)
    with open(gguf, "wb") as fh:
        fh.write(b"x" * 8)
    monkeypatch.setattr(local_model, "is_running", lambda timeout=2.5: False)
    monkeypatch.setattr(local_model, "_pid_is_our_server", lambda pid, exe=None: False)
    monkeypatch.setattr(local_model, "find_server_exe",
                        lambda variant="vulkan": f"C:/ll/{variant}/llama-server.exe")
    spawns = []
    monkeypatch.setattr(local_model, "_spawn",
                        lambda exe, args: spawns.append(os.path.basename(os.path.dirname(exe))) or 111)
    no_gpu = local_model.HardwareInfo(cpu_cores=8, ram_mb=32768, vram_mb=0)
    assert local_model.start_server(entry, hw=no_gpu) is True
    assert spawns == ["vulkan"]  # vulkan attempted even with no GPU info


def test_ensure_running_gates_on_provider_and_url(monkeypatch):
    calls = []
    monkeypatch.setattr(local_model, "start_server",
                        lambda e, hw=None: calls.append("start") or True)
    monkeypatch.setattr(local_model, "wait_until_ready", lambda timeout=None: True)
    monkeypatch.setattr(local_model, "ensure_runtime", lambda on_line=None: True)
    monkeypatch.setattr(local_model, "is_running", lambda timeout=2.5: False)

    cfg = DeeptorrentConfig()
    cfg.llm.provider = "deepseek"
    assert local_model.ensure_running(cfg) is False
    assert calls == []

    cfg.llm.provider = "local"
    cfg.llm.model = "no-such-model"
    # No installed model -> never spawns.
    assert local_model.ensure_running(cfg) is False
    assert calls == []

    payload = b"x" * 10
    entry = _entry_with(10, payload)
    os.makedirs(os.path.dirname(local_model.model_path(entry)), exist_ok=True)
    with open(local_model.model_path(entry), "wb") as fh:
        fh.write(payload)
    # The id is unknown to the real manifest -> resolve via the installed set.
    monkeypatch.setattr(local_model, "installed_models", lambda: [entry])
    cfg.llm.model = entry["id"]
    assert local_model.ensure_running(cfg) is True
    assert calls == ["start"]

    # A custom base URL (someone else's server) is never ours to start.
    cfg.llm.base_url = "http://127.0.0.1:9999/v1"
    assert local_model.ensure_running(cfg) is False


def test_reap_orphan_removes_dead_state(tmp_path, monkeypatch):
    monkeypatch.setattr(local_model, "is_running", lambda timeout=2.5: False)
    monkeypatch.setattr(local_model, "_pid_is_our_server", lambda pid, exe=None: False)
    local_model._write_state({"pid": 424242, "port": 8199, "model_id": "x"})
    local_model.reap_orphan()
    assert not os.path.exists(local_model._state_path())


def test_reap_orphan_keeps_healthy_server(tmp_path, monkeypatch):
    monkeypatch.setattr(local_model, "is_running", lambda timeout=2.5: True)
    local_model._write_state({"pid": 424242, "port": 8199, "model_id": "x"})
    local_model.reap_orphan()
    assert os.path.exists(local_model._state_path())


def test_posix_and_missing_runtime_are_no_ops(monkeypatch):
    monkeypatch.setattr(local_model.sys, "platform", "linux")
    assert local_model.find_server_exe("vulkan") is None
    assert local_model.ensure_runtime() is False


def test_download_complete_part_verifies_without_http(tmp_path, monkeypatch):
    """Cancel landing on the FINAL chunk (or a crash before the rename)
    leaves a complete .part — the next run must verify+rename it without
    issuing a Range request the CDN would answer 416."""
    payload = os.urandom(500_000)
    entry = _entry_with(len(payload), payload)
    part = local_model.model_path(entry) + ".part"
    os.makedirs(os.path.dirname(part), exist_ok=True)
    with open(part, "wb") as fh:
        fh.write(payload)

    def no_http(*a, **k):
        raise AssertionError("no HTTP request may happen for a complete part")

    monkeypatch.setattr(local_model.requests, "get", no_http)
    path = local_model.download_model(entry)
    assert open(path, "rb").read() == payload
    assert not os.path.exists(part)


def test_pid_check_is_exact_and_image_verified(monkeypatch):
    """123 must NOT match 1234, only llama-server.exe counts, and when the
    caller knows the exe PATH we spawned, a same-image foreign server
    (ModelSwarm runs llama-server.exe too — verified on this machine)
    must be refused. The kill path is taskkill /T /F."""
    csv_out = ('"llama-server.exe","1234","Console","1","1,024 K"\r\n'
               '"llama-server.exe","501","Console","1","1,024 K"\r\n')

    class _FakeProc:
        stdout = csv_out
        returncode = 0

    monkeypatch.setattr(local_model.subprocess, "run", lambda *a, **k: _FakeProc())
    assert local_model._pid_is_our_server(501) is True
    assert local_model._pid_is_our_server(123) is False    # substring trap
    assert local_model._pid_is_our_server(1234) is True
    assert local_model._pid_is_our_server(0) is False
    assert local_model._pid_is_our_server(-5) is False
    # Exe-path gate: foreign path -> refuse; ours/unknown -> accept.
    ours = r"C:\Users\x\.deeptorrent\llama\vulkan\llama-server.exe"
    monkeypatch.setattr(local_model, "_process_exe",
                        lambda pid: r"C:\Users\x\AppData\Local\ModelSwarm\engine-vulkan\llama-server.exe")
    assert local_model._pid_is_our_server(501, exe=ours) is False
    monkeypatch.setattr(local_model, "_process_exe", lambda pid: ours)
    assert local_model._pid_is_our_server(501, exe=ours) is True
    monkeypatch.setattr(local_model, "_process_exe", lambda pid: "")  # probe failed
    assert local_model._pid_is_our_server(501, exe=ours) is True


def test_reap_orphan_spares_server_inside_load_budget(tmp_path, monkeypatch):
    """A spawned-but-still-loading server (health not up yet, GPU loads run
    up to 600s) must NOT be reaped — a racing ensure-running thread would
    otherwise kill a perfectly good load."""
    monkeypatch.setattr(local_model, "is_running", lambda timeout=2.5: False)
    monkeypatch.setattr(local_model, "_pid_is_our_server", lambda pid, exe=None: True)
    killed = []
    monkeypatch.setattr(local_model.subprocess, "run",
                        lambda *a, **k: killed.append(a) or None)
    hw = local_model.HardwareInfo(cpu_cores=8, ram_mb=32768, vram_mb=16384)
    monkeypatch.setattr(local_model, "detect_hardware", lambda refresh=False: hw)
    local_model._write_state({"pid": 4711, "port": 8199, "model_id": "x",
                              "started": time.time() - 10})
    local_model.reap_orphan()
    assert not killed
    assert os.path.exists(local_model._state_path())
    # Long past the load budget and still unhealthy -> reaped.
    local_model._write_state({"pid": 4711, "port": 8199, "model_id": "x",
                              "started": time.time() - 10_000})
    local_model.reap_orphan()
    assert killed
    assert not os.path.exists(local_model._state_path())


def test_ensure_running_reconciles_model_id_in_memory(monkeypatch):
    """When the configured model is missing but another is installed, the
    served alias and the requested model id must stay in agreement."""
    monkeypatch.setattr(local_model, "start_server", lambda e, hw=None: True)
    monkeypatch.setattr(local_model, "wait_until_ready", lambda timeout=None: True)
    monkeypatch.setattr(local_model, "ensure_runtime", lambda on_line=None: True)
    monkeypatch.setattr(local_model, "is_running", lambda timeout=2.5: False)
    payload = b"x" * 10
    entry = _entry_with(10, payload)
    os.makedirs(os.path.dirname(local_model.model_path(entry)), exist_ok=True)
    with open(local_model.model_path(entry), "wb") as fh:
        fh.write(payload)
    monkeypatch.setattr(local_model, "installed_models", lambda: [entry])

    cfg = DeeptorrentConfig()
    cfg.llm.provider = "local"
    cfg.llm.model = "removed-model-id"
    cfg.llm.fast_model = "removed-model-id"
    assert local_model.ensure_running(cfg) is True
    assert cfg.llm.model == entry["id"]      # in-memory reconciliation
    assert cfg.llm.fast_model == entry["id"]


# ---------------------------------------------------------------------------
# Installer step: provider-switch heuristic + idempotency
# ---------------------------------------------------------------------------

def _write_config(path, llm: dict | None = None):
    data: dict = {}
    if llm is not None:
        data["llm"] = llm
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def test_should_default_to_local(tmp_path):
    p = str(tmp_path / "config.json")
    _write_config(p)                                     # fresh install
    assert local_model_setup.should_default_to_local(p) is True
    _write_config(p, {"provider": "dummy"})              # keyless fossil
    assert local_model_setup.should_default_to_local(p) is True
    _write_config(p, {"provider": "deepseek", "api_key": "sk-user"})
    assert local_model_setup.should_default_to_local(p) is False
    _write_config(p, {"provider": "openrouter"})         # deliberate choice
    assert local_model_setup.should_default_to_local(p) is False


def test_run_setup_switches_fresh_install_to_local(tmp_path, monkeypatch):
    p = str(tmp_path / "config.json")
    _write_config(p)
    hw = local_model.HardwareInfo(cpu_cores=8, ram_mb=32768,
                                  gpu_name="RTX", vram_mb=16384)
    entry = dict(_entry("Q4_K_M"))
    started = {}
    monkeypatch.setattr(local_model, "ensure_runtime", lambda on_line=None: True)
    monkeypatch.setattr(local_model, "detect_hardware", lambda refresh=False: hw)
    monkeypatch.setattr(local_model, "recommend",
                        lambda h, models=None: (entry, "test reason"))
    monkeypatch.setattr(local_model, "is_installed", lambda e: True)
    monkeypatch.setattr(local_model, "reap_orphan", lambda: None)
    monkeypatch.setattr(local_model, "start_server",
                        lambda e, hw=None: started.setdefault("a", True))
    monkeypatch.setattr(local_model, "wait_until_ready", lambda timeout=None: True)

    lines = []
    result = local_model_setup.run_setup(config_path=p, on_progress=lines.append)
    assert result["ok"] and result["provider_switched"] and result["started"]
    assert result["model_id"] == entry["id"]
    cfg = DeeptorrentConfig.from_file(p)
    assert cfg.llm.provider == "local"
    assert cfg.llm.model == entry["id"]
    assert cfg.llm.base_url == local_model.DEFAULT_BASE_URL
    assert cfg.llm.fast_model == entry["id"]
    assert any("Recommended" in ln for ln in lines)


def test_run_setup_never_stomps_cloud_choice(tmp_path, monkeypatch):
    p = str(tmp_path / "config.json")
    _write_config(p, {"provider": "openrouter", "api_key": "sk-or"})
    hw = local_model.HardwareInfo(cpu_cores=8, ram_mb=32768)
    entry = dict(next(m for m in local_model.load_manifest()))
    monkeypatch.setattr(local_model, "ensure_runtime", lambda on_line=None: True)
    monkeypatch.setattr(local_model, "detect_hardware", lambda refresh=False: hw)
    monkeypatch.setattr(local_model, "recommend",
                        lambda h, models=None: (entry, "test"))
    monkeypatch.setattr(local_model, "is_installed", lambda e: True)
    monkeypatch.setattr(local_model, "reap_orphan", lambda: None)
    monkeypatch.setattr(local_model, "start_server", lambda e, hw=None: True)
    monkeypatch.setattr(local_model, "wait_until_ready", lambda timeout=None: True)
    result = local_model_setup.run_setup(config_path=p)
    assert result["ok"] and not result["provider_switched"]
    cfg = DeeptorrentConfig.from_file(p)
    assert cfg.llm.provider == "openrouter"


def test_run_setup_skips_weak_hardware(tmp_path, monkeypatch):
    p = str(tmp_path / "config.json")
    _write_config(p)
    weak = local_model.HardwareInfo(cpu_cores=2, ram_mb=2048)
    monkeypatch.setattr(local_model, "ensure_runtime", lambda on_line=None: True)
    monkeypatch.setattr(local_model, "detect_hardware", lambda refresh=False: weak)
    result = local_model_setup.run_setup(config_path=p)
    assert result["ok"] is True and result["skipped"]
    assert not result["downloaded"]


def test_run_setup_downloads_when_missing(tmp_path, monkeypatch):
    p = str(tmp_path / "config.json")
    _write_config(p)
    hw = local_model.HardwareInfo(cpu_cores=8, ram_mb=32768, vram_mb=16384,
                                  gpu_name="RTX")
    entry = dict(_entry("Q4_K_M"))
    monkeypatch.setattr(local_model, "ensure_runtime", lambda on_line=None: True)
    monkeypatch.setattr(local_model, "detect_hardware", lambda refresh=False: hw)
    monkeypatch.setattr(local_model, "recommend",
                        lambda h, models=None: (entry, "test"))
    monkeypatch.setattr(local_model, "is_installed", lambda e: False)
    monkeypatch.setattr(local_model, "download_model",
                        lambda e, on_progress=None, cancel=None: local_model.model_path(e))
    monkeypatch.setattr(local_model, "reap_orphan", lambda: None)
    monkeypatch.setattr(local_model, "start_server", lambda e, hw=None: True)
    monkeypatch.setattr(local_model, "wait_until_ready", lambda timeout=None: True)
    result = local_model_setup.run_setup(config_path=p, on_bytes=lambda d, t: None)
    assert result["downloaded"] and result["provider_switched"]


# ---------------------------------------------------------------------------
# Provider wiring: keyless "local" must construct the REAL client
# ---------------------------------------------------------------------------

def test_keyless_local_boot_is_not_dummy():
    from agent.llm import DeepSeekClient, create_llm_client
    cfg = DeeptorrentConfig()
    cfg.llm.provider = "local"
    cfg.llm.api_key = ""
    cfg.llm.model = local_model.DEFAULT_MODEL_ID
    client = create_llm_client(cfg.llm)
    assert isinstance(client, DeepSeekClient)
    assert client.base_url == local_model.DEFAULT_BASE_URL
    assert client.supports_tools() is True


def test_from_file_keeps_local_provider_and_never_injects_keys(tmp_path):
    p = str(tmp_path / "config.json")
    _write_config(p, {"provider": "local",
                      "model": local_model.DEFAULT_MODEL_ID,
                      "base_url": local_model.DEFAULT_BASE_URL})
    cfg = DeeptorrentConfig.from_file(p)
    assert cfg.llm.provider == "local"
    # The shared DeepSeek key is deepseek-gated; local never receives it
    # (even on key-embedding build machines).
    assert cfg.llm.api_key == ""
    assert cfg.llm.model == local_model.DEFAULT_MODEL_ID


def test_from_file_clears_stale_deepseek_fast_model_for_local(tmp_path):
    p = str(tmp_path / "config.json")
    _write_config(p, {"provider": "local", "model": local_model.DEFAULT_MODEL_ID,
                      "fast_model": "deepseek-flash"})
    cfg = DeeptorrentConfig.from_file(p)
    assert cfg.llm.fast_model == ""


def test_posix_setup_step_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(local_model_setup.sys, "platform", "linux")
    result = local_model_setup.run_setup(config_path=str(tmp_path / "c.json"))
    assert result["ok"] is False and "Windows-only" in result["error"]


# ---------------------------------------------------------------------------
# GPU detection robustness (6.1): utf-8 probe + DXGI/nvidia-smi fallback
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0


def test_vulkan_probe_parses_stderr_and_survives_non_utf8(monkeypatch):
    monkeypatch.setattr(local_model, "find_server_exe", lambda v="vulkan": "x.exe")
    # A localized adapter name (e.g. a zh-CN driver) must not raise a
    # decode error through text=True's locale codec.
    monkeypatch.setattr(local_model.subprocess, "run",
                        lambda *a, **k: _FakeProc(
                            stderr="  Vulkan0: NVIDIA GeForce RTX 5080 \u7b14\u8bb0\u672c GPU (16003 MiB, 15000 MiB free)"))
    devices = local_model._vulkan_devices()
    assert devices and devices[0][1] == 16003


def test_missing_runtime_falls_back_to_static_inventory(monkeypatch):
    monkeypatch.setattr(local_model, "find_server_exe",
                        lambda v="vulkan": None)
    from infra import hardware_monitor
    monkeypatch.setattr(hardware_monitor, "dxgi_adapters",
                        lambda: [("NVIDIA GeForce RTX 5080 Laptop GPU", 16003),
                                 ("Intel(R) Graphics", 128)])
    local_model._hw_cache = None
    hw = local_model.detect_hardware(refresh=True)
    assert hw.gpu_name == "NVIDIA GeForce RTX 5080 Laptop GPU"
    assert hw.vram_mb == 16003


def test_probe_crash_falls_back(monkeypatch):
    monkeypatch.setattr(local_model, "find_server_exe", lambda v="vulkan": "x.exe")

    def boom(*a, **k):
        raise OSError("driver hiccup")
    monkeypatch.setattr(local_model.subprocess, "run", boom)
    from infra import hardware_monitor
    monkeypatch.setattr(hardware_monitor, "dxgi_adapters", lambda: [])
    monkeypatch.setattr(hardware_monitor, "nvidia_smi_query", lambda: [
        {"name": "NVIDIA GPU", "util": 1.0, "mem_used_mb": 0,
         "mem_total_mb": 16384}])
    devices = local_model._vulkan_devices()
    assert devices == [("NVIDIA GPU", 16384)]


def test_fallback_ranks_discrete_over_bigger_integrated(monkeypatch):
    from infra import hardware_monitor
    monkeypatch.setattr(hardware_monitor, "dxgi_adapters",
                        lambda: [("Intel(R) Graphics", 18275),
                                 ("AMD Radeon RX 7900", 20480)])
    ranked = local_model._fallback_devices()
    assert ranked[0][0] == "AMD Radeon RX 7900"
