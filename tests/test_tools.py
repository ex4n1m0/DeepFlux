"""Unit tests for the agent tool-calling layer."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent.tools import ToolError, ToolRegistry
from config import DeeptorrentConfig


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.list_torrents.return_value = [
        {
            "info_hash": "a" * 40,
            "name": "Test Torrent",
            "category": "Movies",
            "state": "downloading",
            "progress": 0.25,
            "download_rate": 1024,
            "upload_rate": 0,
            "num_seeds": 2,
            "num_peers": 5,
            "total_size": 1_000_000_000,
        }
    ]
    engine.get_torrent_status.return_value = {
        "info_hash": "a" * 40,
        "name": "Test Torrent",
        "files": [
            {"file_id": 0, "path": "video.mkv", "size": 1_000_000_000, "priority": 4}
        ],
        "category": "Movies",
    }
    engine.get_swarm_stats.return_value = {
        "info_hash": "a" * 40,
        "name": "Test Torrent",
        "state": "downloading",
        "progress": 0.0,
        "download_rate": 0,
        "upload_rate": 0,
        "num_seeds": 0,
        "num_peers": 0,
        "num_complete": 0,
        "num_incomplete": 0,
        "list_peers": 0,
        "list_seeds": 0,
        "connect_candidates": 0,
        "dht_nodes": 0,
        "trackers": [],
        "peers": [],
        "piece_availability_histogram": {"bins": [], "max": 0, "mean": 0.0, "zeros": 0},
        "error": None,
    }
    engine.add_magnet.return_value = "b" * 40
    engine.add_torrent_file.return_value = "c" * 40
    engine.pause.return_value = True
    engine.resume.return_value = True
    engine.remove.return_value = True
    engine.set_file_priority.return_value = True
    engine.add_tracker.return_value = True
    return engine


@pytest.fixture
def tools(mock_engine):
    config = DeeptorrentConfig()
    return ToolRegistry(mock_engine, config)


def test_list_tools(tools):
    names = {t["function"]["name"] for t in tools.list_tools()}
    expected = {
        "add_magnet",
        "add_torrent_file",
        "pause_torrent",
        "resume_torrent",
        "remove_torrent",
        "list_torrents",
        "get_torrent_status",
        "set_file_priority",
        "add_tracker",
        "get_swarm_stats",
        "diagnose_swarm",
        "refresh_tracker_list",
        "find_alt_trackers",
        "search_indexers",
        "find_alt_release",
        "propose_rename_and_category",
    }
    assert expected.issubset(names)


def test_context_tool_selection_limits_unrelated_domains(tools):
    default_names = tools.tool_names_for_context("find an Ubuntu torrent")
    assert "search_indexers" in default_names
    assert "browser_fill" not in default_names
    assert "irc_send_message" not in default_names

    browser_names = tools.tool_names_for_context("open the browser and log in")
    assert "browser_fill" in browser_names
    assert "search_indexers" in browser_names

    rss_names = tools.tool_names_for_context("show my RSS feed subscriptions")
    assert "get_rss_feed_items" in rss_names
    assert "download_from_feed" in rss_names


def test_capability_question_exposes_every_tool(tools):
    names = tools.tool_names_for_context("what can you do?")
    assert names == {tool["function"]["name"] for tool in tools.list_tools()}


def test_add_magnet(tools, mock_engine):
    result = tools.call("add_magnet", {"uri": "magnet:?xt=urn:btih:" + "d" * 40, "save_path": "/tmp", "category": "Movies"})
    assert result["success"] is True
    assert result["info_hash"] == "b" * 40
    mock_engine.add_magnet.assert_called_once()


def test_add_magnet_invalid(tools):
    with pytest.raises(Exception):
        tools.call("add_magnet", {"uri": "not a magnet", "save_path": "/tmp"})


def test_list_torrents(tools, mock_engine):
    result = tools.call("list_torrents", {})
    assert len(result["torrents"]) == 1
    assert result["torrents"][0]["name"] == "Test Torrent"
    assert result["success"] is True


def test_tool_schema_enforces_constraints(tools, mock_engine):
    with pytest.raises(ToolError, match="at most 7"):
        tools.call("set_file_priority", {"info_hash": "a" * 40, "file_id": 0, "level": 8})
    with pytest.raises(ToolError, match="must be an array"):
        tools.call("download_from_feed", {"feed_url": "https://example.com/feed", "item_ids": "one"})
    with pytest.raises(ToolError, match="Provide one of"):
        tools.call("web_fetch", {})
    with pytest.raises(ToolError, match="must be one of"):
        tools.call("list_downloads", {"status": "unknown"})
    mock_engine.set_file_priority.assert_not_called()


def test_list_torrents_paginates(tools, mock_engine):
    mock_engine.list_torrents.return_value = [
        {"info_hash": f"{index:040x}", "name": f"Torrent {index}"}
        for index in range(120)
    ]

    result = tools.call("list_torrents", {"offset": 50, "limit": 25})

    assert result["count"] == 25
    assert result["total"] == 120
    assert result["has_more"] is True
    assert result["torrents"][0]["name"] == "Torrent 50"


def test_torrent_status_paginates_files(tools, mock_engine):
    mock_engine.get_torrent_status.return_value = {
        "info_hash": "a" * 40,
        "files": [{"file_id": index, "path": f"file-{index}"} for index in range(250)],
    }

    result = tools.call("get_torrent_status", {
        "info_hash": "a" * 40, "file_offset": 100, "file_limit": 50,
    })

    assert len(result["status"]["files"]) == 50
    assert result["status"]["files"][0]["file_id"] == 100
    assert result["status"]["file_page"]["total"] == 250
    assert result["status"]["file_page"]["has_more"] is True


def test_organization_analyze_and_apply(mock_engine, tmp_path):
    config = DeeptorrentConfig()
    config.default_save_path = str(tmp_path)
    mock_engine.get_torrent_status.return_value = {
        "info_hash": "a" * 40, "name": "Show S01E02", "progress": 1.0,
        "category": "Other", "save_path": str(tmp_path),
        "files": [{"file_id": 0, "path": "episode.mkv", "size": 100, "priority": 4}],
    }
    destination = str(tmp_path / "TV")
    mock_engine.organize_torrent.return_value = {
        "success": True, "info_hash": "a" * 40, "category": "TV", "destination": destination,
    }
    tools = ToolRegistry(mock_engine, config)

    proposal = tools.call("analyze_organization", {"info_hash": "a" * 40})
    assert proposal["suggested_category"] == "TV"
    assert proposal["destination"] == destination

    result = tools.call("apply_organization_plan", {
        "info_hash": "a" * 40, "category": "TV", "destination": destination,
        "file_renames": [{"file_id": 0, "new_path": "Show S01E02.mkv"}],
    })
    assert result["success"] is True
    mock_engine.organize_torrent.assert_called_once_with(
        "a" * 40, destination, "TV", [{"file_id": 0, "new_path": "Show S01E02.mkv"}],
    )


def test_organization_rejects_destination_outside_save_path(mock_engine, tmp_path):
    config = DeeptorrentConfig()
    config.default_save_path = str(tmp_path / "downloads")
    tools = ToolRegistry(mock_engine, config)

    with pytest.raises(ToolError, match="inside the default save path"):
        tools.call("apply_organization_plan", {
            "info_hash": "a" * 40, "category": "Movies",
            "destination": str(tmp_path / "elsewhere"),
        })
    mock_engine.organize_torrent.assert_not_called()


def test_organization_rename_schema_rejects_unknown_fields(mock_engine, tmp_path):
    config = DeeptorrentConfig()
    config.default_save_path = str(tmp_path)
    tools = ToolRegistry(mock_engine, config)

    with pytest.raises(ToolError, match="Unknown argument"):
        tools.call("apply_organization_plan", {
            "info_hash": "a" * 40, "category": "Movies",
            "destination": str(tmp_path / "Movies"),
            "file_renames": [{"file_id": 0, "new_path": "movie.mkv", "extra": True}],
        })


def test_diagnose_swarm_stalled(tools):
    result = tools.call("diagnose_swarm", {"info_hash": "a" * 40})
    assert result["health"] == "dead"
    assert "no trackers" in result["cause"].lower()
    assert "summary" in result


def test_diagnose_swarm_healthy(tools, mock_engine):
    mock_engine.get_swarm_stats.return_value = {
        "info_hash": "a" * 40,
        "name": "Test Torrent",
        "state": "downloading",
        "progress": 0.5,
        "download_rate": 1024,
        "upload_rate": 0,
        "num_seeds": 10,
        "num_peers": 20,
        "num_complete": 0,
        "num_incomplete": 0,
        "list_peers": 0,
        "list_seeds": 0,
        "connect_candidates": 0,
        "dht_nodes": 100,
        "trackers": [{"url": "http://tracker.example.com"}],
        "peers": [{"down_speed": 1024, "progress": 0.5}],
        "piece_availability_histogram": {"bins": [0] * 10, "max": 5, "mean": 2.0, "zeros": 0},
        "error": None,
    }
    result = tools.call("diagnose_swarm", {"info_hash": "a" * 40})
    assert result["health"] == "healthy"


@patch("agent.tools.WebSearchClient.fetch_tracker_lists")
@patch("agent.tools.WebSearchClient.search")
@patch("agent.tools.TorznabClient.search")
def test_discovery_tools_return_structured_json(mock_torznab, mock_web_search, mock_trackers, mock_engine):
    config = DeeptorrentConfig()
    # Set an indexer API key so search_indexers uses the Torznab client
    # (which is mocked) instead of the web-search fallback.
    config.indexer.api_key = "test-key"
    # No configured sources -> the all-in-one "all" endpoint is used.
    config.sources.sources = []
    tools = ToolRegistry(mock_engine, config)

    mock_trackers.return_value = {"success": True, "count": 10, "trackers": ["udp://tracker1", "udp://tracker2"], "errors": []}
    result = tools.call("refresh_tracker_list", {})
    assert result["success"] is True
    assert isinstance(result["trackers"], list)

    mock_web_search.return_value = {"provider": "duckduckgo", "query": "x", "results": [{"title": "Tracker", "url": "http://example.com"}]}
    result = tools.call("find_alt_trackers", {"torrent_name": "Ubuntu"})
    assert "results" in result

    mock_torznab.return_value = {"success": True, "count": 2, "results": [{"name": "Ubuntu.iso", "size": 0, "seeders": 10, "leechers": 5, "indexer": "Jackett"}]}
    result = tools.call("search_indexers", {"query": "Ubuntu"})
    assert result["success"] is True
    assert len(result["results"]) == 1
    assert result["results"][0]["seeders"] == 10

    result = tools.call("find_alt_release", {"torrent_name": "Ubuntu"})
    assert "results" in result


@patch("agent.tools.TorznabClient.search_indexer")
def test_search_indexers_prefers_private_with_enough_seeds(mock_search_indexer, mock_engine):
    """Private indexers are queried first; public indexers are skipped when private has enough seeds."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.min_seeders = 10
    config.sources.sources = [
        SourceConfig(id="iptorrents", name="IPTorrents", url="https://iptorrents.com/", type="private"),
        SourceConfig(id="nyaasi", name="Nyaa.si", url="https://nyaa.si/", type="public"),
    ]
    tools = ToolRegistry(mock_engine, config)

    def fake_search(indexer_id, query, category=""):
        if indexer_id == "iptorrents":
            return {"success": True, "results": [
                {"name": "Ubuntu private release A", "seeders": 25},
                {"name": "Ubuntu private release B", "seeders": 5},
            ]}
        return {"success": True, "results": [{"name": "Ubuntu public release", "seeders": 500}]}

    mock_search_indexer.side_effect = fake_search
    result = tools.call("search_indexers", {"query": "Ubuntu"})
    # Best private (25) >= min_seeders, so the public indexer was never queried.
    assert all(r["source_type"] == "private" for r in result["results"])
    assert [r["name"] for r in result["results"]] == ["Ubuntu private release A", "Ubuntu private release B"]
    assert all(c.args[0] == "iptorrents" for c in mock_search_indexer.call_args_list)


