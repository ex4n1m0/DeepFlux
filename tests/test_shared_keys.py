"""Shared in-box key hygiene + provider/model migrations.

The shared keys ship obfuscated in the bundle and are injected into the live
config by ``from_file`` as a last-resort fallback. They must NEVER persist:
``to_file``/``sanitized_dict`` blank the slots they filled, and values saved
into config.json by pre-3.5.9 builds are scrubbed on load. These tests
monkeypatch the shared-key globals so they run identically on keyless
machines (no _embedded_keys.py) and build machines.

Since 7.9 (owner 2026-11-17) the shipped default agent is Venice AI's
uncensored DeepSeek V4.1 Flash — the venice migration tests pin who flips
(shared-key riders, keyless dummies) and who must NEVER flip (own keys,
env-declared DeepSeek users, explicit provider choices).
"""
import json
import os

import pytest

import config
from config import DeeptorrentConfig

SHARED = {
    "_SHARED_DEEPSEEK_API_KEY": "sk-test-shared-deepseek-key",
    "_SHARED_VENICE_API_KEY": "venice-test-shared-key",
    "_SHARED_PERPLEXITY_API_KEY": "pplx-test-shared-perplexity-key",
    "_SHARED_TMDB_API_KEY": "test-shared-tmdb-key",
    "_SHARED_OPENSUBTITLES_API_KEY": "test-shared-ost-key",
    "_SHARED_TPDB_API_KEY": "test-shared-tpdb-key",
    "_SHARED_STASHDB_API_KEY": "test-shared-stashdb-key",
    "_SHARED_OMDB_API_KEY": "test-shared-omdb-key",
    "_SHARED_FANARTTV_API_KEY": "test-shared-fanarttv-key",
}

ALL_SLOTS = (
    (("llm", "api_key"), "_SHARED_VENICE_API_KEY"),
    (("web_search", "api_key"), "_SHARED_PERPLEXITY_API_KEY"),
    (("iptv", "tmdb_api_key"), "_SHARED_TMDB_API_KEY"),
    (("iptv", "opensubtitles_api_key"), "_SHARED_OPENSUBTITLES_API_KEY"),
    (("iptv", "tpdb_api_key"), "_SHARED_TPDB_API_KEY"),
    (("iptv", "stashdb_api_key"), "_SHARED_STASHDB_API_KEY"),
    (("iptv", "omdb_api_key"), "_SHARED_OMDB_API_KEY"),
    (("iptv", "fanarttv_api_key"), "_SHARED_FANARTTV_API_KEY"),
)

# Env vars that must not interfere with the fallback chain in these tests.
SLOT_ENV_VARS = (
    "DEEPSEEK_API_KEY", "VENICE_API_KEY", "PERPLEXITY_API_KEY", "TMDB_API_KEY",
    "OPENSUBTITLES_API_KEY", "TPDB_API_KEY", "STASHDB_API_KEY",
    "OMDB_API_KEY", "FANARTTV_API_KEY",
)


@pytest.fixture(autouse=True)
def shared_keys(monkeypatch, tmp_path):
    for var in SLOT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for attr, value in SHARED.items():
        monkeypatch.setattr(config, attr, value)
    monkeypatch.setattr(config, "_SHARED_KEY_SLOTS", {})
    return tmp_path


