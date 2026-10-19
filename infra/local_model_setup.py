"""The installer's local-model final step (``DeepFlux --setup-localmodel``).

Same shape as infra/flaresolverr.py::run_setup: idempotent, never elevates,
streams human progress lines (plus byte progress for the multi-GB
download), and leaves DeepFlux in a working state on every exit path.

Pipeline: extract the bundled llama.cpp runtimes -> detect hardware ->
recommend a model from the pinned ladder -> download it (resumable,
sha256-verified) unless already installed -> switch the LLM provider to
"local" when the user hasn't deliberately chosen a cloud setup -> start
the server and health-check it.

Provider-switch rule (owner direction: local runs AS THE DEFAULT, but a
deliberate cloud choice is never stomped): switch only when the RAW saved
config has no user-typed llm.api_key and its provider is missing/""/
"deepseek"/"dummy" — i.e. fresh installs and default-state machines. The
raw file (not from_file) is read because from_file injects the shared
DeepSeek key, making a default machine indistinguishable from a keyed one.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from typing import Any, Callable, Dict, Optional

from config import DeeptorrentConfig
from infra import local_model

logger = logging.getLogger(__name__)


def _raw_llm(config_path: str) -> Dict[str, Any]:
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        llm = data.get("llm") or {}
        return {"api_key": llm.get("api_key") or "", "provider": llm.get("provider")}
    except Exception:
        return {"api_key": "", "provider": None}


def should_default_to_local(config_path: str) -> bool:
    raw = _raw_llm(config_path)
    return (not raw["api_key"]
            and raw["provider"] in (None, "", "deepseek", "dummy"))


def run_setup(config_path: Optional[str] = None,
              on_progress: Optional[Callable[[str], None]] = None,
              on_bytes: Optional[Callable[[int, int], None]] = None,
              cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    def say(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass
        logger.info("localmodel-setup: %s", msg)

    config_path = config_path or DeeptorrentConfig.default_config_path()
    config = DeeptorrentConfig.from_file(config_path)
    result: Dict[str, Any] = {
        "ok": False, "error": None, "skipped": None, "downloaded": False,
        "already_installed": False, "provider_switched": False,
        "started": False, "model_id": None, "summary": "",
    }

    if sys.platform != "win32":
        result["error"] = "The bundled local-model setup is Windows-only."
        result["skipped"] = result["error"]
        return result

    # 1) llama.cpp runtimes (idempotent — both variants, ~142 MB on disk).
    if not local_model.ensure_runtime(on_line=say):
        result["error"] = "no llama.cpp runtime was bundled with this build"
        say(result["error"])
        return result

    # 2) Hardware + recommendation from the pinned ladder.
    hw = local_model.detect_hardware(refresh=True)
    say(f"Hardware: {hw.summary()}")
    entry, reason = local_model.recommend(hw)
    if entry is None:
        # Not an error — this machine just can't run a local model.
        result["ok"] = True
        result["skipped"] = reason
        result["summary"] = f"Skipped: {reason}"
        say(f"No local model recommended — {reason}")
        say("DeepFlux keeps using its cloud AI (add a key in File → API Keys).")
        return result
    result["model_id"] = entry["id"]
    say(f"Recommended for this PC: {reason}")

    # 3) Download (or reuse) the model.
    if local_model.is_installed(entry):
        result["already_installed"] = True
        say(f"{entry['label']} is already installed — reusing it.")
    else:
        size_gib = int(entry["size"]) / 1024 ** 3
        say(f"Downloading {entry['label']} ({size_gib:.1f} GiB) from "
            "huggingface.co — cancelling keeps the partial download and it "
            "resumes on the next run.")
        try:
            local_model.download_model(entry, on_progress=on_bytes, cancel=cancel)
        except RuntimeError as exc:
            result["error"] = str(exc)
            say(f"Download did not finish ({exc}).")
            if cancel is not None and cancel.is_set():
                say("Cancelled — the partial download resumes on the next run.")
            say("Retry: install again, or use File → API Keys → AI in the app.")
            return result
        result["downloaded"] = True
        say("Download verified (sha256 matches the pinned manifest).")

    # 4) Provider switch (fresh installs / default-state machines only).
    raw_provider = _raw_llm(config_path).get("provider")
    if should_default_to_local(config_path) or raw_provider == "local":
        switched = config.llm.provider != "local"
        config.llm.provider = "local"
        config.llm.base_url = local_model.DEFAULT_BASE_URL
        config.llm.model = entry["id"]
        config.llm.fast_model = entry["id"]
        config.llm.api_key = ""  # the local server needs no key
        result["provider_switched"] = switched
        say("Local AI set as the default agent (cloud keys still work — "
            "switch anytime in File → API Keys).")
    else:
        say("Keeping your configured cloud AI provider — the local model is "
            "installed and selectable in File → API Keys.")

    try:
        config.to_file(config_path)
    except Exception as exc:
        logger.warning("localmodel-setup config save failed: %s", exc)

    # 5) Start + health-check so the first launch is instantly usable.
    local_model.reap_orphan()
    if local_model.start_server(entry, hw=hw) and local_model.wait_until_ready():
        result["started"] = True
        say("Local AI server started — ready.")
    else:
        say("The model is installed but the server did not answer in time; "
            "DeepFlux will start it automatically on launch.")

    result["ok"] = True
    result["summary"] = (f"{'Downloaded' if result['downloaded'] else 'Reused'} "
                         f"{entry['label']} — local AI "
                         f"{'is now the default' if result['provider_switched'] else 'is available'}.")
    return result