@patch("agent.tools.TorznabClient.search_indexer")
def test_search_indexers_keeps_public_when_private_weak(mock_search_indexer, mock_engine):
    """Below the seed threshold, public indexers are searched too and results are merged."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.min_seeders = 10
    config.sources.sources = [
        SourceConfig(id="iptorrents", name="IPTorrents", url="https://iptorrents.com/", type="private"),
        SourceConfig(id="nyaasi", name="Nyaa.si", url="https://nyaa.si/", type="public"),
    ]
    tools = ToolRegistry(mock_engine, config)

    def fake_search(indexer_id, query, category=""):
        if indexer_id == "iptorrents":
            return {"success": True, "results": [{"name": "Ubuntu private release", "seeders": 3}]}
        return {"success": True, "results": [{"name": "Ubuntu public release", "seeders": 500}]}

    mock_search_indexer.side_effect = fake_search
    result = tools.call("search_indexers", {"query": "Ubuntu"})
    assert len(result["results"]) == 2
    types = {r["source_type"] for r in result["results"]}
    assert types == {"private", "public"}
    # A vastly better-seeded public result outranks a weak private one.
    assert result["results"][0]["source_type"] == "public"


@patch("agent.tools.TorznabClient.search_indexer")
def test_search_indexers_maps_category_names(mock_search_indexer, mock_engine):
    """App category names are translated to Torznab category IDs."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="iptorrents", name="IPTorrents", url="https://iptorrents.com/", type="private"),
    ]
    tools = ToolRegistry(mock_engine, config)

    mock_search_indexer.return_value = {"success": True, "results": [{"name": "Ubuntu Movie", "seeders": 50}]}
    tools.call("search_indexers", {"query": "Ubuntu", "category": "Movies"})
    assert mock_search_indexer.call_args.args[2] == "2000"


@patch("agent.tools.TorznabClient.search_indexer")
def test_search_indexers_cache(mock_search_indexer, mock_engine):
    """A repeated identical search is served from the cache without new calls."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="iptorrents", name="IPTorrents", url="https://iptorrents.com/", type="private"),
    ]
    tools = ToolRegistry(mock_engine, config)

    mock_search_indexer.return_value = {"success": True, "results": [{"name": "Ubuntu", "seeders": 50}]}
    first = tools.call("search_indexers", {"query": "Ubuntu"})
    second = tools.call("search_indexers", {"query": "Ubuntu"})
    assert first == second
    assert mock_search_indexer.call_count == 1


def test_web_fetch_extracts_magnets_from_html(mock_engine):
    """Magnet links in href attributes survive HTML-to-text extraction."""
    config = DeeptorrentConfig()
    tools = ToolRegistry(mock_engine, config)

    html = (
        '<html><body><a href="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Ubuntu">'
        "Download</a><p>Some release page</p></body></html>"
    )
    fake_resp = MagicMock()
    fake_resp.text = html
    fake_resp.status_code = 200
    fake_resp.headers = {"content-type": "text/html"}
    fake_resp.raise_for_status = lambda: None

    with patch("agent.tools.requests.get", return_value=fake_resp):
        result = tools.call("web_fetch", {"url": "http://example.com/release"})
    assert result["success"] is True
    assert result["_trust"] == "untrusted_external_content"
    assert result["magnets"] == ["magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Ubuntu"]
    assert "magnet:" not in result["content"]  # tags stripped, but magnets field preserved


def test_web_fetch_blocks_private_network_targets(tools):
    with patch("agent.tools.requests.get") as mock_get:
        result = tools.call("web_fetch", {"url": "http://127.0.0.1/private"})

    assert result["success"] is False
    assert "private network" in result["error"]
    mock_get.assert_not_called()


def test_web_fetch_blocks_redirect_to_private_network(tools):
    response = MagicMock()
    response.status_code = 302
    response.headers = {"Location": "http://169.254.169.254/latest/meta-data"}
    with patch("agent.tools.socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("93.184.216.34", 80)),
    ]), patch("agent.tools.requests.get", return_value=response) as mock_get:
        result = tools.call("web_fetch", {"url": "http://example.com/start"})

    assert result["success"] is False
    assert "private network" in result["error"]
    assert mock_get.call_count == 1


def test_tool_logging_redacts_sensitive_values(mock_engine, caplog):
    bridge = MagicMock()
    bridge.call.return_value = {"success": True, "password": "returned-secret"}
    tools = ToolRegistry(mock_engine, DeeptorrentConfig(), browser_bridge=bridge)

    with caplog.at_level("INFO", logger="agent.tools"):
        result = tools.call("browser_fill", {
            "selector": "#password", "value": "typed-secret", "submit": True,
        })

    assert "typed-secret" not in caplog.text
    assert result["password"] == "returned-secret"


def test_query_variants_strip_quality_and_year(mock_engine):
    config = DeeptorrentConfig()
    tools = ToolRegistry(mock_engine, config)
    variants = tools._query_variants("My Movie 2024 1080p x265")
    assert variants[0] == "My Movie 2024 1080p x265"
    assert "My Movie 2024" in variants
    assert "My Movie" in variants


@patch("agent.tools.WebSearchClient.search")
def test_search_indexers_private_tier_before_public(mock_web_search, mock_engine):
    """Without Jackett, private sources are searched first; public is skipped if they deliver."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = ""  # force web-search path
    config.sources.min_results = 2
    config.sources.sources = [
        SourceConfig(id="iptorrents", name="IPTorrents", url="https://iptorrents.com/", type="private"),
        SourceConfig(id="nyaasi", name="Nyaa.si", url="https://nyaa.si/", type="public"),
    ]
    tools = ToolRegistry(mock_engine, config)

    def fake_search(query, limit=10, llm=True):
        return {"results": [{"title": f"Hit on {query}", "url": "http://example.com"}] * 3}

    mock_web_search.side_effect = fake_search
    result = tools.call("search_indexers", {"query": "Ubuntu"})
    assert result["tier"] == "private"
    # Public source was never queried.
    assert all("nyaa.si" not in c.args[0] for c in mock_web_search.call_args_list)


