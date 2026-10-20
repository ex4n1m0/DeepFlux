"""Local AI models: llama.cpp runtime + GGUF management + the local server.

DeepFlux's "local" LLM provider (LLM_PROVIDER_PRESETS["local"]) runs an
uncensored model on the user's own machine through the official llama.cpp
``llama-server`` (MIT, pinned by packaging/fetch_llama.py — Vulkan build
for GPU offload on any vendor, CPU build otherwise). The model ladder
lives in the tracked manifest ``infra/local_models.json`` (pinned by
packaging/pin_local_models.py: repo + commit + file + LFS sha256 + size)
and the hardware->quant fit matrix is ported from the ModelSwarm project
(measured there 2026-10): RAM gate at 90%, weights <= 90% VRAM = full GPU
offload, and among fits the bigger quant wins on GPU while the SMALLER
model wins on CPU (agent loops are multi-turn — decode speed is king
without a GPU; deliberate deviation from MSP's 8 GiB-peak rule).

Everything here is Qt-free and safe from background threads. The GGUF
download is Range-resumable (sidecar .part) with a final size + sha256
verify against the manifest — HF LFS files are content-addressed, so a
resumed concatenation is byte-exact regardless of which edge node served
each half. POSIX / missing runtime: every entry point degrades to a clean
no-op (find_* -> None), mirroring infra/flaresolverr.py.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8199
DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_PORT}/v1"
# 32K covers DeepFlux's system prompt + tool schemas with room to spare;
# the agent loop's own _fit_context trims history to the token budget.
CONTEXT_SIZE = 32768
DEFAULT_MODEL_ID = "qwen35-9b-uncensored-q4km"
HF_UA = "DeepFlux-local-model/6.0"
# Health/loading budgets: a GPU load of a 9B quant takes tens of seconds
# (MSP budgets 600s); CPU-only loads are faster to first token.
START_TIMEOUT_GPU = 600.0
START_TIMEOUT_CPU = 240.0
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_BACKOFFS = (0, 2, 5, 15)
PROGRESS_EVERY_BYTES = 64 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 20 * 1024 ** 3  # sanity cap; largest ladder entry ~9.5 GB

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_server_pid: Optional[int] = None
_server_lock = threading.Lock()
_hw_cache: Optional["HardwareInfo"] = None


# ---------------------------------------------------------------------------
# Paths + manifest
# ---------------------------------------------------------------------------

def models_root() -> str:
    """Model home (shared with the whisper models voice_input downloads)."""
    env = os.environ.get("DF_MODELS_DIR")
    if env:
        return env
    return str(Path.home() / ".deeptorrent" / "models")


def llama_root() -> str:
    """Where the extracted llama.cpp runtimes live (cpu/ + vulkan/)."""
    env = os.environ.get("DF_LLAMA_DIR")
    if env:
        return env
    return str(Path.home() / ".deeptorrent" / "llama")


def _manifest_path() -> str:
    # Dev: infra/local_models.json. Frozen: app.spec ships it as a datas
    # tuple into _MEIPASS/infra/, which is exactly __file__'s directory.
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "local_models.json")


def load_manifest() -> List[Dict[str, Any]]:
    try:
        with open(_manifest_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        models = data.get("models", [])
        return [m for m in models if isinstance(m, dict) and m.get("id")]
    except Exception as exc:
        logger.warning("local model manifest unreadable: %s", exc)
        return []


def entry_by_id(model_id: str) -> Optional[Dict[str, Any]]:
    for entry in load_manifest():
        if entry.get("id") == model_id:
            return entry
    return None


def model_path(entry: Dict[str, Any]) -> str:
    return os.path.join(models_root(), entry["id"], entry["file"])


def is_installed(entry: Dict[str, Any]) -> bool:
    """Cheap installed check (size match). The full sha256 runs at download
    time; re-hashing 5-9 GB on every boot would be absurd."""
    try:
        return os.path.getsize(model_path(entry)) == int(entry["size"])
    except OSError:
        return False


def installed_models() -> List[Dict[str, Any]]:
    return [e for e in load_manifest() if is_installed(e)]


def remove_model(entry: Dict[str, Any]) -> bool:
    """Delete a model's directory (stops the server first if it is active)."""
    if entry.get("id") and _state().get("model_id") == entry["id"]:
        stop_server()
    try:
        shutil.rmtree(os.path.join(models_root(), entry["id"]), ignore_errors=True)
        return True
    except OSError as exc:
        logger.warning("remove_model failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Hardware detection + recommendation
# ---------------------------------------------------------------------------

@dataclass
class HardwareInfo:
    cpu_cores: int = 0
    ram_mb: int = 0
    gpu_name: str = ""
    vram_mb: int = 0

    def summary(self) -> str:
        cores = f"{self.cpu_cores} cores" if self.cpu_cores else "unknown CPU"
        ram = f"{self.ram_mb / 1024:.0f} GB RAM" if self.ram_mb else "unknown RAM"
        if self.vram_mb:
            return f"{self.gpu_name} ({self.vram_mb / 1024:.0f} GB VRAM), {ram}, {cores}"
        return f"No GPU detected, {ram}, {cores}"


def _total_ram_mb() -> int:
    if os.name == "nt":
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX(dwLength=ctypes.sizeof(MEMORYSTATUSEX))
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys // (1024 * 1024))
        except Exception:
            pass
    try:  # POSIX fallback
        with open("/proc/meminfo", "r", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


_DEVICE_LINE_RE = re.compile(
    r"^\s*\w+\d+:\s*(?P<name>.+?)\s*\((?P<total>\d+)\s*MiB", re.MULTILINE)

# Integrated markers (iGPUs report near-system-RAM "VRAM" because they share
# system memory — e.g. this machine's "Intel(R) Graphics (18275 MiB)" next to
# an RTX 5080's real 16 GB). A discrete GPU, even a smaller one, beats any
# iGPU for LLM inference, so integrated devices rank LAST regardless of size.
_INTEGRATED_HINTS = ("uhd", "iris", "hd graphics", "apple m")


def _is_integrated_gpu(name: str) -> bool:
    n = name.lower()
    if "intel" in n:
        return any(h in n for h in _INTEGRATED_HINTS) or n.rstrip(")").endswith("graphics")
    if "radeon" in n:
        # "(TM) Graphics" / "Vega" = AMD APUs; "RX"/"Pro" dGPUs stay discrete.
        return "vega" in n or n.rstrip(")").endswith("graphics")
    return False


def _parse_devices(text: str) -> List[Tuple[str, int]]:
    devices = [(m.group("name").strip(), int(m.group("total")))
               for m in _DEVICE_LINE_RE.finditer(text or "")]
    # Discrete first; ties/new ties broken by larger reported memory.
    return sorted(devices, key=lambda d: (_is_integrated_gpu(d[0]), -d[1]))


def _fallback_devices() -> List[Tuple[str, int]]:
    """Static GPU inventory for when the Vulkan probe comes up empty
    (runtime not extracted yet / a driver hiccup): DXGI sees every D3D
    adapter with true above-4 GB dedicated VRAM sizes, nvidia-smi covers
    NVIDIA-only boxes where DXGI somehow failed. Same discrete-first
    ranking as the probe — a working GPU must never be reported missing
    just because the llama.cpp runtime is absent (owner-reported on an
    RTX 5080 Mobile, 2026-10-20)."""
    from infra import hardware_monitor
    devices = hardware_monitor.dxgi_adapters() or [
        (g["name"], g["mem_total_mb"])
        for g in hardware_monitor.nvidia_smi_query() if g["name"]]
    return sorted(devices, key=lambda d: (_is_integrated_gpu(d[0]), -d[1]))


def _vulkan_devices() -> List[Tuple[str, int]]:
    """GPU name/VRAM straight from the runtime that will serve the model —
    the Vulkan backend sees every vendor with no nvml/WMI dependency
    (verified output format live 2026-10-19, llama.cpp b11727:
    ``  Vulkan0: NVIDIA GeForce RTX 5080 Laptop GPU (16003 MiB, 15235 MiB free)``).
    utf-8/replace decode so a localized adapter name can never kill the
    probe; stderr is parsed too. Empty/failed probes fall back to static
    enumeration (a single cold-start hiccup once forced the CPU build on a
    16 GB GPU machine, 2026-10-19)."""
    exe = find_server_exe("vulkan")
    if not exe:
        return _fallback_devices()
    try:
        proc = subprocess.run(
            [exe, "--list-devices"], capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=90, creationflags=_CREATE_NO_WINDOW)
        return _parse_devices((proc.stdout or "")
                              + "\n" + (proc.stderr or "")) \
            or _fallback_devices()
    except Exception as exc:
        logger.debug("vulkan device probe failed: %s", exc)
        return _fallback_devices()


def detect_hardware(refresh: bool = False) -> HardwareInfo:
    global _hw_cache
    if _hw_cache is not None and not refresh:
        return _hw_cache
    cpu = os.cpu_count() or 0
    hw = HardwareInfo(cpu_cores=cpu, ram_mb=_total_ram_mb())
    devices = _vulkan_devices()
    if devices:
        hw.gpu_name, hw.vram_mb = devices[0]
    _hw_cache = hw
    return hw


def ram_estimate_mb(size_bytes: int) -> int:
    """MSP's formula: weights + 128 MB KV cache + 256 MB runtime overhead,
    rounded up to 128 MB."""
    total_mb = size_bytes / (1024 * 1024) + 128 + 256
    return int((total_mb + 127) // 128 * 128)


def fit_tier(hw: HardwareInfo, entry: Dict[str, Any]) -> Optional[str]:
    """None = infeasible on this machine. Else 'gpu_full' | 'gpu_partial' |
    'ram_only' (MSP fit matrix: 90% gates both ways)."""
    ram_est = ram_estimate_mb(int(entry["size"]))
    if hw.ram_mb <= 0 or ram_est > hw.ram_mb * 9 // 10:
        return None
    weights_mb = int(entry["size"]) // (1024 * 1024)
    if hw.vram_mb > 0 and weights_mb <= hw.vram_mb * 9 // 10:
        return "gpu_full"
    if hw.vram_mb > 0:
        return "gpu_partial"
    return "ram_only"


_TIER_ORDER = {"gpu_full": 0, "gpu_partial": 1, "ram_only": 2}


def _tier_score(hw: HardwareInfo, entry: Dict[str, Any], tier: str) -> float:
    size_gb = int(entry["size"]) / (1024 ** 3)
    weights_mb = max(1, int(entry["size"]) // (1024 * 1024))
    if tier == "gpu_full":
        return 10.0 + size_gb                       # bigger quant wins (MSP)
    if tier == "gpu_partial":
        return (6.0 + 6.0 * min(1.0, hw.vram_mb / weights_mb)
                + 0.2 * size_gb)                    # MSP formula verbatim
    return 6.0 - 0.5 * size_gb                      # CPU: smaller = faster decode


def recommend(hw: Optional[HardwareInfo] = None,
              models: Optional[List[Dict[str, Any]]] = None,
              ) -> Tuple[Optional[Dict[str, Any]], str]:
    """Best ladder entry for this machine + a human reason string.
    Tier preference: full GPU offload > partial > CPU-only."""
    hw = hw or detect_hardware()
    models = models if models is not None else load_manifest()
    candidates: List[Tuple[int, float, Dict[str, Any], str]] = []
    for entry in models:
        tier = fit_tier(hw, entry)
        if tier:
            candidates.append((_TIER_ORDER[tier], _tier_score(hw, entry, tier),
                               entry, tier))
    if not candidates:
        if hw.vram_mb == 0 and hw.ram_mb and hw.ram_mb < 4096:
            return None, (f"This PC has only {hw.ram_mb / 1024:.0f} GB RAM and no "
                          "GPU — too little for a local model.")
        return None, "No local model fits this machine's memory."
    _, _, entry, tier = min(candidates, key=lambda c: (c[0], -c[1]))
    reasons = {
        "gpu_full": "fits your GPU entirely",
        "gpu_partial": "partially offloaded to your GPU",
        "ram_only": "runs on the CPU",
    }
    return entry, f"{entry['label']} ({reasons[tier]})"


# ---------------------------------------------------------------------------
# Model download (Range-resumable, sha256-verified against the manifest)
# ---------------------------------------------------------------------------

def _download_url(entry: Dict[str, Any]) -> str:
    from urllib.parse import quote
    return (f"https://huggingface.co/{entry['repo']}/resolve/"
            f"{entry['revision']}/{quote(entry['file'])}")


def _sha256_of(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def download_model(entry: Dict[str, Any],
                   on_progress: Optional[Callable[[int, int], None]] = None,
                   cancel: Optional[threading.Event] = None) -> str:
    """Download a pinned GGUF into models/<id>/<file>. Resumable: the .part
    sidecar is appended to on retry/re-run; the completed file is size +
    sha256 verified against the manifest before the atomic rename."""
    dest = model_path(entry)
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if is_installed(entry):
        return dest

    url = _download_url(entry)
    expected_size = int(entry["size"])
    headers = {"User-Agent": HF_UA}
    last_report = 0

    for attempt in range(DOWNLOAD_ATTEMPTS):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("cancelled")
        done = os.path.getsize(part) if os.path.isfile(part) else 0
        if done > expected_size:
            os.remove(part)  # stale part of something else
            done = 0
        if done == expected_size:
            break  # complete but unrenamed (cancel on the last chunk) — verify
        try:
            req_headers = dict(headers)
            if done:
                req_headers["Range"] = f"bytes={done}-"
            with requests.get(url, stream=True, timeout=(15, 90),
                              headers=req_headers, allow_redirects=True) as resp:
                if resp.status_code == 200 and done:
                    # Server ignored the range — restart from scratch.
                    done = 0
                resp.raise_for_status()
                total = expected_size
                if resp.status_code == 206:
                    content_range = resp.headers.get("Content-Range", "")
                    m = re.search(r"/(\d+)$", content_range)
                    if m and int(m.group(1)) != expected_size:
                        raise RuntimeError(
                            "the file changed on Hugging Face — the manifest is stale")
                mode = "ab" if done and resp.status_code == 206 else "wb"
                if mode == "wb":
                    done = 0
                with open(part, mode) as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        if cancel is not None and cancel.is_set():
                            raise RuntimeError("cancelled")
                        fh.write(chunk)
                        done += len(chunk)
                        if done > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError("download exceeded the sanity cap")
                        if on_progress and (done - last_report >= PROGRESS_EVERY_BYTES
                                            or done >= total):
                            last_report = done
                            try:
                                on_progress(done, total)
                            except Exception:
                                pass
        except RuntimeError:
            raise
        except Exception as exc:
            if attempt + 1 >= DOWNLOAD_ATTEMPTS:
                raise RuntimeError(f"download failed after {DOWNLOAD_ATTEMPTS} "
                                   f"attempts: {exc}") from exc
            time.sleep(DOWNLOAD_BACKOFFS[attempt])
            continue

    # Verification + atomic rename (also reached directly when the .part is
    # already complete — a cancel on the final chunk or a crash between the
    # last write and the rename must not brick the transfer behind a 416).
    got = os.path.getsize(part) if os.path.isfile(part) else 0
    if got != expected_size:
        if got:
            os.remove(part)
        raise RuntimeError(f"downloaded {got} bytes, expected {expected_size}")
    digest = _sha256_of(part)
    if digest != entry["sha256"]:
        os.remove(part)
        raise RuntimeError("sha256 mismatch — the downloaded file is corrupt")
    os.replace(part, dest)
    if on_progress:
        try:
            on_progress(expected_size, expected_size)
        except Exception:
            pass
    logger.info("downloaded local model %s (%.2f GiB)", entry["id"],
                expected_size / 1024 ** 3)
    return dest


# ---------------------------------------------------------------------------
# llama.cpp runtime (bundled zips -> ~/.deeptorrent/llama/{cpu,vulkan})
# ---------------------------------------------------------------------------

def find_server_exe(variant: str = "vulkan") -> Optional[str]:
    if sys.platform != "win32":
        return None
    exe = os.path.join(llama_root(), variant, "llama-server.exe")
    return exe if os.path.isfile(exe) else None


def _bundled_zip(variant: str) -> Optional[str]:
    """The installer-shipped zip for a variant ('cpu'/'vulkan' inside the
    asset name), if this build carries one."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        roots = [os.path.join(sys._MEIPASS, "llama")]
    else:
        roots = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "packaging", "llama")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if name.endswith(f"-bin-win-{variant}-x64.zip"):
                return os.path.join(root, name)
    return None


def ensure_runtime(on_line: Optional[Callable[[str], None]] = None) -> bool:
    """Extract the bundled llama.cpp zips once (both variants — disk cost
    ~142 MB total; keeping both lets the server fall back CPU->GPU->CPU and
    survive hardware changes). Idempotent. Frozen builds DELETE the zips
    after extraction (Inno re-copies them every install); dev keeps them —
    they are the fetch script's build input (flaresolverr lesson 2026-10-13)."""
    def say(msg: str) -> None:
        logger.info("llama runtime: %s", msg)
        if on_line:
            try:
                on_line(msg)
            except Exception:
                pass

    if sys.platform != "win32":
        return False
    extracted_any = False
    for variant in ("vulkan", "cpu"):
        if find_server_exe(variant):
            continue
        zip_path = _bundled_zip(variant)
        if zip_path is None:
            continue
        dest = os.path.join(llama_root(), variant)
        say(f"Extracting the llama.cpp {variant} runtime…")
        try:
            os.makedirs(dest, exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(dest)
        except Exception as exc:
            say(f"runtime extraction failed: {exc}")
            continue
        if not find_server_exe(variant):
            say(f"the {variant} zip did not contain llama-server.exe")
            continue
        extracted_any = True
    if extracted_any and getattr(sys, "frozen", False):
        for variant in ("vulkan", "cpu"):
            zip_path = _bundled_zip(variant)
            if zip_path:
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
    if find_server_exe("vulkan") or find_server_exe("cpu"):
        return True
    say("no llama.cpp runtime available (build did not bundle it)")
    return False


# ---------------------------------------------------------------------------
# Server lifecycle (loopback-only, port 8199)
# ---------------------------------------------------------------------------

def _state_path() -> str:
    return os.path.join(llama_root(), "server.json")


def _state() -> Dict[str, Any]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(data: Dict[str, Any]) -> None:
    # Atomic (temp + replace), same lesson as config.to_file: a truncated
    # state file would orphan a server nothing can identify by pid anymore.
    try:
        os.makedirs(llama_root(), exist_ok=True)
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _state_path())
    except OSError as exc:
        logger.debug("llama state write failed: %s", exc)


def is_running(timeout: float = 2.5) -> bool:
    try:
        resp = requests.get(f"http://127.0.0.1:{DEFAULT_PORT}/health",
                            timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def test_endpoint(base_url: str = DEFAULT_BASE_URL,
                  timeout: float = 5.0) -> Tuple[bool, str]:
    """/v1/models probe for the settings dialog's Test button."""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            ids = [m.get("id", "?") for m in data.get("data", [])][:3]
            return True, ", ".join(ids) or "no models served"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, str(exc)


def build_server_args(gguf_path: str, model_id: str, hw: HardwareInfo,
                      tier: str, variant: str) -> List[str]:
    """Pure argv builder (pinned contract from the ModelSwarm engine):
    loopback bind, embedded alias, capped context, half the logical cores.
    GPU layers: 'all' when the weights fit VRAM; otherwise omitted — b11727
    defaults to 'auto', which fits as many layers as VRAM allows."""
    threads = max(1, (hw.cpu_cores or 4) // 2)
    args = [
        "llama-server",
        "--host", "127.0.0.1",
        "--port", str(DEFAULT_PORT),
        "-m", gguf_path,
        "--alias", model_id,
        "-c", str(CONTEXT_SIZE),
        "--no-webui",
        # Qwen3.5 thinks inline by default; this maps the thinking segment to
        # OpenAI-style reasoning_content, which DeepSeekClient._consume_stream
        # already captures (the GUI dims it like DeepSeek's own thinking).
        "--reasoning-format", "deepseek",
        "-t", str(threads),
    ]
    if variant == "vulkan" and tier == "gpu_full":
        args += ["-ngl", "all"]
    return args


def _spawn(exe: str, args: List[str]) -> Optional[int]:
    try:
        proc = subprocess.Popen(
            [exe] + args[1:], cwd=os.path.dirname(exe),
            creationflags=_CREATE_NO_WINDOW, close_fds=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.pid
    except Exception as exc:
        logger.warning("llama-server spawn failed (%s): %s", exe, exc)
        return None


def start_server(entry: Dict[str, Any],
                 hw: Optional[HardwareInfo] = None) -> bool:
    """Start llama-server with the given model. If a server is already up
    serving a DIFFERENT model, it is stopped first. GPU path tries the
    Vulkan runtime, falling back to the CPU runtime on spawn failure."""
    global _server_pid
    if sys.platform != "win32":
        return False
    gguf = model_path(entry)
    if not os.path.isfile(gguf):
        logger.info("local model %s not installed", entry.get("id"))
        return False
    hw = hw or detect_hardware()
    tier = fit_tier(hw, entry) or "ram_only"

    with _server_lock:
        state = _state()
        if is_running():
            if not state.get("pid"):
                # A healthy server with NO state file (spawned outside
                # start_server, or its state was cleaned up): ADOPT it —
                # b11727+ accepts any model name, so it keeps serving;
                # replacing it would just race the port (live lesson
                # 2026-10-19: the replacement died on bind while the
                # healthy original carried on anyway).
                logger.info("adopting stateless llama-server on port %d", DEFAULT_PORT)
                return True
            if state.get("model_id") == entry["id"]:
                return True
            stop_server()  # deliberate model switch: replace it

        # A load of THIS model may already be in flight (spawned by another
        # thread/process; GPU loads take up to the full budget to bind the
        # port). Respawning on the same port would kill both — wait on it.
        if (state.get("model_id") == entry["id"] and state.get("pid")
                and time.time() - (state.get("started") or 0) < _load_budget(hw)):
            if _pid_is_our_server(int(state["pid"]), state.get("exe")):
                return True

        # Always prefer the Vulkan runtime when present: the hardware probe
        # is ADVISORY (a transient --list-devices failure once forced the
        # CPU build on a 16 GB GPU machine — the vulkan binary also runs
        # CPU-only when no device is present, so this is strictly safer).
        attempts: List[Tuple[str, str]] = []
        if find_server_exe("vulkan"):
            attempts.append(("vulkan", build_server_args(gguf, entry["id"], hw, tier, "vulkan")))
        if find_server_exe("cpu"):
            attempts.append(("cpu", build_server_args(gguf, entry["id"], hw, tier, "cpu")))
        if not attempts:
            logger.warning("no llama.cpp runtime extracted (run the setup step)")
            return False

        for variant, args in attempts:
            exe = find_server_exe(variant)
            pid = _spawn(exe, args)
            if pid is None:
                continue
            _server_pid = pid
            _write_state({"pid": pid, "port": DEFAULT_PORT, "variant": variant,
                          "exe": exe, "model_id": entry["id"],
                          "started": time.time()})
            logger.info("started llama-server (%s) pid %s for %s",
                        variant, pid, entry["id"])
            return True
        return False


def _load_budget(hw: Optional[HardwareInfo] = None) -> float:
    hw = hw or detect_hardware()
    return START_TIMEOUT_GPU if hw.vram_mb else START_TIMEOUT_CPU


def wait_until_ready(timeout: Optional[float] = None) -> bool:
    budget = timeout if timeout is not None else _load_budget()
    deadline = time.time() + budget
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(1.0)
    return is_running()


def _process_exe(pid: int) -> str:
    """Executable path of a pid ('' when unknown). Verified the hard way
    (2026-10-19, this machine): image-NAME matching alone is not enough —
    another llama.cpp app (ModelSwarm) runs llama-server.exe too, and a
    stale state pid must never redirect a taskkill /T /F into it."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {pid}).Path"],
            capture_output=True, text=True, timeout=15,
            creationflags=_CREATE_NO_WINDOW)
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def _pid_is_our_server(pid: int, exe: Optional[str] = None) -> bool:
    """Exact-match, image-verified liveness check (plus exe-PATH check when
    the caller knows which binary we spawned). The naive `str(pid) in
    tasklist-output` substring test matches 123 against 1234, and a bare
    pid says nothing about WHO owns it after a reboot recycled pids —
    taskkill /T /F on a wrong pid kills an innocent process tree."""
    if pid <= 0:
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq llama-server.exe",
             "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=_CREATE_NO_WINDOW)
        matched = False
        for line in (proc.stdout or "").splitlines():
            cols = [c.strip('"') for c in line.split('","')]
            if len(cols) >= 2 and cols[0].lower() == "llama-server.exe" \
                    and cols[1] == str(pid):
                matched = True
                break
        if not matched:
            return False
        if exe:
            live_exe = _process_exe(pid)
            # Failed probe -> keep the image-name verdict; a POSITIVE answer
            # must agree with the path we spawned.
            return not live_exe or os.path.normcase(live_exe) == os.path.normcase(exe)
        return True
    except Exception:
        return False


def stop_server() -> bool:
    """Terminate the server (the recorded pid covers a server started by
    another DeepFlux process, e.g. the installer step). The image-verified
    pid check means a recycled pid can never redirect the kill."""
    global _server_pid
    with _server_lock:
        state = _state()
        pid = _server_pid or state.get("pid")
        stopped = False
        if pid and _pid_is_our_server(int(pid), state.get("exe")):
            try:
                kill = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=20,
                    creationflags=_CREATE_NO_WINDOW)
                stopped = kill.returncode == 0
                if not stopped:
                    logger.warning("taskkill pid %s failed: %s", pid,
                                   (kill.stderr or b"").decode(errors="replace"))
            except Exception as exc:
                logger.debug("taskkill failed: %s", exc)
        try:
            os.remove(_state_path())
        except OSError:
            pass
        _server_pid = None
        return stopped


def reap_orphan() -> None:
    """Boot-time cleanup: a recorded server that is NOT healthy was left
    hung/crashed — kill it; a healthy one (e.g. started by the installer
    step, whose process then exited) is adopted as-is. A server still
    inside its model-load budget is LEFT ALONE (a GPU load takes up to
    START_TIMEOUT_GPU — killing "unhealthy" there would murder a perfectly
    good load; a second ensure_running thread racing the first was exactly
    that)."""
    with _server_lock:
        state = _state()
        pid = state.get("pid")
        if not pid:
            return
        if is_running():
            return
        started = state.get("started") or 0
        if time.time() - started < _load_budget():
            return  # mid-load, not orphaned
        if _pid_is_our_server(int(pid), state.get("exe")):
            logger.info("reaping unhealthy llama-server pid %s", pid)
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20,
                               creationflags=_CREATE_NO_WINDOW)
            except Exception:
                pass
        try:
            os.remove(_state_path())
        except OSError:
            pass


def ensure_running(config: "Any") -> bool:
    """Start the local server when the config asks for OUR local provider,
    a model is installed and the server is down (Jackett/FlareSolverr
    lazy-start contract — cheap when already up, never touches a custom
    base URL)."""
    try:
        if getattr(config.llm, "provider", "") != "local":
            return False
        base = (config.llm.base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        if base and base != DEFAULT_BASE_URL:
            return False
        entry = entry_by_id(config.llm.model)
        if entry is None or not is_installed(entry):
            installed = installed_models()
            if not installed:
                return False
            entry = installed[0]
            # Keep request model and served --alias in agreement: the client
            # sends config.llm.model, the server answers under entry's id.
            # In-memory only (verified live: b11727 ACCEPTS mismatched names,
            # so this is future-proofing, not a hot fix); the next config
            # save persists it.
            config.llm.model = entry["id"]
            config.llm.fast_model = entry["id"]
        # A healthy server already serving THIS model needs nothing; one
        # serving a different model is restarted by start_server below.
        if is_running() and _state().get("model_id") == entry["id"]:
            return True
        if not ensure_runtime():
            return False
        if not start_server(entry):
            return False
        return wait_until_ready()
    except Exception as exc:
        logger.debug("local model ensure_running failed: %s", exc)
        return False