def _saved(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_decode_shared_value_roundtrip_and_tamper():
    import importlib.util
    from pathlib import Path
    gen = importlib.util.spec_from_file_location(
        "_gen_embedded_keys",
        Path(__file__).resolve().parents[1] / "packaging" / "gen_embedded_keys.py")
    mod = importlib.util.module_from_spec(gen)
    gen.loader.exec_module(mod)
    salt_b64, blob_b64 = mod.encode_shared_value("sk-roundtrip-value-123")
    assert config._decode_shared_value((salt_b64, blob_b64)) == "sk-roundtrip-value-123"
    # A wrong salt must never yield the true key (fail closed to "" or garbage).
    tampered = ("QUFBQUFBQUFBQUFBQUFBQUE=", blob_b64)
    assert config._decode_shared_value(tampered) != "sk-roundtrip-value-123"
    assert config._decode_shared_value(None) == ""
    assert config._decode_shared_value(("", "")) == ""
    # Legacy plaintext form still passes through
    assert config._decode_shared_value("sk-plain") == "sk-plain"


def test_fresh_install_injects_but_never_persists(tmp_path):
    path = str(tmp_path / "config.json")
    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.provider == "venice"            # 7.9 shipped default
    assert cfg.llm.api_key == SHARED["_SHARED_VENICE_API_KEY"]
    assert cfg.web_search.api_key == SHARED["_SHARED_PERPLEXITY_API_KEY"]
    for (section, field), attr in ALL_SLOTS:
        assert getattr(getattr(cfg, section), field) == SHARED[attr]

    cfg.to_file(path)
    saved = _saved(path)
    for (section, field), _attr in ALL_SLOTS:
        assert saved[section][field] == "", f"shared key leaked into config.json: {section}.{field}"

    # Reload: slots re-inject in memory despite the blank file.
    reloaded = DeeptorrentConfig.from_file(path)
    assert reloaded.llm.api_key == SHARED["_SHARED_VENICE_API_KEY"]


def test_dummy_fossil_heals_with_shared_key(tmp_path, monkeypatch):
    """A config that fossilized on provider="dummy" (a keyless-era boot
    wrote it; the installer now PRESERVES configs, so it can survive an
    upgrade) must still get the shared key and heal back to the default
    provider (venice since 7.9) — fresh installs get the built-in key the
    first time they run (owner ask 2026-09-19). Keyless builds stay in
    dummy mode."""
    import config as config_module
    monkeypatch.setattr(config_module, "_SHARED_VENICE_API_KEY", "venice-heal-test")
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": "dummy", "api_key": ""}}, f)

    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.provider == "venice"            # healed, not stuck on dummy
    assert cfg.llm.api_key == "venice-heal-test"   # shared key injected

    cfg.to_file(path)
    saved = _saved(path)
    assert saved["llm"]["api_key"] == ""            # still never persisted
    assert saved["llm"]["provider"] == "venice"     # the heal itself persists


def test_dummy_fossil_stays_dummy_when_keyless(tmp_path, monkeypatch):
    """Keyless builds (source clones, macOS/Linux CI) must keep their
    demo-mode behavior — the heal AND the venice flip only fire when a key
    actually exists (no flip-flopping dummy↔venice on save cycles)."""
    import config as config_module
    monkeypatch.setattr(config_module, "_SHARED_DEEPSEEK_API_KEY", "")
    monkeypatch.setattr(config_module, "_SHARED_VENICE_API_KEY", "")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("VENICE_API_KEY", raising=False)
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": "dummy", "api_key": ""}}, f)

    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.provider == "dummy"
    assert cfg.llm.api_key == ""


def test_pre_359_leaked_keys_are_scrubbed_on_upgrade(tmp_path):
    path = str(tmp_path / "config.json")
    cfg = DeeptorrentConfig.from_file(path)  # inject shared keys
    cfg.to_file(path)
    data = _saved(path)
    # Simulate a config.json written by a pre-3.5.9 build: shared keys baked in.
    for (section, field), attr in ALL_SLOTS:
        data.setdefault(section, {})[field] = SHARED[attr]
    # ...including a leaked pre-7.9 DeepSeek agent key (the old default).
    data["llm"]["api_key"] = SHARED["_SHARED_DEEPSEEK_API_KEY"]
    data["llm"]["provider"] = "deepseek"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    upgraded = DeeptorrentConfig.from_file(path)
    assert upgraded.llm.provider == "venice"  # shared-DeepSeek rider migrated
    assert upgraded.llm.api_key == SHARED["_SHARED_VENICE_API_KEY"]  # re-injected
    upgraded.to_file(path)
    saved = _saved(path)
    for (section, field), _attr in ALL_SLOTS:
        assert saved[section][field] == "", f"leaked key not scrubbed: {section}.{field}"


def test_user_keys_win_and_persist(tmp_path):
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"api_key": "sk-user-own", "provider": "deepseek"},
                   "web_search": {"api_key": "pplx-user-own"}}, f)
    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.api_key == "sk-user-own"
    assert cfg.web_search.api_key == "pplx-user-own"
    cfg.to_file(path)
    saved = _saved(path)
    assert saved["llm"]["api_key"] == "sk-user-own"
    assert saved["web_search"]["api_key"] == "pplx-user-own"