@patch("agent.tools.WebSearchClient.search")
def test_search_indexers_stops_after_popular_batch(mock_web_search, mock_engine):
    """Once min_results is reached, lower-popularity sources are not queried."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = ""
    config.sources.search_batch_size = 1
    config.sources.min_results = 1
    config.sources.sources = [
        SourceConfig(id="popular", name="Popular", url="https://popular.example/", type="public", popularity=100),
        SourceConfig(id="niche", name="Niche", url="https://niche.example/", type="public", popularity=1),
    ]
    tools = ToolRegistry(mock_engine, config)

    mock_web_search.return_value = {"results": [{"title": "Hit", "url": "http://example.com"}]}
    result = tools.call("search_indexers", {"query": "Ubuntu"})
    assert result["count"] == 1
    # Only the popular source was queried; the niche one was skipped.
    queried = [c.args[0] for c in mock_web_search.call_args_list]
    assert any("popular.example" in q for q in queried)
    assert not any("niche.example" in q for q in queried)


@patch("agent.tools.TorznabClient.search_indexer")
def test_search_indexers_quick_pass_limits_public_tier(mock_search_indexer, mock_engine):
    """Default quick pass searches only the top public indexer; deep sweeps all."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.min_seeders = 1000  # never "good enough" — always reach public tier
    config.sources.min_results = 1000  # never stop early within a sweep
    config.sources.sources = [
        SourceConfig(id="popular", name="Popular", url="https://popular.example/", type="public", popularity=100),
        SourceConfig(id="niche", name="Niche", url="https://niche.example/", type="public", popularity=1),
    ]
    tools = ToolRegistry(mock_engine, config)

    mock_search_indexer.return_value = {"success": True, "results": [{"name": "Ubuntu", "seeders": 5}]}
    result = tools.call("search_indexers", {"query": "Ubuntu"})
    assert result["scope"] == "quick"
    assert result["deep_available"] is True
    assert {c.args[0] for c in mock_search_indexer.call_args_list} == {"popular"}

    mock_search_indexer.reset_mock()
    result = tools.call("search_indexers", {"query": "Ubuntu", "deep": True})
    assert result["scope"] == "deep"
    assert "deep_available" not in result
    assert {c.args[0] for c in mock_search_indexer.call_args_list} == {"popular", "niche"}


@patch("agent.tools.TorznabClient.search_indexer")
def test_search_indexers_quick_escalates_when_empty(mock_search_indexer, mock_engine):
    """A quick pass with zero hits automatically sweeps the remaining sources."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="popular", name="Popular", url="https://popular.example/", type="public", popularity=100),
        SourceConfig(id="niche", name="Niche", url="https://niche.example/", type="public", popularity=1),
    ]
    tools = ToolRegistry(mock_engine, config)

    def fake_search(indexer_id, query, category=""):
        if indexer_id == "popular":
            return {"success": True, "results": []}
        return {"success": True, "results": [{"name": "Ubuntu niche", "seeders": 50}]}

    mock_search_indexer.side_effect = fake_search
    result = tools.call("search_indexers", {"query": "Ubuntu"})
    assert result["scope"] == "deep"
    assert result["count"] == 1
    assert {c.args[0] for c in mock_search_indexer.call_args_list} == {"popular", "niche"}


@patch("agent.tools.TorznabClient.search_indexer")
def test_search_indexers_surfaces_indexer_errors(mock_search_indexer, mock_engine):
    """A timed-out indexer is reported in `errors` — not silently treated as zero hits."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.min_seeders = 10
    config.sources.sources = [
        SourceConfig(id="deadtracker", name="DeadTracker", url="https://dead.example/", type="private"),
        SourceConfig(id="iptorrents", name="IPTorrents", url="https://iptorrents.com/", type="private"),
    ]
    tools = ToolRegistry(mock_engine, config)

    def fake_search(indexer_id, query, category=""):
        if indexer_id == "deadtracker":
            return {"success": False, "results": [], "error": "Read timed out"}
        return {"success": True, "results": [{"name": "Ubuntu private release", "seeders": 25}]}

    mock_search_indexer.side_effect = fake_search
    result = tools.call("search_indexers", {"query": "Ubuntu"})
    assert result["success"] is True
    assert [r["name"] for r in result["results"]] == ["Ubuntu private release"]
    assert any("DeadTracker" in e and "Read timed out" in e for e in result["errors"])
    assert "results may be incomplete" in result["note"]


@patch("agent.tools.WebSearchClient.search")
@patch("agent.tools.TorznabClient.search_indexer")
def test_search_indexers_failed_indexer_not_retried_across_variants(mock_search_indexer, mock_web_search, mock_engine):
    """An indexer that errors is skipped for query variants AND the auto deep sweep;
    total failure is surfaced as an explicit error instead of falling back to web search."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="deadtracker", name="DeadTracker", url="https://dead.example/", type="private"),
    ]
    tools = ToolRegistry(mock_engine, config)

    mock_search_indexer.return_value = {"success": False, "results": [], "error": "Read timed out"}
    # 3 query variants ("My Movie 2024 1080p" → "My Movie 2024" → "My Movie") would
    # normally re-query the indexer each time plus the deep sweep.
    result = tools.call("search_indexers", {"query": "My Movie 2024 1080p"})
    assert result["success"] is False
    assert "error" in result
    assert any("DeadTracker" in e for e in result["errors"])
    assert "not a lack of results" in result["note"]
    assert mock_search_indexer.call_count == 1
    mock_web_search.assert_not_called()

    # Failures are not cached — a retry queries the indexer again.
    tools.call("search_indexers", {"query": "My Movie 2024 1080p"})
    assert mock_search_indexer.call_count == 2


@patch("agent.tools.TorznabClient.search_indexer")
def test_search_indexers_pages_large_result_sets(mock_search_indexer, mock_engine):
    """Large Jackett result sets are cached in full but served to the agent in pages."""
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="iptorrents", name="IPTorrents", url="https://iptorrents.com/", type="private"),
    ]
    tools = ToolRegistry(mock_engine, config)

    mock_search_indexer.return_value = {
        "success": True,
        "results": [{"name": f"Ubuntu release {i:03d}", "seeders": 50 - i % 10} for i in range(100)],
    }

    first = tools.call("search_indexers", {"query": "Ubuntu"})
    assert first["count"] == 40
    assert first["total_found"] == 100
    assert first["has_more"] is True
    assert len(first["results"]) == 40

    second = tools.call("search_indexers", {"query": "Ubuntu", "offset": 40})
    assert second["count"] == 40
    assert second["offset"] == 40
    assert second["has_more"] is True

    third = tools.call("search_indexers", {"query": "Ubuntu", "offset": 80})
    assert third["count"] == 20
    assert third["has_more"] is False

    # Pages are distinct slices of one ranked list — no re-query, no overlap.
    names = [r["name"] for r in first["results"] + second["results"] + third["results"]]
    assert len(names) == len(set(names)) == 100
    assert mock_search_indexer.call_count == 1


def _brave_response(results):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = {"web": {"results": results}}
    return resp


def _perplexity_response(answer="summary", citations=None):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = {
        "choices": [{"message": {"content": answer}}],
        "citations": citations or [],
    }
    return resp


@patch("agent.tools.requests.post")
@patch("agent.tools.requests.get")
@patch("agent.tools.DDGS")
def test_web_search_merges_all_providers(mock_ddgs, mock_get, mock_post, mock_engine):
    """All configured providers run in parallel; results merge deduped by URL."""
    config = DeeptorrentConfig()
    config.web_search.brave_api_key = "BSA-test"
    config.web_search.api_key = "pplx-test"
    tools = ToolRegistry(mock_engine, config)
    tools._web_search._rate_limit = 0  # no throttle sleeps in tests

    mock_ddgs.return_value.text.return_value = [
        {"title": "Ubuntu", "href": "http://example.com", "body": "OS"},
        {"title": "DDG only", "href": "http://ddg.com", "body": "ddg"},
    ]
    mock_get.return_value = _brave_response([
        {"title": "Dup", "url": "http://example.com", "description": "dup"},  # deduped
        {"title": "Brave only", "url": "http://brave.com", "description": "brave"},
    ])
    mock_post.return_value = _perplexity_response(answer="A summary", citations=["http://pplx.com"])

    result = tools._web_search.search("ubuntu")

    assert result["provider"] == "duckduckgo+brave+perplexity"
    assert result["providers"] == ["duckduckgo", "brave", "perplexity"]
    urls = [r["url"] for r in result["results"]]
    assert urls == ["http://example.com", "http://ddg.com", "http://brave.com", "http://pplx.com"]
    assert result["results"][0]["snippet"] == "OS"
    assert result["answer"] == "A summary"
    mock_get.assert_called_once()   # Brave queried alongside DDG
    mock_post.assert_called_once()  # Perplexity queried too


@patch("agent.tools.requests.post")
@patch("agent.tools.requests.get")
@patch("agent.tools.DDGS")
def test_web_search_magnets_from_answer(mock_ddgs, mock_get, mock_post, mock_engine):
    """Magnet links inside the Perplexity answer are surfaced alongside results."""
    config = DeeptorrentConfig()
    config.web_search.api_key = "pplx-test"
    tools = ToolRegistry(mock_engine, config)
    tools._web_search._rate_limit = 0

    mock_ddgs.return_value.text.return_value = [
        {"title": "Ubuntu", "href": "http://example.com", "body": "OS"},
    ]
    mock_post.return_value = _perplexity_response(
        answer="Get it here magnet:?xt=urn:btih:abc123", citations=["http://pplx.com"]
    )

    result = tools._web_search.search("ubuntu")
    assert result["providers"] == ["duckduckgo", "perplexity"]
    assert result["answer"].startswith("Get it here")
    assert result["magnets"] == ["magnet:?xt=urn:btih:abc123"]
    mock_get.assert_not_called()  # no Brave key — Brave never queried


@patch("agent.tools.requests.post")
@patch("agent.tools.requests.get")
@patch("agent.tools.DDGS")
def test_web_search_llm_false_skips_perplexity(mock_ddgs, mock_get, mock_post, mock_engine):
    """llm=False (bulk sweeps) keeps Perplexity out even when a key is configured."""
    config = DeeptorrentConfig()
    config.web_search.brave_api_key = "BSA-test"
    config.web_search.api_key = "pplx-test"
    tools = ToolRegistry(mock_engine, config)
    tools._web_search._rate_limit = 0

    mock_ddgs.return_value.text.return_value = [
        {"title": "Ubuntu", "href": "http://example.com", "body": "OS"},
    ]
    mock_get.return_value = _brave_response(
        [{"title": "Brave", "url": "http://brave.com", "description": "brave"}]
    )

    result = tools._web_search.search("ubuntu", llm=False)
    assert result["provider"] == "duckduckgo+brave"
    assert result["providers"] == ["duckduckgo", "brave"]
    assert "answer" not in result
    mock_post.assert_not_called()  # Perplexity skipped


@patch("agent.tools.requests.post")
@patch("agent.tools.requests.get")
@patch("agent.tools.DDGS")
def test_web_search_partial_failure_still_merges(mock_ddgs, mock_get, mock_post, mock_engine):
    """A failing provider doesn't block results from the others."""
    config = DeeptorrentConfig()
    config.web_search.brave_api_key = "BSA-test"
    config.web_search.api_key = "pplx-test"
    tools = ToolRegistry(mock_engine, config)
    tools._web_search._rate_limit = 0

    mock_ddgs.return_value.text.side_effect = Exception("ddg down")
    mock_get.return_value = _brave_response(
        [{"title": "Brave", "url": "http://brave.com", "description": "brave"}]
    )
    mock_post.return_value = _perplexity_response(answer="ans", citations=["http://pplx.com"])

    result = tools._web_search.search("ubuntu")
    assert result["providers"] == ["brave", "perplexity"]
    assert [r["url"] for r in result["results"]] == ["http://brave.com", "http://pplx.com"]


