"""Shared in-box key hygiene + DeepSeek model migration (3.5.9).

The shared keys ship obfuscated in the bundle and are injected into the live
config by ``from_file`` as a last-resort fallback. They must NEVER persist:
``to_file``/``sanitized_dict`` blank the slots they filled, and values saved
into config.json by pre-3.5.9 builds are scrubbed on load. These tests
monkeypatch the shared-key globals so they run identically on keyless
machines (no _embedded_keys.py) and build machines.
"""
import json
import os

import pytest

import config
from config import DeeptorrentConfig

SHARED = {
    "_SHARED_DEEPSEEK_API_KEY": "sk-test-shared-deepseek-key",
    "_SHARED_PERPLEXITY_API_KEY": "pplx-test-shared-perplexity-key",
    "_SHARED_TMDB_API_KEY": "test-shared-tmdb-key",
    "_SHARED_OPENSUBTITLES_API_KEY": "test-shared-ost-key",
    "_SHARED_TPDB_API_KEY": "test-shared-tpdb-key",
    "_SHARED_STASHDB_API_KEY": "test-shared-stashdb-key",
    "_SHARED_OMDB_API_KEY": "test-shared-omdb-key",
    "_SHARED_FANARTTV_API_KEY": "test-shared-fanarttv-key",
}

ALL_SLOTS = (
    (("llm", "api_key"), "_SHARED_DEEPSEEK_API_KEY"),
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
    "DEEPSEEK_API_KEY", "PERPLEXITY_API_KEY", "TMDB_API_KEY",
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
    assert cfg.llm.api_key == SHARED["_SHARED_DEEPSEEK_API_KEY"]
    assert cfg.web_search.api_key == SHARED["_SHARED_PERPLEXITY_API_KEY"]
    for (section, field), attr in ALL_SLOTS:
        assert getattr(getattr(cfg, section), field) == SHARED[attr]

    cfg.to_file(path)
    saved = _saved(path)
    for (section, field), _attr in ALL_SLOTS:
        assert saved[section][field] == "", f"shared key leaked into config.json: {section}.{field}"

    # Reload: slots re-inject in memory despite the blank file.
    reloaded = DeeptorrentConfig.from_file(path)
    assert reloaded.llm.api_key == SHARED["_SHARED_DEEPSEEK_API_KEY"]


def test_pre_359_leaked_keys_are_scrubbed_on_upgrade(tmp_path):
    path = str(tmp_path / "config.json")
    cfg = DeeptorrentConfig.from_file(path)  # inject shared keys
    cfg.to_file(path)
    data = _saved(path)
    # Simulate a config.json written by a pre-3.5.9 build: shared keys baked in.
    for (section, field), attr in ALL_SLOTS:
        data.setdefault(section, {})[field] = SHARED[attr]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    upgraded = DeeptorrentConfig.from_file(path)
    assert upgraded.llm.api_key == SHARED["_SHARED_DEEPSEEK_API_KEY"]  # re-injected
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
    assert DeeptorrentConfig().llm.model == "deepseek-flash"
    assert DeeptorrentConfig().llm.fast_model == "deepseek-flash"
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
])
def test_model_alias_migration(tmp_path, provider, saved, expected):
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"provider": provider, "model": saved, "fast_model": saved}}, f)
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
