"""Agent loop limits: raised defaults (2026-11-17) + the one-time
limits_defaults_gen migration — pre-8.0 defaults were persisted into every
config.json by any save, so old saved defaults remap once and explicit
choices survive."""
from __future__ import annotations

import json

from config import DeeptorrentConfig


def test_limits_defaults_raised():
    cfg = DeeptorrentConfig()
    assert (cfg.llm.max_turns, cfg.llm.max_llm_calls,
            cfg.llm.max_tool_calls) == (80, 100, 200)


def _write(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_old_saved_defaults_migrate_once(tmp_path):
    path = _write(tmp_path, {"llm": {"max_turns": 40, "max_llm_calls": 50,
                                     "max_tool_calls": 100}})
    cfg = DeeptorrentConfig.from_file(path)
    assert (cfg.llm.max_turns, cfg.llm.max_llm_calls,
            cfg.llm.max_tool_calls) == (80, 100, 200)
    assert cfg.limits_defaults_gen == 2
    # After the stamp is saved, a deliberate later low choice persists.
    cfg.llm.max_turns = 30
    cfg.to_file(path)
    cfg2 = DeeptorrentConfig.from_file(path)
    assert cfg2.llm.max_turns == 30
    assert cfg2.llm.max_llm_calls == 100  # untouched by the second load
    assert cfg2.limits_defaults_gen == 2


def test_gen2_config_keeps_its_values(tmp_path):
    """A config already stamped gen 2 is never re-migrated."""
    path = _write(tmp_path, {
        "llm": {"max_turns": 40, "max_llm_calls": 25, "max_tool_calls": 100},
        "limits_defaults_gen": 2,
    })
    cfg = DeeptorrentConfig.from_file(path)
    assert (cfg.llm.max_turns, cfg.llm.max_llm_calls,
            cfg.llm.max_tool_calls) == (40, 25, 100)


def test_explicit_non_default_values_survive_migration(tmp_path):
    """Values that never matched the old defaults are user choices."""
    path = _write(tmp_path, {"llm": {"max_turns": 120, "max_llm_calls": 50,
                                     "max_tool_calls": 300}})
    cfg = DeeptorrentConfig.from_file(path)
    assert (cfg.llm.max_turns, cfg.llm.max_llm_calls,
            cfg.llm.max_tool_calls) == (120, 100, 300)