@patch("agent.tools.requests.post")
@patch("agent.tools.requests.get")
@patch("agent.tools.DDGS")
def test_web_search_all_empty(mock_ddgs, mock_get, mock_post, mock_engine):
    """Every provider failing yields an empty result set, not an exception."""
    import requests as rq

    config = DeeptorrentConfig()
    config.web_search.brave_api_key = "BSA-test"
    config.web_search.api_key = "pplx-test"
    tools = ToolRegistry(mock_engine, config)
    tools._web_search._rate_limit = 0

    mock_ddgs.return_value.text.side_effect = Exception("ddg down")
    mock_get.side_effect = rq.ConnectionError("brave down")
    mock_post.side_effect = rq.ConnectionError("perplexity down")

    result = tools._web_search.search("ubuntu")
    assert result["results"] == []
    assert result["providers"] == []


def test_web_search_config_tolerates_stale_keys():
    """A config saved with the removed google_enabled key still loads."""
    import json
    import tempfile
    import os

    from config import DeeptorrentConfig

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"web_search": {"google_enabled": True, "brave_api_key": "BSA-x"}}, f)
        config = DeeptorrentConfig.from_file(path)
    assert config.web_search.brave_api_key == "BSA-x"
    assert not hasattr(config.web_search, "google_enabled")


@patch("agent.tools.ToolRegistry._fetch_about")
@patch("agent.tools.TorznabClient.search")
def test_search_indexers_attaches_about(mock_search, mock_about, mock_engine):
    """The about complement rides along with torrent results (and the cache)."""
    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = []  # all-in-one endpoint path
    tools = ToolRegistry(mock_engine, config)

    mock_search.return_value = {"success": True, "results": [{"name": "x", "seeders": 5}]}
    mock_about.return_value = {"source": "tmdb", "title": "The Matrix", "year": "1999"}

    result = tools.call("search_indexers", {"query": "The Matrix 1999"})
    assert result["success"] is True
    assert result["about"]["source"] == "tmdb"
    # Cached result keeps the about field and doesn't re-run the lookup.
    mock_about.reset_mock()
    again = tools.call("search_indexers", {"query": "The Matrix 1999"})
    assert again["about"]["title"] == "The Matrix"
    mock_about.assert_not_called()


@patch("agent.tools.TorznabClient.search")
def test_search_indexers_without_about_still_works(mock_search, mock_engine):
    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = []
    tools = ToolRegistry(mock_engine, config)
    mock_search.return_value = {"success": True, "results": [{"name": "x", "seeders": 5}]}
    with patch.object(ToolRegistry, "_fetch_about", side_effect=RuntimeError("boom")):
        result = tools.call("search_indexers", {"query": "ubuntu"})
    assert result["success"] is True
    assert "about" not in result


def test_fetch_about_tmdb_match(mock_engine):
    """Confident TMDb hit produces a tmdb about block."""
    config = DeeptorrentConfig()
    config.iptv.tmdb_api_key = "tmdb-test"
    tools = ToolRegistry(mock_engine, config)
    meta = {"title": "The Matrix", "year": "1999", "rating": 8.2,
            "genres": ["Action", "Sci-Fi"], "synopsis": "A hacker learns the truth."}
    with patch("iptv.metadata.TMDBProvider.fetch", return_value=meta):
        about = tools._fetch_about("The Matrix 1999 1080p")
    assert about["source"] == "tmdb"
    assert about["kind"] == "movie"
    assert about["year"] == "1999"
    assert about["genres"] == ["Action", "Sci-Fi"]


def test_fetch_about_tmdb_fuzzy_miss_falls_back_to_wikipedia(mock_engine):
    """A TMDb hit for a different work is rejected; Wikipedia covers the topic."""
    config = DeeptorrentConfig()
    config.iptv.tmdb_api_key = "tmdb-test"
    tools = ToolRegistry(mock_engine, config)

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.raise_for_status = lambda: None
        if "opensearch" in str(kwargs.get("params", {}).get("action", "")):
            resp.json.return_value = ["john digweed", ["John Digweed"], [""], ["https://en.wikipedia.org/wiki/John_Digweed"]]
        else:
            resp.json.return_value = {
                "title": "John Digweed",
                "description": "British DJ and record producer",
                "extract": "John Digweed is a British DJ, record producer and actor.",
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/John_Digweed"}},
            }
        return resp

    with patch("iptv.metadata.TMDBProvider.fetch", return_value={"title": "Totally Unrelated", "year": "2001"}), \
         patch("agent.tools.requests.get", side_effect=fake_get):
        about = tools._fetch_about("john digweed")
    assert about["source"] == "wikipedia"
    assert "British DJ" in about["description"]
    assert about["url"].endswith("/John_Digweed")


def test_fetch_about_tolerates_total_failure(mock_engine):
    config = DeeptorrentConfig()
    config.iptv.tmdb_api_key = "tmdb-test"
    tools = ToolRegistry(mock_engine, config)
    with patch("iptv.metadata.TMDBProvider.fetch", side_effect=RuntimeError("tmdb down")), \
         patch("agent.tools.requests.get", side_effect=RuntimeError("wiki down")):
        assert tools._fetch_about("anything") is None


def test_add_download_submits_file_job(mock_engine):
    """add_download submits a direct URL to the injected download manager."""
    from agent.tools import ToolError

    dl = MagicMock()
    job = MagicMock(id="job1", filename="x.zip", save_path="/dl/x.zip", job_type="file")
    dl.add_job.return_value = job
    tools = ToolRegistry(mock_engine, DeeptorrentConfig(), dl_engine=dl)

    result = tools.call("add_download", {"url": "https://example.com/files/x.zip?token=1"})
    dl.add_job.assert_called_once()
    assert dl.add_job.call_args.kwargs["url"] == "https://example.com/files/x.zip?token=1"
    assert result["success"] is True and result["job_id"] == "job1"

    with pytest.raises(ToolError):
        tools.call("add_download", {"url": "ftp://example.com/x.zip"})


def test_add_download_stream_detection(mock_engine):
    """HLS/DASH URLs route to add_stream_job, plain files to add_job."""
    dl = MagicMock()
    dl.add_stream_job.return_value = MagicMock(id="s1", filename="v.mp4", save_path="/dl/v.mp4", job_type="hls")
    tools = ToolRegistry(mock_engine, DeeptorrentConfig(), dl_engine=dl)

    result = tools.call("add_download", {"url": "https://example.com/live/master.m3u8"})
    dl.add_stream_job.assert_called_once()
    dl.add_job.assert_not_called()
    assert result["job_type"] == "hls"


def test_add_download_lazy_engine_creation(mock_engine):
    """Without an injected engine (CLI), one is created and started on demand."""
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    fake = MagicMock()
    fake.add_job.return_value = MagicMock(id="j9", filename="y.iso", save_path="/dl/y.iso", job_type="file")
    with patch("dlmgr.engine.DownloadEngine", return_value=fake):
        result = tools.call("add_download", {"url": "https://example.com/y.iso"})
    fake.start.assert_called_once()
    assert result["success"] is True
    tools.shutdown()
    fake.stop.assert_called_once()


def test_web_fetch_extracts_download_links(mock_engine):
    """web_fetch surfaces direct file/stream links, resolved against the page URL."""
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    html = (
        '<a href="/files/setup-1.2.msi">msi</a>'
        '<a href="https://cdn.example.com/v/clip.mp4">mp4</a>'
        '<a href="torrents/x.torrent">tor</a>'
        '<a href="/about.html">nope</a>'
    )
    resp = MagicMock()
    resp.text = html
    resp.status_code = 200
    resp.headers = {"content-type": "text/html"}
    resp.raise_for_status = lambda: None
    with patch("agent.tools.requests.get", return_value=resp):
        result = tools.call("web_fetch", {"url": "https://example.com/page/index.html"})
    assert result["success"] is True
    assert result["download_links"] == [
        "https://example.com/files/setup-1.2.msi",
        "https://cdn.example.com/v/clip.mp4",
    ]
    assert result["torrent_urls"] == ["https://example.com/page/torrents/x.torrent"]