def test_env_var_beats_shared_key(tmp_path):
    path = str(tmp_path / "config.json")
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    try:
        cfg = DeeptorrentConfig.from_file(path)
        # A machine-level DeepSeek env var is an explicit DeepSeek choice —
        # it must beat the venice default AND pin the provider.
        assert cfg.llm.provider == "deepseek"
        assert cfg.llm.api_key == "sk-from-env"
        cfg.to_file(path)
        # Env values are not shared keys; they persist as before.
        assert _saved(path)["llm"]["api_key"] == "sk-from-env"
    finally:
        del os.environ["DEEPSEEK_API_KEY"]


def test_sanitized_dict_noop_for_user_configs():
    from dataclasses import asdict
    from config import LLMConfig
    # A config built directly (no from_file injection) must round-trip unchanged.
    cfg = DeeptorrentConfig(llm=LLMConfig(api_key="sk-user-own"))
    assert cfg.sanitized_dict() == asdict(cfg)


def test_model_defaults_are_current():
    assert DeeptorrentConfig().llm.provider == "venice"
    assert DeeptorrentConfig().llm.model == ""      # "" = provider preset default
    assert DeeptorrentConfig().llm.fast_model == ""
    assert "deepseek-v4-1-flash-uncensored" in config.LLM_PROVIDER_PRESETS["venice"]["models"]
    assert config.LLM_PROVIDER_PRESETS["venice"]["base_url"] == "https://api.venice.ai/api/v1"
    assert "deepseek-flash" in config.LLM_PROVIDER_PRESETS["deepseek"]["models"]
    assert "deepseek/deepseek-v4.1-flash" in config.LLM_PROVIDER_PRESETS["openrouter"]["models"]


@pytest.mark.parametrize("provider,saved,expected", [
    ("deepseek", "deepseek-v4-flash", "deepseek-flash"),
    ("deepseek", "deepseek-v4-flash-vision-exp", "deepseek-flash"),
    ("deepseek", "deepseek-chat", "deepseek-flash"),
    ("deepseek", "deepseek-reasoner", "deepseek-flash"),
    ("deepseek", "deepseek-v4-pro", "deepseek-flash"),   # old preset default, retired
    ("deepseek", "some-custom-name", "some-custom-name"),  # unknown -> kept
    ("openrouter", "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4.1-flash"),
    ("openrouter", "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4.1-flash"),
    ("custom", "deepseek-chat", "deepseek-chat"),        # custom never remapped
    ("venice", "deepseek-flash", "deepseek-v4-1-flash-uncensored"),
    ("venice", "deepseek-chat", "deepseek-v4-1-flash-uncensored"),
    ("venice", "some-custom-name", "some-custom-name"),  # unknown -> kept
])
def test_model_alias_migration(tmp_path, provider, saved, expected):
    path = str(tmp_path / "config.json")
    # A key keeps explicit deepseek/openrouter choices out of the 7.9
    # venice flip (keyless shared-key riders legitimately DO flip).
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": provider, "model": saved,
                           "fast_model": saved, "api_key": "sk-user-own"}}, f)
    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.model == expected
    # fast_model follows the same remap when the result is valid where used
    # (a stale DIRECT-preset name on openrouter is cleared instead — below).
    if not (provider == "openrouter" and expected == "deepseek-flash"):
        assert cfg.llm.fast_model == expected


def test_openrouter_stale_direct_fast_model_cleared(tmp_path):
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro",
                           "fast_model": "deepseek-flash"}}, f)
    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.fast_model == ""


