"""Reasoning-effort default (2026-11-21, owner: "taking too long to stop
thinking and give an answer"): the shipped default dropped high → low, old
saved "high" defaults migrate once via effort_defaults_gen, and the AI
settings page gained a level dropdown (dialog round-trip lives in
tests/test_gui_render.py next to the other APIKeysDialog tests — this file
deliberately stays Qt-free; a Qt-importing 'a' file rolled the a–c chunk
into a poisoned per-boot heap shape)."""
from __future__ import annotations

import json

from config import DeeptorrentConfig


def test_effort_default_is_low():
    assert DeeptorrentConfig().llm.reasoning_effort == "low"


def _write(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_old_saved_high_migrates_once(tmp_path):
    """"high" was the never-choosable default (no UI before 2026-11-21) and
    every save persisted it — remap to low once, then leave the user alone."""
    path = _write(tmp_path, {"llm": {"reasoning_effort": "high"}})
    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.reasoning_effort == "low"
    assert cfg.effort_defaults_gen == 2
    # After the stamp is saved, a deliberate later High choice persists.
    cfg.llm.reasoning_effort = "high"
    cfg.to_file(path)
    cfg2 = DeeptorrentConfig.from_file(path)
    assert cfg2.llm.reasoning_effort == "high"
    assert cfg2.effort_defaults_gen == 2


def test_gen2_config_keeps_its_value(tmp_path):
    """A config already stamped gen 2 is never re-migrated."""
    path = _write(tmp_path, {
        "llm": {"reasoning_effort": "high"},
        "effort_defaults_gen": 2,
    })
    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.reasoning_effort == "high"


def test_agent_set_effort_survives_migration(tmp_path):
    """A value that never matched the old default (agent-set via
    set_settings) is a deliberate choice — the migration must not touch it."""
    path = _write(tmp_path, {"llm": {"reasoning_effort": "medium"}})
    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.reasoning_effort == "medium"