def test_torznab_error_body_is_not_success():
    """Jackett returns HTTP 200 with an <error> body for a bad API key — that
    must surface as an error, not as a successful search with zero results."""
    from agent.tools import TorznabClient
    from config import IndexerConfig

    client = TorznabClient(IndexerConfig(api_key="k"))
    bad_key = client._parse_torznab(
        '<?xml version="1.0" encoding="UTF-8"?> <error code="100" description="Invalid API Key" />'
    )
    assert bad_key["success"] is False
    assert "Invalid API Key" in bad_key["error"]
    ok = client._parse_torznab(
        '<?xml version="1.0"?><rss><channel><item><title>x</title>'
        '<size>1</size><link>magnet:?xt=urn:btih:abc</link></item></channel></rss>'
    )
    assert ok["success"] is True and ok["count"] == 1


def test_jackett_env_var_never_clobbers_saved_key(monkeypatch):
    """A saved Jackett API key wins over the JACKETT_API_KEY env var; the env
    var only fills in when the config file has no key."""
    import json
    import os
    import tempfile

    from config import DeeptorrentConfig

    monkeypatch.setenv("JACKETT_API_KEY", "env-key")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"indexer": {"api_key": "saved-key"}}, f)
        assert DeeptorrentConfig.from_file(path).indexer.api_key == "saved-key"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"indexer": {"api_key": ""}}, f)
        assert DeeptorrentConfig.from_file(path).indexer.api_key == "env-key"


def test_fresh_install_has_no_api_keys(monkeypatch):
    """Since 3.5 the shared DeepSeek key ships in the box so the agent works
    out of the box; every OTHER key field must still come up empty and a
    user-saved or env-provided key always wins over the shared one."""
    import os
    import tempfile

    from config import DeeptorrentConfig, _SHARED_DEEPSEEK_API_KEY

    for var in ("DEEPSEEK_API_KEY", "JACKETT_API_KEY", "BRAVE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    with tempfile.TemporaryDirectory() as td:
        cfg = DeeptorrentConfig.from_file(os.path.join(td, "missing.json"))
    assert cfg.llm.api_key == _SHARED_DEEPSEEK_API_KEY
    assert cfg.indexer.api_key == ""
    assert cfg.web_search.api_key == ""
    assert cfg.web_search.brave_api_key == ""
    assert cfg.iptv.tmdb_api_key == ""

    # The shared key never overrides a user-saved one.
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"llm": {"api_key": "sk-my-own"}}, fh)
        assert DeeptorrentConfig.from_file(path).llm.api_key == "sk-my-own"

    # Bare defaults stay keyless (only from_file injects the shared key).
    plain = DeeptorrentConfig()
    assert plain.llm.api_key == ""
    assert plain.indexer.api_key == ""
    assert plain.web_search.api_key == ""
    assert plain.iptv.tmdb_api_key == ""


def test_fresh_install_has_no_sources():
    """No built-in sources ship: fresh installs (and upgrades — the installer
    rewrites config.json) start with an empty list, the installer-seeded
    empty list is not refilled from defaults, and user-added sources survive
    a reload."""
    import json
    import os
    import tempfile

    from config import DEFAULT_SOURCES, DeeptorrentConfig

    assert DEFAULT_SOURCES == []

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")

        # Missing config file = fresh install.
        assert DeeptorrentConfig.from_file(path).sources.sources == []

        # Installer-seeded empty list stays empty (no defaults merged back).
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"sources": {"use_jackett": True, "sources": []}}, f)
        assert DeeptorrentConfig.from_file(path).sources.sources == []

        # User-added sources are preserved as-is.
        saved = {"sources": {"sources": [{
            "id": "1337x", "name": "1337x", "url": "https://1337x.to/",
            "type": "public", "enabled": True,
        }]}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(saved, f)
        cfg = DeeptorrentConfig.from_file(path)
        assert [s.id for s in cfg.sources.sources] == ["1337x"]


def test_irc_test_era_network_migrates_to_deepflux():
    """Saved networks still carrying the old '#deepflux-test' default channel
    have it DROPPED (3.2.1+: no shipped auto-join channel anymore — #DeepFlux
    is stripped too; the tab auto-requests /LIST instead). The auto_connect
    field was removed in 3.2.1 — stale saved values are dropped on load."""
    import os
    import tempfile

    from config import DeeptorrentConfig

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")
        saved = {"irc": {"networks": [{
            "id": "libera", "host": "irc.libera.chat", "port": 6697, "tls": True,
            "nick": "DeepFluxUser", "channels": ["#deepflux-test"], "auto_connect": False,
        }]}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(saved, f)
        cfg = DeeptorrentConfig.from_file(path)
        net = next(n for n in cfg.irc.networks if n.id == "libera")
        assert net.channels == []
        assert not hasattr(net, "auto_connect")

        # Other joined channels survive the migration; casing variants match.
        saved["irc"]["networks"][0]["channels"] = ["#DeepFlux-Test", "#mychan", "#DeepFlux"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(saved, f)
        cfg = DeeptorrentConfig.from_file(path)
        net = next(n for n in cfg.irc.networks if n.id == "libera")
        assert net.channels == ["#mychan"]

        # Customized entries (no test channel) are left untouched.
        saved["irc"]["networks"][0]["channels"] = ["#mychan"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(saved, f)
        cfg = DeeptorrentConfig.from_file(path)
        net = next(n for n in cfg.irc.networks if n.id == "libera")
        assert net.channels == ["#mychan"]


def test_browser_homepage_defaults_to_deepflux_site():
    """Homepage defaults to https://deepflux.space/; empty/missing values migrate,
    custom user homepages are preserved."""
    import os
    import tempfile

    from config import DEFAULT_BROWSER_HOMEPAGE, DeeptorrentConfig

    assert DEFAULT_BROWSER_HOMEPAGE == "https://deepflux.space/"
    assert DeeptorrentConfig().browser.homepage == DEFAULT_BROWSER_HOMEPAGE

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")

        # Fresh install / empty value -> default site.
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"browser": {"homepage": ""}}, f)
        assert DeeptorrentConfig.from_file(path).browser.homepage == DEFAULT_BROWSER_HOMEPAGE

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"browser": {}}, f)
        assert DeeptorrentConfig.from_file(path).browser.homepage == DEFAULT_BROWSER_HOMEPAGE

        # Custom homepage is preserved.
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"browser": {"homepage": "https://example.com/"}}, f)
        assert DeeptorrentConfig.from_file(path).browser.homepage == "https://example.com/"


def test_default_download_folder_is_deepflux():
    """Downloads default to ~/Downloads/DeepFlux; configs saved with an old
    default (DeepTorrent/Deeptorrent) or an empty value migrate on load,
    while custom user folders are kept."""
    import json
    import os
    import tempfile
    from pathlib import Path

    from config import DeeptorrentConfig

    new_default = str(Path.home() / "Downloads" / "DeepFlux")

    plain = DeeptorrentConfig()
    assert plain.default_save_path == new_default
    assert plain.download.default_folder == new_default

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")

        # Fresh install (the installer seeds empty strings) -> new default.
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"default_save_path": "", "download": {"default_folder": ""}}, f)
        cfg = DeeptorrentConfig.from_file(path)
        assert cfg.default_save_path == new_default
        assert cfg.download.default_folder == new_default

        # Old defaults migrate to the new folder.
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "default_save_path": str(Path.home() / "Downloads" / "Deeptorrent"),
                "download": {"default_folder": str(Path.home() / "Downloads" / "DeepTorrent")},
            }, f)
        cfg = DeeptorrentConfig.from_file(path)
        assert cfg.default_save_path == new_default
        assert cfg.download.default_folder == new_default

        # Custom user folders are preserved.
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "default_save_path": "D:\\Media\\Torrents",
                "download": {"default_folder": "D:\\Media\\IDM"},
            }, f)
        cfg = DeeptorrentConfig.from_file(path)
        assert cfg.default_save_path == "D:\\Media\\Torrents"
        assert cfg.download.default_folder == "D:\\Media\\IDM"


# ---------------------------------------------------------------------------
# Filesystem tools
# ---------------------------------------------------------------------------

def test_list_directory_dirs_first_then_alpha(mock_engine, tmp_path):
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "c.txt").write_text("xyz")
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())

    result = tools.call("list_directory", {"path": str(tmp_path)})
    assert result["success"] is True
    assert result["count"] == 3
    names = [e["name"] for e in result["entries"]]
    assert names == ["a_dir", "b.txt", "c.txt"]  # dirs first, then alphabetical
    assert result["entries"][0]["type"] == "dir"
    assert result["entries"][1]["size"] == 1


def test_create_folder_nested(mock_engine, tmp_path):
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    target = tmp_path / "a" / "b" / "c"
    result = tools.call("create_folder", {"path": str(target)})
    assert result["success"] is True
    assert target.is_dir()


