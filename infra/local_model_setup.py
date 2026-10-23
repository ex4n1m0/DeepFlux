"""The installer's local-model final step (``DeepFlux --setup-localmodel``).

Same shape as infra/flaresolverr.py::run_setup: idempotent, never elevates,
streams human progress lines (plus byte progress for the multi-GB
download), and leaves DeepFlux in a working state on every exit path.

Pipeline: extract the bundled llama.cpp runtimes -> detect hardware ->
recommend a model from the pinned ladder -> download it (resumable,
sha256-verified) unless already installed. The provider is NEVER changed:
the built-in DeepSeek key stays the default agent regardless (owner
decision 6.4.1, 2026-10-21 — downloading a local model is installation
only); users switch to local AI explicitly in File -> API Keys -> AI, and
the server then starts on demand.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Dict, Optional

from config import DeeptorrentConfig
from infra import local_model

logger = logging.getLogger(__name__)


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
    result: Dict[str, Any] = {
        "ok": False, "error": None, "skipped": None, "downloaded": False,
        "already_installed": False, "model_id": None, "summary": "",
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
        say("DeepFlux keeps its built-in DeepSeek AI as the default.")
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
            say("Retry: install again, or use Settings → AI & API Keys → "
                "AI Agent Key… in the app.")
            return result
        result["downloaded"] = True
        say("Download verified (sha256 matches the pinned manifest).")

    say("The model is installed. The built-in DeepSeek key REMAINS the "
        "default agent — switch to 'Local AI (built-in, uncensored)' in "
        "Settings → AI & API Keys → AI Agent Key… whenever you want to use "
        "it; the local server then starts automatically.")

    result["ok"] = True
    result["summary"] = (f"{'Downloaded' if result['downloaded'] else 'Reused'} "
                         f"{entry['label']} — installed and selectable in "
                         "Settings → AI & API Keys → AI Agent Key… "
                         "(DeepSeek stays the default).")
    return result
