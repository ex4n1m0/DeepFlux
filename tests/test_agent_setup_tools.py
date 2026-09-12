"""App-setup agent tools (2026-09-12): the agent can configure anything the
user could have typed into a dialog — IPTV sources, API keys (write-only),
general settings (whitelisted scalars, secrets masked), torrent search
sources. Everything persists through the redirected
default config path (conftest autouse fixture)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent.loop import SYSTEM_PROMPT
from agent.tools import (
    CONFIRMATION_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    ToolError,
    ToolRegistry,
    redact_tool_arguments,
)
from config import DeeptorrentConfig


@pytest.fixture
def tools():
    return ToolRegistry(MagicMock(), DeeptorrentConfig())


@pytest.fixture
def public_urls():
    """Keep URL validation offline: every validate_public_http_url call is a
    pass-through in these tests (private-target rejection has its own test
    against the real validator via an IP literal, which needs no DNS)."""
    with patch("agent.tools.validate_public_http_url", side_effect=lambda u: u):
        yield


class FakeBridge:
    def __init__(self):
        self.reloads = 0

    def request_reload(self):
        self.reloads += 1


def _persisted_sources(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["iptv"]["sources"]


# ---------------------------------------------------------------------------
# IPTV sources
# ---------------------------------------------------------------------------

def test_iptv_add_list_update_remove(tools, public_urls):
    with patch("agent.tools._peek_playlist", return_value=None):
        result = tools.call("iptv_add_source", {
            "url": "https://provider.example/playlist.m3u8", "name": "My IPTV"})
    assert result["success"], result
    source = result["source"]
    assert source["name"] == "My IPTV" and source["kind"] == "m3u_url"
    assert tools.config.iptv.sources[0].id == source["id"]
    # Persisted to (the redirected) config.json.
    saved = _persisted_sources(DeeptorrentConfig.default_config_path())
    assert [s["id"] for s in saved] == [source["id"]]

    listing = tools.call("iptv_list_sources", {})
    assert listing["count"] == 1
    assert listing["sources"][0]["url"] == "https://provider.example/playlist.m3u8"
    assert "password" not in json.dumps(listing)

    # Partial update by name: disable only — nothing else is touched.
    tools.config.iptv.sources[0].username = "keepme"
    updated = tools.call("iptv_update_source", {"source": "my iptv", "enabled": False})
    assert updated["success"], updated
    assert tools.config.iptv.sources[0].enabled is False
    assert tools.config.iptv.sources[0].username == "keepme"

    removed = tools.call("iptv_remove_source", {"source": source["id"][:8]})
    assert removed["success"], removed
    assert tools.config.iptv.sources == []


def test_iptv_add_requests_bridge_reload(tools, public_urls):
    bridge = FakeBridge()
    tools.set_iptv_bridge(bridge)
    with patch("agent.tools._peek_playlist", return_value=None):
        tools.call("iptv_add_source", {"url": "https://provider.example/pl.m3u"})
    assert bridge.reloads == 1
    tools.call("iptv_remove_source", {"source": "provider.example"})
    assert bridge.reloads == 2


def test_iptv_add_rejects_non_playlist_payload(tools, public_urls):
    with patch("agent.tools._peek_playlist", return_value="content is HTML/XML, not a playlist"):
        result = tools.call("iptv_add_source", {"url": "https://provider.example/epg.xml"})
    assert result["success"] is False
    assert "validate=false" in result["error"]
    assert tools.config.iptv.sources == []


def test_iptv_add_rejects_private_target(tools):
    # IP literal — no DNS needed for the validator's private-range check.
    with patch("agent.tools._peek_playlist", return_value=None):
        with pytest.raises(ToolError, match="Local and private"):
            tools.call("iptv_add_source", {"url": "http://127.0.0.1:8080/pl.m3u"})


def test_iptv_add_duplicate_url(tools, public_urls):
    with patch("agent.tools._peek_playlist", return_value=None):
        first = tools.call("iptv_add_source", {"url": "https://provider.example/pl.m3u"})
        second = tools.call("iptv_add_source", {"url": "https://provider.example/pl.m3u",
                                                "name": "dup"})
    assert first["success"]
    assert second["success"] is False and "already exists" in second["error"]
    assert len(tools.config.iptv.sources) == 1


def test_iptv_add_local_folder_requires_existing_path(tools):
    with pytest.raises(ToolError, match="does not exist"):
        tools.call("iptv_add_source", {
            "url": "C:/does/not/exist", "kind": "local_folder"})


def test_iptv_add_xtream_stores_credentials(tools, public_urls):
    result = tools.call("iptv_add_source", {
        "url": "https://xtream.example:8080", "kind": "xtream",
        "username": "bob", "password": "hunter2"})
    assert result["success"], result
    stored = tools.config.iptv.sources[0]
    assert stored.username == "bob" and stored.password == "hunter2"
    # Credentials never leak back through the listing.
    listing = json.dumps(tools.call("iptv_list_sources", {}))
    assert "hunter2" not in listing and "bob" not in listing
    # ...and the confirmation/log preview redacts the password.
    preview = redact_tool_arguments("iptv_add_source", {
        "url": "https://x/", "kind": "xtream", "username": "bob", "password": "hunter2"})
    assert preview["password"] == "<redacted>"


# ---------------------------------------------------------------------------
# API keys — write-only
# ---------------------------------------------------------------------------

def test_set_api_key_writes_and_reports_without_leaking(tools, public_urls):
    result = tools.call("set_api_key", {"slot": "tmdb", "value": "sk-secret-value"})
    assert result["success"], result
    assert tools.config.iptv.tmdb_api_key == "sk-secret-value"
    assert "sk-secret-value" not in json.dumps(result)

    listing = tools.call("list_api_keys", {})
    row = next(k for k in listing["keys"] if k["slot"] == "tmdb")
    assert row["configured"] is True
    assert "sk-secret-value" not in json.dumps(listing)

    # Values are redacted in the confirmation/log preview.
    preview = redact_tool_arguments("set_api_key", {"slot": "tmdb", "value": "sk-secret-value"})
    assert preview["value"] == "<redacted>"

    # Cleared with an empty value.
    tools.call("set_api_key", {"slot": "tmdb", "value": ""})
    assert tools.config.iptv.tmdb_api_key == ""
    listing = tools.call("list_api_keys", {})
    assert next(k for k in listing["keys"] if k["slot"] == "tmdb")["configured"] is False


def test_set_api_key_reloads_iptv_for_iptv_slots(tools, public_urls):
    bridge = FakeBridge()
    tools.set_iptv_bridge(bridge)
    tools.call("set_api_key", {"slot": "omdb", "value": "k"})
    assert bridge.reloads == 1


def test_set_api_key_unknown_slot(tools):
    with pytest.raises(ToolError, match="Unknown slot"):
        tools.call("set_api_key", {"slot": "nope", "value": "x"})


# ---------------------------------------------------------------------------
# General settings
# ---------------------------------------------------------------------------

def test_list_settings_masks_secrets(tools):
    tools.config.llm.api_key = "sk-live-secret"
    listing = tools.call("list_settings", {})
    blob = json.dumps(listing)
    assert "sk-live-secret" not in blob
    row = next(s for s in listing["settings"] if s["path"] == "llm.api_key")
    assert row["value"] == "<set>"
    # Forced-on internals are not offered at all.
    assert not any(s["path"] == "llm.stream" for s in listing["settings"])
    # A normal scalar is shown with its value.
    row = next(s for s in listing["settings"] if s["path"] == "iptv.vod_group_mode")
    assert row["value"] == "year"


def test_set_settings_roundtrip_and_coercion(tools):
    bridge = FakeBridge()
    tools.set_iptv_bridge(bridge)
    result = tools.call("set_settings", {"path": "iptv.epg_url", "value": "https://guide.example/t.xml"})
    assert result["success"], result
    assert tools.config.iptv.epg_url == "https://guide.example/t.xml"
    assert bridge.reloads == 1  # iptv.* settings apply live

    tools.call("set_settings", {"path": "download.max_concurrent", "value": "7"})
    assert tools.config.download.max_concurrent == 7

    tools.call("set_settings", {"path": "iptv.enable_epg", "value": "false"})
    assert tools.config.iptv.enable_epg is False

    persisted = json.load(open(DeeptorrentConfig.default_config_path(), encoding="utf-8"))
    assert persisted["download"]["max_concurrent"] == 7


def test_set_settings_enum_validation(tools):
    tools.call("set_settings", {"path": "iptv.vod_group_mode", "value": "category"})
    assert tools.config.iptv.vod_group_mode == "category"
    with pytest.raises(ToolError, match="must be one of"):
        tools.call("set_settings", {"path": "iptv.vod_group_mode", "value": "flat"})


def test_set_settings_rejects_secrets_and_unknown(tools):
    with pytest.raises(ToolError, match="set_api_key"):
        tools.call("set_settings", {"path": "llm.api_key", "value": "sk-x"})
    with pytest.raises(ToolError, match="set_api_key"):
        tools.call("set_settings", {"path": "iptv.opensubtitles_password", "value": "p"})
    with pytest.raises(ToolError, match="Unknown or non-settable"):
        tools.call("set_settings", {"path": "no.such.field", "value": "1"})
    with pytest.raises(ToolError, match="Unknown or non-settable"):
        tools.call("set_settings", {"path": "llm.stream", "value": True})
    with pytest.raises(ToolError, match="whole number"):
        tools.call("set_settings", {"path": "download.max_concurrent", "value": "lots"})


# ---------------------------------------------------------------------------
# Torrent sources
# ---------------------------------------------------------------------------

def test_torrent_source_add_list_remove(tools, public_urls):
    result = tools.call("add_torrent_source", {
        "name": "Example Index", "url": "https://example.is", "categories": ["Movies"]})
    assert result["success"], result
    assert result["source"]["id"] == "example-index"
    listing = tools.call("list_torrent_sources", {})
    assert listing["sources"][0]["name"] == "Example Index"

    dup = tools.call("add_torrent_source", {"name": "Other", "url": "https://example.is"})
    assert dup["success"] is False
    removed = tools.call("remove_torrent_source", {"source": "Example Index"})
    assert removed["success"], removed
    assert tools.config.sources.sources == []




def test_setup_tools_policy_classification():
    reads = {"iptv_list_sources", "list_api_keys", "list_settings", "list_torrent_sources"}
    confirms = {"iptv_add_source", "iptv_update_source", "iptv_remove_source",
                "set_api_key", "set_settings", "add_torrent_source",
                "remove_torrent_source"}
    assert reads <= READ_ONLY_TOOL_NAMES
    assert confirms <= CONFIRMATION_TOOL_NAMES


def test_context_routing_exposes_setup_tools(tools):
    names = tools.tool_names_for_context("can you find and add an iptv m3u playlist for me?")
    assert "iptv_add_source" in names and "iptv_list_sources" in names

    names = tools.tool_names_for_context("change the max concurrent downloads setting")
    assert "set_settings" in names and "list_settings" in names

    names = tools.tool_names_for_context("set my tmdb api key")
    assert "set_api_key" in names and "list_api_keys" in names

    # Cheap reads ride along everywhere so the model knows setup is possible.
    names = tools.tool_names_for_context("hello")
    assert {"list_settings", "list_api_keys", "iptv_list_sources"} <= names


def test_system_prompt_breadth_and_setup_section():
    # The old narrow identity must be gone (the only allowed mention is the
    # "You are NOT just a ..." guard itself).
    assert "LLM-assisted BitTorrent" not in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.count("BitTorrent download manager") == 1
    assert "all-in-one" in SYSTEM_PROMPT
    assert "Introductions" in SYSTEM_PROMPT
    assert "iptv_add_source" in SYSTEM_PROMPT
    assert "set_api_key" in SYSTEM_PROMPT
    assert "WRITE-ONLY" in SYSTEM_PROMPT