def test_copy_move_rename_delete_roundtrip(mock_engine, tmp_path):
    src = tmp_path / "file.txt"
    src.write_text("data")
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())

    r = tools.call("copy_path", {"source": str(src), "destination": str(tmp_path / "sub")})
    # "sub" doesn't exist -> treated as the target file path, parents created.
    assert r["success"] is True
    assert (tmp_path / "sub").read_text() == "data"

    dest_dir = tmp_path / "destdir"
    dest_dir.mkdir()
    r = tools.call("copy_path", {"source": str(src), "destination": str(dest_dir)})
    assert (dest_dir / "file.txt").read_text() == "data"

    r = tools.call("rename_path", {"path": str(src), "new_name": "renamed.txt"})
    assert r["success"] is True
    assert not src.exists() and (tmp_path / "renamed.txt").exists()

    r = tools.call("move_path", {"source": str(tmp_path / "renamed.txt"), "destination": str(dest_dir)})
    assert (dest_dir / "renamed.txt").exists()

    r = tools.call("delete_path", {"path": str(dest_dir / "renamed.txt")})
    assert r["success"] is True
    assert not (dest_dir / "renamed.txt").exists()


def test_copy_path_overwrite_guard(mock_engine, tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())

    with pytest.raises(ToolError, match="overwrite"):
        tools.call("copy_path", {"source": str(tmp_path / "a.txt"), "destination": str(tmp_path / "b.txt")})
    assert (tmp_path / "b.txt").read_text() == "b"  # untouched

    r = tools.call("copy_path", {"source": str(tmp_path / "a.txt"),
                                 "destination": str(tmp_path / "b.txt"), "overwrite": True})
    assert r["success"] is True
    assert (tmp_path / "b.txt").read_text() == "a"


def test_delete_nonempty_dir_requires_recursive(mock_engine, tmp_path):
    d = tmp_path / "full"
    d.mkdir()
    (d / "x.txt").write_text("x")
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())

    with pytest.raises(ToolError):
        tools.call("delete_path", {"path": str(d)})
    assert d.exists()

    r = tools.call("delete_path", {"path": str(d), "recursive": True})
    assert r["success"] is True
    assert not d.exists()


def test_rename_rejects_paths_and_collisions(mock_engine, tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())

    with pytest.raises(ToolError, match="plain name"):
        tools.call("rename_path", {"path": str(tmp_path / "a.txt"), "new_name": "sub/c.txt"})
    with pytest.raises(ToolError, match="already exists"):
        tools.call("rename_path", {"path": str(tmp_path / "a.txt"), "new_name": "b.txt"})


def test_missing_source_raises(mock_engine, tmp_path):
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    with pytest.raises(ToolError, match="not found"):
        tools.call("copy_path", {"source": str(tmp_path / "nope"), "destination": str(tmp_path / "x")})
    with pytest.raises(ToolError, match="not found"):
        tools.call("delete_path", {"path": str(tmp_path / "nope")})


# ---------------------------------------------------------------------------
# IPTV tools (Play tab)
# ---------------------------------------------------------------------------

class _FakeIPTVBridge:
    """Stands in for gui.iptv_tab.AgentIPTVBridge — no Qt required."""

    def __init__(self, manager):
        self.manager = manager
        self.calls = []

    def play_item(self, item):
        self.calls.append(("play_item", item))

    def play_url(self, url, title=""):
        self.calls.append(("play_url", url, title))

    def play_file(self, path):
        self.calls.append(("play_file", path))

    def stop(self):
        self.calls.append(("stop",))

    def pause(self):
        self.calls.append(("pause",))

    def set_volume(self, level):
        self.calls.append(("volume", level))

    def status(self):
        return {"state": "playing", "position": 12.0, "duration": 100.0,
                "title": "BBC One", "url": "http://stream/bbc"}


@pytest.fixture
def iptv_setup(mock_engine, tmp_path):
    from iptv.manager import IPTVManager
    from iptv.models import Category, Channel, Episode, Movie, Playlist, Series

    manager = IPTVManager(sources=[], data_dir=str(tmp_path))
    ch = Channel(id="s1::ch1", name="BBC One", url="http://stream/bbc",
                 tvg_id="bbc1.uk", group="News")
    movie = Movie(id="s1::m1", name="The Matrix", url="http://vod/matrix.mkv",
                  group="Sci-Fi", year="1999", rating=8.7)
    series = Series(id="s1::s1", name="Breaking Bad", group="Drama",
                    episodes=[Episode(season=1, episode=1, url="http://vod/bb-s01e01.mkv")])
    pl = Playlist(source_id="s1", channels=[ch], movies=[movie], series=[series],
                  categories=[Category(name="News", section="live", count=1)])
    manager._playlists["s1"] = pl
    manager._active_source_id = "s1"
    bridge = _FakeIPTVBridge(manager)
    tools = ToolRegistry(mock_engine, DeeptorrentConfig(), iptv_bridge=bridge)
    return tools, bridge, ch, movie, series


def test_iptv_search_across_sections(iptv_setup):
    tools, _bridge, ch, movie, _series = iptv_setup
    result = tools.call("iptv_search", {"query": "bbc"})
    assert result["success"] is True
    assert result["total_found"] == 1
    assert result["sections"]["live"][0]["id"] == ch.id

    result = tools.call("iptv_search", {"query": "matrix"})
    hit = result["sections"]["movies"][0]
    assert hit["id"] == movie.id
    assert hit["year"] == "1999"
    assert hit["rating"] == 8.7

    result = tools.call("iptv_search", {"query": "nothing matches this"})
    assert result["total_found"] == 0
    assert "note" in result


def test_iptv_list_with_categories(iptv_setup):
    tools, _bridge, *_ = iptv_setup
    result = tools.call("iptv_list", {"section": "live"})
    assert result["success"] is True
    assert result["total"] == 1
    assert result["items"][0]["name"] == "BBC One"
    assert result["categories"] == [{"name": "News", "count": 1}]

    with pytest.raises(ToolError, match="section"):
        tools.call("iptv_list", {"section": "radio"})


def test_iptv_epg_resolves_channel(iptv_setup):
    tools, _bridge, *_ = iptv_setup
    result = tools.call("iptv_epg", {"channel": "bbc one"})
    assert result["success"] is True
    assert result["channel"] == "BBC One"
    assert result["tvg_id"] == "bbc1.uk"
    assert "note" in result  # no guide data in the test cache

    result = tools.call("iptv_epg", {"channel": "no such channel"})
    assert result["success"] is False


def test_iptv_play_by_query_and_id(iptv_setup):
    tools, bridge, ch, movie, _series = iptv_setup

    result = tools.call("iptv_play", {"query": "bbc one"})
    assert result["success"] is True
    assert bridge.calls[-1] == ("play_item", ch)

    result = tools.call("iptv_play", {"item_id": movie.id})
    assert result["success"] is True
    assert bridge.calls[-1] == ("play_item", movie)

    result = tools.call("iptv_play", {"query": "no match here"})
    assert result["success"] is False


def test_iptv_play_series_rejected(iptv_setup):
    tools, bridge, *_ = iptv_setup
    result = tools.call("iptv_play", {"query": "breaking bad"})
    assert result["success"] is False
    assert "series" in result["error"]
    assert not any(c[0] == "play_item" for c in bridge.calls)


def test_iptv_play_url_and_file(iptv_setup, tmp_path):
    tools, bridge, *_ = iptv_setup
    media = tmp_path / "clip.mp4"
    media.write_text("fake")

    result = tools.call("iptv_play", {"url": "http://example.com/live.m3u8", "title": "Live"})
    assert result["success"] is True
    assert bridge.calls[-1] == ("play_url", "http://example.com/live.m3u8", "Live")

    result = tools.call("iptv_play", {"file": str(media)})
    assert result["success"] is True
    assert bridge.calls[-1] == ("play_file", str(media))

    with pytest.raises(ToolError, match="not found"):
        tools.call("iptv_play", {"file": str(tmp_path / "nope.mp4")})


def test_iptv_play_requires_gui(mock_engine):
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    result = tools.call("iptv_play", {"url": "http://example.com/x.m3u8"})
    assert result["success"] is False
    assert "GUI" in result["error"]


def test_iptv_now_playing(iptv_setup, mock_engine):
    tools, _bridge, *_ = iptv_setup
    result = tools.call("iptv_now_playing", {})
    assert result["success"] is True
    assert result["state"] == "playing"
    assert result["title"] == "BBC One"

    cli_tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    result = cli_tools.call("iptv_now_playing", {})
    assert result["success"] is False


def test_iptv_player_controls(iptv_setup, mock_engine):
    tools, bridge, *_ = iptv_setup
    assert tools.call("iptv_pause", {})["success"] is True
    assert tools.call("iptv_stop", {})["success"] is True
    assert tools.call("iptv_set_volume", {"level": 250})["volume"] == 100  # clamped
    assert ("pause",) in bridge.calls
    assert ("stop",) in bridge.calls
    assert ("volume", 100) in bridge.calls

    cli_tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    assert cli_tools.call("iptv_stop", {})["success"] is False


def test_new_tools_classified():
    from agent.loop import DESTRUCTIVE_TOOLS, READ_ONLY_TOOLS

    for t in ("list_directory", "iptv_search", "iptv_list", "iptv_epg", "iptv_now_playing"):
        assert t in READ_ONLY_TOOLS
    for t in ("create_folder", "copy_path", "move_path", "rename_path", "delete_path", "iptv_play"):
        assert t in DESTRUCTIVE_TOOLS


# ---------------------------------------------------------------------------
# Browser tools (Browse tab)
# ---------------------------------------------------------------------------

class _FakeBrowserBridge:
    """Stands in for gui.browser_bridge.BrowserBridge — no Qt required."""

    def __init__(self):
        self.calls = []

    def call(self, op, **params):
        self.calls.append((op, params))
        return {"success": True, "action": op, **params}