def test_bundle_walker_descends_into_tuple_constants():
    """The obfuscated (salt, blob) pairs are TUPLE constants in the pyc — the
    release-gate walker must yield strings inside tuples or it would miss a
    key hidden there (blind spot found while verifying the 3.5.9 build)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "_vb", Path(__file__).resolve().parents[1] / "packaging" / "verify_bundle_keys.py")
    vb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vb)
    code = compile("PAIR = ('sk-hidden-inside-a-tuple-constant-12345', 'benign')", "<t>", "exec")
    strings = list(vb._walk_code_strings(code))
    assert any("sk-hidden-inside-a-tuple-constant-12345" in s for s in strings)
    hit = vb._match_markers("<t>", " ".join(strings), [])
    assert hit and hit[0] == "REVIEW"


def test_unstamped_local_migrates_to_default(tmp_path, monkeypatch):
    """6.4.2 owner rule: configs whose provider was AUTO-switched to local
    by the pre-6.4.1 flows carry no local_chosen stamp — from_file must
    force them back onto the built-in default (venice since 7.9; this is
    how auto-updaters get the key back) and clear the loopback base URL."""
    import config as config_module
    monkeypatch.setattr(config_module, "_SHARED_VENICE_API_KEY", "venice-migrate-test")
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": "local",
                           "model": "qwen35-9b-uncensored-q4km",
                           "fast_model": "qwen35-9b-uncensored-q4km",
                           "base_url": "http://127.0.0.1:8199/v1",
                           "api_key": ""}}, f)

    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.provider == "venice"
    assert cfg.llm.model == "deepseek-v4-1-flash-uncensored"
    assert cfg.llm.fast_model == ""
    assert cfg.llm.base_url == ""
    assert cfg.llm.api_key == "venice-migrate-test"  # shared key injected

    cfg.to_file(path)
    saved = _saved(path)
    assert saved["llm"]["provider"] == "venice"


def test_shared_deepseek_rider_migrates_to_venice(tmp_path):
    """7.9 owner rule: an install riding the shared DeepSeek key (saved
    blank after the pre-scrub, or literally leaked pre-3.5.9) moves to the
    venice default with its model lineup; the saved direct-API names must
    not survive onto the venice endpoint."""
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": "deepseek",
                           "model": "deepseek-flash",
                           "fast_model": "deepseek-flash",
                           "api_key": SHARED["_SHARED_DEEPSEEK_API_KEY"]}}, f)

    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.provider == "venice"
    assert cfg.llm.model == "deepseek-v4-1-flash-uncensored"
    assert cfg.llm.fast_model == ""
    assert cfg.llm.api_key == SHARED["_SHARED_VENICE_API_KEY"]
    assert config.llm_key_is_shared()

    cfg.to_file(path)
    saved = _saved(path)
    assert saved["llm"]["provider"] == "venice"
    assert saved["llm"]["api_key"] == ""


def test_own_deepseek_key_never_migrates(tmp_path):
    """A user's own DeepSeek key is an explicit choice — the venice flip
    must leave the provider, model lineup and key untouched."""
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": "deepseek",
                           "model": "deepseek-flash",
                           "api_key": "sk-user-own-deepseek"}}, f)

    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.provider == "deepseek"
    assert cfg.llm.model == "deepseek-flash"
    assert cfg.llm.api_key == "sk-user-own-deepseek"


def test_deepseek_env_user_with_saved_choice_stays(tmp_path):
    """DEEPSEEK_API_KEY env + a saved deepseek provider: the env is an
    explicit DeepSeek declaration — no venice flip, env key injected."""
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": "deepseek", "model": "deepseek-flash"}}, f)
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    try:
        cfg = DeeptorrentConfig.from_file(path)
        assert cfg.llm.provider == "deepseek"
        assert cfg.llm.api_key == "sk-from-env"
    finally:
        del os.environ["DEEPSEEK_API_KEY"]


def test_empty_provider_heals_to_venice(tmp_path):
    """A hand-edited config carrying an explicitly EMPTY provider string
    gets the venice key AND heals the provider to venice — an empty
    provider with a key would crash client construction."""
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": "", "api_key": ""}}, f)
    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.provider == "venice"
    assert cfg.llm.api_key == SHARED["_SHARED_VENICE_API_KEY"]


def test_explicit_provider_choices_survive_venice_default(tmp_path):
    """openrouter/custom configs are explicit choices — the 7.9 flip never
    touches them (custom needs a base_url+model to even be valid)."""
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": "openrouter",
                           "model": "deepseek/deepseek-v4.1-flash",
                           "api_key": "or-user-key"}}, f)
    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.provider == "openrouter"
    assert cfg.llm.model == "deepseek/deepseek-v4.1-flash"
    assert cfg.llm.api_key == "or-user-key"


def test_stamped_local_choice_survives(tmp_path, monkeypatch):
    """A local provider the user explicitly saved through the 6.4.1+ GUI
    carries the local_chosen stamp — the migration must leave it alone."""
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": "local",
                           "model": "qwen35-9b-uncensored-q4km",
                           "local_chosen": True,
                           "base_url": "http://127.0.0.1:8199/v1",
                           "api_key": ""}}, f)

    cfg = DeeptorrentConfig.from_file(path)
    assert cfg.llm.provider == "local"
    assert cfg.llm.model == "qwen35-9b-uncensored-q4km"
    assert cfg.llm.local_chosen is True
