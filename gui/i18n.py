"""UI string translation — Simplified Chinese (zh-CN), phase 1 (app shell).

Homegrown catalog on purpose (2026-09-24 localization study): Qt's
QTranslator/.ts pipeline cannot reach the module-level constants a quarter
of this app's labels live in, and a plain dict keyed by the English source
string diffs and reviews far better for LLM-maintained translations.

Contract:
- ``tr("English source")`` returns the Chinese translation when the UI
  language is ``"zh"``, otherwise the source unchanged. A missing key
  falls back to the source — never blank, never a crash, so shipping a
  partially translated build is safe by construction.
- The language is set ONCE per process (``set_language`` from
  ``run_gui``, right after the config loads) and applies until restart —
  the same "applies next launch" convention as several other settings.
  Widgets already built are not retranslated.
- Keys are the exact source strings, ``&&`` mnemonics included, so
  wrapping a call site needs no key invention. If one English string
  ever needs two Chinese meanings, prefix the second catalog entry with
  a ``ctx|`` marker and pass ``tr(source, ctx="...")`` — none exist yet.
- Module-level constants must keep ENGLISH literals: ``gui/__init__``
  imports the whole package (via main_window) before any caller can set
  the language, so ``tr()`` at constant-definition time would freeze
  English forever. Call ``tr()`` where a string is RENDERED (menu item,
  hub entry), never where it is merely defined.
- Maintenance rule: every change that adds user-visible strings adds its
  zh entries in the same change; tests/test_i18n.py keeps the catalog
  sane (unique keys, non-empty values, no English leaking into zh).
"""
from __future__ import annotations

from gui.i18n_zh_cn import ZH_CN

SUPPORTED_LANGUAGES = ("en", "zh")

_ACTIVE = "en"


def set_language(code: str) -> None:
    """Set the process-wide UI language. Unknown codes fall back to English."""
    global _ACTIVE
    _ACTIVE = code if code in SUPPORTED_LANGUAGES else "en"


def active_language() -> str:
    return _ACTIVE


def tr(source: str) -> str:
    """Translate a user-visible string; English source is the key and the
    fallback. Cheap enough to call per-paint — but phase-1 call sites run
    at build time, matching the restart-to-apply contract above."""
    if _ACTIVE == "en":
        return source
    return ZH_CN.get(source, source)