def test_browser_tools_route_through_bridge(mock_engine):
    bridge = _FakeBrowserBridge()
    tools = ToolRegistry(mock_engine, DeeptorrentConfig(), browser_bridge=bridge)

    assert tools.call("browser_list_tabs", {})["success"] is True
    assert tools.call("browser_navigate", {"url": "example.com", "new_tab": True})["success"] is True
    assert tools.call("browser_close_tab", {"index": 2})["success"] is True
    assert tools.call("browser_switch_tab", {"index": 0})["success"] is True
    assert tools.call("browser_go", {"action": "back"})["success"] is True
    assert tools.call("browser_get_content", {"max_chars": 500, "include_links": False})["success"] is True
    assert tools.call("browser_snapshot", {"limit": 20})["success"] is True
    assert tools.call("browser_wait", {"url_contains": "example"})["success"] is True
    assert tools.call("browser_click_ref", {"ref": "e1"})["success"] is True
    assert tools.call("browser_type_ref", {"ref": "e2", "value": "hello"})["success"] is True
    assert tools.call("browser_select_ref", {"ref": "e3", "value": "Option"})["success"] is True
    assert tools.call("browser_check_ref", {"ref": "e4", "checked": True})["success"] is True
    assert tools.call("browser_click", {"text": "Download"})["success"] is True
    assert tools.call("browser_fill", {"selector": "#q", "value": "hi", "submit": True})["success"] is True
    assert tools.call("browser_scroll", {"direction": "bottom"})["success"] is True
    assert tools.call("browser_add_bookmark", {"url": "https://x.com", "title": "X"})["success"] is True
    assert tools.call("browser_remove_bookmark", {"url": "https://x.com"})["success"] is True
    assert tools.call("browser_list_bookmarks", {})["success"] is True

    actions = [a for a, _ in bridge.calls]
    assert actions == ["list_tabs", "navigate", "close_tab", "switch_tab", "go",
                       "get_content", "snapshot", "wait", "click_ref", "type_ref",
                       "select_ref", "check_ref", "click", "fill", "scroll",
                       "add_bookmark", "remove_bookmark", "list_bookmarks"]
    # Params forwarded as bridge kwargs.
    assert bridge.calls[1][1] == {"url": "example.com", "new_tab": True}
    assert bridge.calls[5][1] == {"max_chars": 500, "include_links": False}
    assert bridge.calls[13][1] == {"selector": "#q", "value": "hi", "submit": True}


def test_browser_tools_require_gui(mock_engine):
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    for name, args in (("browser_list_tabs", {}),
                       ("browser_navigate", {"url": "example.com"}),
                       ("browser_click", {"selector": "a"}),
                       ("browser_get_content", {})):
        result = tools.call(name, args)
        assert result["success"] is False
        assert "GUI" in result["error"]


def test_browser_tools_classification():
    from agent.loop import DESTRUCTIVE_TOOLS, READ_ONLY_TOOLS

    assert {"browser_list_tabs", "browser_get_content", "browser_snapshot", "browser_wait", "browser_list_bookmarks"} <= READ_ONLY_TOOLS
    assert {"browser_click", "browser_fill", "browser_click_ref", "browser_type_ref", "browser_select_ref", "browser_check_ref", "browser_close_tab", "browser_add_bookmark", "browser_remove_bookmark"} <= DESTRUCTIVE_TOOLS
    # Navigation/tab switching run without confirmation but sequentially.
    for t in ("browser_navigate", "browser_switch_tab", "browser_go", "browser_scroll"):
        assert t not in READ_ONLY_TOOLS and t not in DESTRUCTIVE_TOOLS


# ---------------------------------------------------------------------------
# Download-manager job tools (Downloads panel)
# ---------------------------------------------------------------------------

def _fake_dl_job(job_id="abc123def456", status="downloading", filename="x.zip"):
    job = MagicMock()
    job.id = job_id
    job.filename = filename
    job.save_path = f"/dl/{filename}"
    job.url = f"http://example.com/{filename}"
    job.job_type = "file"
    job.status.value = status
    job.file_size = 100
    job.downloaded = 40
    job.speed_bps = 10
    job.error_message = "boom" if status == "error" else ""
    return job


def test_list_downloads_and_filter(mock_engine):
    dl = MagicMock()
    jobs = [_fake_dl_job("aaa111", "downloading", "a.zip"),
            _fake_dl_job("bbb222", "completed", "b.zip")]
    dl.list_jobs.return_value = jobs
    tools = ToolRegistry(mock_engine, DeeptorrentConfig(), dl_engine=dl)

    result = tools.call("list_downloads", {})
    assert result["success"] and result["count"] == 2
    first = result["downloads"][0]
    assert first["job_id"] == "aaa111"
    assert first["progress_pct"] == 40.0
    assert first["speed_bps"] == 10

    result = tools.call("list_downloads", {"status": "completed"})
    assert result["count"] == 1
    assert result["downloads"][0]["filename"] == "b.zip"


def test_download_job_controls(mock_engine):
    dl = MagicMock()
    job = _fake_dl_job("abc123def456", "downloading")
    dl.list_jobs.return_value = [job]
    dl.get_job.side_effect = lambda jid: job if jid == job.id else None
    dl.pause_job.return_value = True
    dl.resume_job.return_value = True
    dl.retry_job.return_value = job
    dl.cancel_job.return_value = True
    dl.remove_job.return_value = True
    tools = ToolRegistry(mock_engine, DeeptorrentConfig(), dl_engine=dl)

    # Unique id prefix resolves.
    r = tools.call("pause_download", {"job_id": "abc123"})
    assert r["success"] and r["job_id"] == job.id
    dl.pause_job.assert_called_once_with(job.id)

    assert tools.call("resume_download", {"job_id": job.id})["success"] is True
    assert tools.call("retry_download", {"job_id": job.id})["success"] is True

    r = tools.call("cancel_download", {"job_id": job.id, "delete_file": False})
    assert r["success"] and r["deleted_file"] is False
    dl.cancel_job.assert_called_once_with(job.id, delete_file=False)

    assert tools.call("remove_download", {"job_id": job.id})["success"] is True


def test_download_job_id_errors(mock_engine):
    dl = MagicMock()
    jobs = [_fake_dl_job("abc111", "paused", "a.zip"),
            _fake_dl_job("abc222", "paused", "b.zip")]
    dl.list_jobs.return_value = jobs
    dl.get_job.return_value = None
    tools = ToolRegistry(mock_engine, DeeptorrentConfig(), dl_engine=dl)

    with pytest.raises(ToolError, match="Ambiguous"):
        tools.call("pause_download", {"job_id": "abc"})
    with pytest.raises(ToolError, match="Unknown job id"):
        tools.call("pause_download", {"job_id": "zzz"})

    # Engine refusal surfaces as success=false with a reason.
    dl.pause_job.return_value = False
    r = tools.call("pause_download", {"job_id": "abc111"})
    assert r["success"] is False and r["error"]


def test_torrent_session_tools(mock_engine):
    config = DeeptorrentConfig()
    tools = ToolRegistry(mock_engine, config)

    r = tools.call("set_torrent_rate_limits", {"download_kb": 500, "upload_kb": 100})
    assert r["success"] is True
    mock_engine.set_rate_limits.assert_called_once_with(500, 100)
    assert config.torrents.download_rate_limit_kb == 500

    assert tools.call("set_sequential_download", {"info_hash": "a" * 40, "on": True})["success"]
    mock_engine.set_sequential_download.assert_called_once_with("a" * 40, True)
    assert tools.call("force_recheck", {"info_hash": "a" * 40})["success"]
    mock_engine.force_recheck.assert_called_once_with("a" * 40)
    assert tools.call("force_reannounce", {"info_hash": "a" * 40})["success"]
    mock_engine.force_reannounce.assert_called_once_with("a" * 40)


def test_rss_feed_management(mock_engine, tmp_path):
    config = DeeptorrentConfig()
    tools = ToolRegistry(mock_engine, config)
    with patch.object(DeeptorrentConfig, "default_config_path",
                      return_value=str(tmp_path / "config.json")):
        r = tools.call("add_rss_feed", {"url": "https://example.com/feed.xml",
                                        "name": "Example", "mode": "monitor"})
        assert r["success"] is True
        assert len(config.rss.feeds) == 1
        assert (tmp_path / "config.json").exists()  # persisted

        # Duplicate subscribe is a no-op.
        r = tools.call("add_rss_feed", {"url": "https://example.com/feed.xml"})
        assert "already" in r["note"].lower()
        assert len(config.rss.feeds) == 1

        r = tools.call("remove_rss_feed", {"url": "https://example.com/feed.xml"})
        assert r["success"] is True
        assert config.rss.feeds == []

        r = tools.call("remove_rss_feed", {"url": "https://example.com/feed.xml"})
        assert r["success"] is False

    with pytest.raises(ToolError, match="mode"):
        tools.call("add_rss_feed", {"url": "https://x.com/f", "mode": "banana"})
    with pytest.raises(ToolError, match="Invalid feed URL"):
        tools.call("add_rss_feed", {"url": "ftp://x.com/f"})


def test_rss_item_dict_preserves_stable_guid():
    from agent.rss import FeedItem

    item = FeedItem(title="Release", link="https://example.com/release", guid="stable-guid")
    data = item.to_dict()

    assert data["item_id"] == "stable-guid"
    assert data["guid"] == "stable-guid"


def test_get_rss_items_is_side_effect_free(mock_engine):
    from config import RSSFeed

    config = DeeptorrentConfig()
    feed = RSSFeed(url="https://example.com/feed.xml", seen_items=["old-guid"])
    config.rss.feeds = [feed]
    tools = ToolRegistry(mock_engine, config)
    tools._rss_monitor.check_feed = MagicMock(return_value={
        "feed_name": "Example", "total_items": 2, "new_items": 1,
        "items": [{"item_id": "new-guid", "guid": "new-guid", "title": "New"}],
        "all_items": [
            {"item_id": "old-guid", "guid": "old-guid", "title": "Old"},
            {"item_id": "new-guid", "guid": "new-guid", "title": "New"},
        ],
    })

    result = tools.call("get_rss_feed_items", {"feed_url": feed.url})

    assert result["items"][0]["item_id"] == "new-guid"
    assert feed.seen_items == ["old-guid"]


def test_download_from_feed_uses_stable_ids_and_marks_success_only(mock_engine, tmp_path):
    from config import RSSFeed

    config = DeeptorrentConfig()
    feed = RSSFeed(url="https://example.com/feed.xml", seen_items=["old-guid"])
    config.rss.feeds = [feed]
    tools = ToolRegistry(mock_engine, config)
    new_item = {
        "item_id": "new-guid", "guid": "new-guid", "title": "New",
        "magnet_uri": "magnet:?xt=urn:btih:" + "b" * 40,
    }
    tools._rss_monitor.check_feed = MagicMock(return_value={
        "items": [new_item],
        "all_items": [{"item_id": "old-guid", "guid": "old-guid", "title": "Old"}, new_item],
    })
    mock_engine.add_magnet.return_value = "b" * 40

    with patch.object(DeeptorrentConfig, "default_config_path", return_value=str(tmp_path / "config.json")):
        result = tools.call("download_from_feed", {"feed_url": feed.url, "item_ids": ["new-guid"]})

    assert result["success"] is True
    assert result["downloaded"][0]["item_id"] == "new-guid"
    assert feed.seen_items == ["old-guid", "new-guid"]
    mock_engine.add_magnet.assert_called_once_with(new_item["magnet_uri"], config.default_save_path, "Other")


def test_download_from_feed_legacy_index_targets_new_items(mock_engine, tmp_path):
    from config import RSSFeed

    config = DeeptorrentConfig()
    feed = RSSFeed(url="https://example.com/feed.xml", seen_items=["old-guid"])
    config.rss.feeds = [feed]
    tools = ToolRegistry(mock_engine, config)
    new_item = {
        "item_id": "new-guid", "guid": "new-guid", "title": "New",
        "magnet_uri": "magnet:?xt=urn:btih:" + "c" * 40,
    }
    tools._rss_monitor.check_feed = MagicMock(return_value={
        "items": [new_item],
        "all_items": [
            {"item_id": "old-guid", "guid": "old-guid", "title": "Old",
             "magnet_uri": "magnet:?xt=urn:btih:" + "a" * 40},
            new_item,
        ],
    })
    mock_engine.add_magnet.return_value = "c" * 40

    with patch.object(DeeptorrentConfig, "default_config_path", return_value=str(tmp_path / "config.json")):
        result = tools.call("download_from_feed", {"feed_url": feed.url, "item_indices": [0]})

    assert result["downloaded"][0]["item_id"] == "new-guid"
    mock_engine.add_magnet.assert_called_once_with(new_item["magnet_uri"], config.default_save_path, "Other")


def test_update_rss_feed(mock_engine, tmp_path):
    from config import RSSFeed

    config = DeeptorrentConfig()
    config.rss.feeds = [RSSFeed(
        url="https://example.com/feed.xml", name="Old", mode="monitor", category="Other",
    )]
    tools = ToolRegistry(mock_engine, config)

    with patch.object(DeeptorrentConfig, "default_config_path", return_value=str(tmp_path / "config.json")):
        result = tools.call("update_rss_feed", {
            "url": "https://example.com/feed.xml", "name": "New",
            "mode": "auto_download", "category": "TV",
        })

    assert result["name"] == "New"
    assert result["mode"] == "auto_download"
    assert result["category"] == "TV"


def test_add_download_accepts_destination(mock_engine, tmp_path):
    dl = MagicMock()
    dl.add_job.return_value = MagicMock(
        id="job1", filename="x.zip", save_path=str(tmp_path / "x.zip"), job_type="file",
    )
    tools = ToolRegistry(mock_engine, DeeptorrentConfig(), dl_engine=dl)

    tools.call("add_download", {
        "url": "https://example.com/x.zip", "save_path": str(tmp_path),
    })

    assert dl.add_job.call_args.kwargs["save_path"] == str(tmp_path) + __import__("os").sep


def test_agent_diagnostics_exposes_capabilities_without_keys(mock_engine):
    config = DeeptorrentConfig()
    config.llm.api_key = "never-return-this"
    config.web_search.brave_api_key = "also-secret"
    tools = ToolRegistry(mock_engine, config)

    result = tools.call("agent_diagnostics", {})

    assert result["tools"]["total"] == len(tools.list_tools())
    assert result["llm"]["api_key_configured"] is True
    assert result["integrations"]["brave"] is True
    assert "never-return-this" not in json.dumps(result)
    assert "also-secret" not in json.dumps(result)


def test_successful_empty_search_is_cached(mock_engine):
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    tools._search_indexers_uncached = MagicMock(return_value={
        "success": True, "count": 0, "results": [], "source": "test",
    })
    tools._fetch_about = MagicMock(return_value=None)

    first = tools.call("search_indexers", {"query": "nothing-here"})
    second = tools.call("search_indexers", {"query": "nothing-here"})

    assert first["results"] == second["results"] == []
    assert tools._search_indexers_uncached.call_count == 1


def test_multi_web_fetch_reports_partial_failure(mock_engine):
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    tools._fetch_one = MagicMock(side_effect=[
        {"success": True, "url": "https://example.com/a", "content": "a"},
        {"success": False, "url": "https://example.com/b", "error": "failed"},
    ])

    result = tools.call("web_fetch", {"urls": ["https://example.com/a", "https://example.com/b"]})

    assert result["success"] is True
    assert result["partial"] is True
    assert result["succeeded"] == 1
    assert result["failed"] == 1


def test_torrent_url_blocks_private_network(mock_engine):
    tools = ToolRegistry(mock_engine, DeeptorrentConfig())

    with patch("agent.tools.requests.get") as mock_get, pytest.raises(ToolError, match="private network"):
        tools.call("add_torrent_file", {
            "path": "http://127.0.0.1/file.torrent", "save_path": "C:\\Downloads",
        })

    mock_get.assert_not_called()


def test_torrent_url_allows_configured_local_jackett(mock_engine):
    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.indexer.url = "http://127.0.0.1:9117"
    tools = ToolRegistry(mock_engine, config)
    response = MagicMock()
    response.status_code = 200
    response.content = ("magnet:?xt=urn:btih:" + "d" * 40).encode()
    response.headers = {"Content-Type": "text/plain"}
    response.raise_for_status.return_value = None
    mock_engine.add_magnet.return_value = "d" * 40

    with patch("agent.tools.requests.get", return_value=response) as mock_get:
        result = tools.call("add_torrent_file", {
            "path": "http://127.0.0.1:9117/dl/test", "save_path": "C:\\Downloads",
        })

    assert result["success"] is True
    assert mock_get.call_args.kwargs["allow_redirects"] is False
    mock_engine.add_magnet.assert_called_once()


def test_lazy_download_engine_initialization_is_synchronized(mock_engine):
    from concurrent.futures import ThreadPoolExecutor

    tools = ToolRegistry(mock_engine, DeeptorrentConfig())
    engine = MagicMock()
    with patch("dlmgr.engine.DownloadEngine", return_value=engine) as constructor:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resources = list(pool.map(lambda _: tools._get_dl_engine(), range(20)))

    assert all(resource is engine for resource in resources)
    constructor.assert_called_once()
    engine.start.assert_called_once()
    tools.shutdown()


def test_rss_fetch_blocks_private_network():
    from agent.rss import RSSFeedClient
    from config import RSSFeed

    client = RSSFeedClient(RSSFeed(url="http://169.254.169.254/feed.xml"))
    with patch("agent.rss.requests.get") as mock_get:
        assert client.fetch() == []
    mock_get.assert_not_called()


def test_rss_parser_rejects_entity_declarations():
    from agent.rss import RSSFeedClient
    from config import RSSFeed

    client = RSSFeedClient(RSSFeed(url="https://example.com/feed.xml"))
    content = b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&xxe;</title></item></channel></rss>'

    assert client._parse(content) == []


def test_gapfill_tools_classified():
    from agent.loop import DESTRUCTIVE_TOOLS, READ_ONLY_TOOLS

    assert "list_downloads" in READ_ONLY_TOOLS
    assert {"cancel_download", "add_rss_feed", "update_rss_feed", "remove_rss_feed", "download_from_feed", "edit_memory", "forget_memory", "apply_organization_plan"} <= DESTRUCTIVE_TOOLS
    assert {"propose_rename_and_category", "analyze_organization", "list_memories", "agent_diagnostics"} <= READ_ONLY_TOOLS
    for t in ("pause_download", "resume_download", "retry_download", "remove_download",
              "set_torrent_rate_limits", "set_sequential_download",
              "force_recheck", "force_reannounce"):
        assert t not in READ_ONLY_TOOLS and t not in DESTRUCTIVE_TOOLS
