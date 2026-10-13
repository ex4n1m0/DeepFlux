"""Unit tests for infra.indexer_health + its integration into the Jackett
search tier (2026-10-13 study follow-up).

All error strings below are REAL, captured live from this machine's Jackett
(v0.24.2793, 96 configured indexers) — keep them verbatim so the classifier
keeps matching what Jackett actually returns.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from config import DeeptorrentConfig, SourceConfig
from infra import indexer_health, jackett
from infra.indexer_health import (
    COOLDOWN_THRESHOLD,
    KIND_AUTH,
    KIND_DOWN,
    KIND_FLARE,
    KIND_JACKETT,
    KIND_MOVED,
    KIND_RATE,
    KIND_TIMEOUT,
    KIND_UNKNOWN,
    ZOMBIE_FAILURES,
    IndexerHealthStore,
    classify_indexer_error,
)


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.list_torrents.return_value = []
    engine.get_torrent_status.return_value = {}
    return engine


# ----------------------------------------------------------------------
# classify_indexer_error — real Jackett error strings
# ----------------------------------------------------------------------

def test_classify_flare_challenge():
    err = ("Jackett.Common.IndexerException: Exception (1337x): Challenge detected "
           "but FlareSolverr is not configured")
    assert classify_indexer_error(err) == KIND_FLARE


def test_classify_site_down_522():
    err = ("Jackett.Common.IndexerException: Exception (pornxlab): Request to "
           "https://www.pornxlab.com/s.php?search=x failed (Error 522) - "
           "The tracker seems to be down.")
    assert classify_indexer_error(err) == KIND_DOWN


def test_classify_moved_domain():
    err = ("Jackett.Common.IndexerException: Exception (52bt): Got redirected to "
           "another domain. Try changing the indexer URL to https://www.52btbt.icu/.")
    assert classify_indexer_error(err) == KIND_MOVED


def test_classify_unknown_indexer_json_500():
    # Zombie source: Jackett answers HTTP 500 with a JSON body; the client's
    # XML parse of that surfaces as a parse-ish error string.
    assert classify_indexer_error("Unknown indexer: u2p") == KIND_UNKNOWN
    assert classify_indexer_error("Indexer u2p is not configured") == KIND_UNKNOWN


def test_classify_jackett_outage_is_global_not_per_indexer():
    """The exact requests exception when the Jackett host is down — contains
    'ConnectTimeoutError' whose text also says 'timeout'. It MUST classify as
    the global kind or a Jackett outage mass-cooldowns every indexer."""
    err = ("HTTPConnectionPool(host='127.0.0.1', port=9117): Max retries exceeded "
           "with url: /api/v2.0/indexers/1337x/results/torznab "
           "(Caused by ConnectTimeoutError(<urllib3.connection.HTTPConnection object "
           "at 0x...>, 'Connection to 127.0.0.1 timed out. (connect timeout=25)'))")
    assert classify_indexer_error(err) == KIND_JACKETT


def test_classify_tracker_timeout_is_per_indexer():
    assert classify_indexer_error("HTTPSConnectionPool... Read timed out. (read timeout=25)") == KIND_TIMEOUT


def test_classify_rate_limit_needs_explicit_marker():
    # A bare "429" must NOT match: .NET stack traces carry line numbers.
    assert classify_indexer_error("at Jackett.Common/Indexers/BaseIndexer.cs:line 429") != KIND_RATE
    assert classify_indexer_error("Search failed with Error 429 (Too Many Requests)") == KIND_RATE


def test_classify_auth_and_other():
    assert classify_indexer_error("Login page returned - credentials rejected") == KIND_AUTH
    assert classify_indexer_error("something entirely novel") == "other"


# ----------------------------------------------------------------------
# IndexerHealthStore — cooldown policy + persistence
# ----------------------------------------------------------------------

def _store(tmp_path) -> IndexerHealthStore:
    return IndexerHealthStore(str(tmp_path / "health.json"))


def test_cooldown_starts_after_threshold(tmp_path):
    store = _store(tmp_path)
    assert store.record_failure("a", KIND_FLARE, "Challenge detected")["failures"] == 1
    assert not store.is_cooling("a")  # one blip is not a pattern
    store.record_failure("a", KIND_FLARE, "Challenge detected")
    assert store.is_cooling("a")
    assert store.cooldown_remaining("a") > 0


def test_cooldown_doubles_and_caps(tmp_path):
    store = _store(tmp_path)
    entry = None
    for _ in range(20):
        entry = store.record_failure("a", KIND_DOWN, "522")
    assert entry["failures"] == 20
    # 8h cap (COOLDOWN_MAX) — capped long before 2**18 × 30min.
    assert store.cooldown_remaining("a") <= 8 * 3600 + 5


def test_success_resets_but_creates_no_entry(tmp_path):
    store = _store(tmp_path)
    store.record_success("healthy")  # no entry for healthy indexers
    assert store.summary()["kinds"] == {}
    store.record_failure("a", KIND_DOWN, "x")
    store.record_failure("a", KIND_DOWN, "x")
    assert store.is_cooling("a")
    store.record_success("a")
    assert not store.is_cooling("a")
    assert store.consecutive_failures("a") == 0


def test_global_failures_do_not_count(tmp_path):
    store = _store(tmp_path)
    for _ in range(10):
        store.record_failure("a", KIND_JACKETT, "Max retries exceeded")
    assert store.consecutive_failures("a") == 0
    assert not store.is_cooling("a")


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "health.json"
    store = IndexerHealthStore(str(path))
    store.record_failure("a", KIND_FLARE, "Challenge detected")
    store.record_failure("a", KIND_FLARE, "Challenge detected")
    store.flush()
    reloaded = IndexerHealthStore(str(path))
    assert reloaded.consecutive_failures("a") == 2
    assert reloaded.failure_kind("a") == KIND_FLARE
    assert reloaded.is_cooling("a")


def test_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("{not json", encoding="utf-8")
    store = IndexerHealthStore(str(path))
    assert store.summary() == {"cooling": {}, "kinds": {}}


def test_cooldown_expiry_lets_probe_through(tmp_path):
    store = _store(tmp_path)
    store.record_failure("a", KIND_FLARE, "x")
    store.record_failure("a", KIND_FLARE, "x")
    assert store.is_cooling("a")
    # Force-expire: rewrite the deadline in the persisted file, reload.
    store.flush()
    data = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    data["a"]["cooldown_until"] = time.time() - 1
    (tmp_path / "health.json").write_text(json.dumps(data), encoding="utf-8")
    reloaded = IndexerHealthStore(str(tmp_path / "health.json"))
    assert not reloaded.is_cooling("a")  # half-open: next search re-probes it


# ----------------------------------------------------------------------
# Search-tier integration (via ToolRegistry with mocked Jackett)
# ----------------------------------------------------------------------

def _registry(mock_engine, sources):
    from agent.tools import ToolRegistry
    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = sources
    return ToolRegistry(mock_engine, config), config


def test_search_skips_indexers_in_cooldown(mock_engine, tmp_path):
    """Two failures put an indexer in cooldown; the next search skips it
    without querying (it is re-probed only after the cooldown expires)."""
    from agent.tools import ToolRegistry

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="flarewall", name="FlareWall", type="public", popularity=100),
        SourceConfig(id="healthy", name="Healthy", type="public", popularity=50),
    ]
    with patch("agent.tools.TorznabClient.search_indexer") as mock_search:
        def fake(indexer_id, query, category=""):
            if indexer_id == "flarewall":
                return {"success": False, "results": [],
                        "error": "Challenge detected but FlareSolverr is not configured"}
            return {"success": True, "results": [{"name": "Ubuntu", "seeders": 50}]}
        mock_search.side_effect = fake
        tools = ToolRegistry(mock_engine, config)
        tools.call("search_indexers", {"query": "Ubuntu", "deep": True})
        tools.call("search_indexers", {"query": "Debian", "deep": True})
        ids = {c.args[0] for c in mock_search.call_args_list}
        assert ids == {"flarewall", "healthy"}  # failure #2 → cooldown just engaged
        # Fresh query strings so the 5-minute result cache doesn't swallow sweeps.
        mock_search.reset_mock()
        tools.call("search_indexers", {"query": "Fedora", "deep": True})
        ids = {c.args[0] for c in mock_search.call_args_list}
        assert "flarewall" not in ids  # cooling — not re-queried
        assert "healthy" in ids


def test_search_failure_summary_classified(mock_engine):
    from agent.tools import ToolRegistry

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="flarewall", name="FlareWall", type="public"),
        SourceConfig(id="deadsite", name="DeadSite", type="public"),
    ]
    with patch("agent.tools.TorznabClient.search_indexer") as mock_search:
        def fake(indexer_id, query, category=""):
            if indexer_id == "flarewall":
                return {"success": False, "results": [],
                        "error": "Challenge detected but FlareSolverr is not configured\n"
                                 "   at Jackett.Common..."}
            return {"success": False, "results": [],
                    "error": "Request to https://dead.example failed (Error 522) - "
                             "The tracker seems to be down."}
        mock_search.side_effect = fake
        tools = ToolRegistry(mock_engine, config)
        result = tools.call("search_indexers", {"query": "Ubuntu", "deep": True})
        # Both indexers failed → explicit failure with the classified summary.
        assert result["success"] is False
        summary = result["failure_summary"]["failed"]
        assert summary["flare"]["count"] == 1
        assert summary["flare"]["indexers"] == ["FlareWall"]
        assert "FlareSolverr" in summary["flare"]["hint"]
        assert summary["down"]["count"] == 1
        # The raw error entries are single-line (no .NET stack spillover).
        for entry in result["errors"]:
            assert "\n" not in entry


def test_zombie_stops_being_queried_but_stays_enabled(mock_engine):
    """A source Jackett keeps answering 'Unknown indexer' is provably not a
    Jackett indexer. After ZOMBIE_FAILURES strikes the Jackett tier stops
    querying it — but NEVER mutates the config (search_indexers is a
    read-only tool; a legacy manual website source draws the same answer and
    its web-tier value must survive)."""
    from agent.tools import ToolRegistry

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="zombie", name="Zombie", type="public", origin=""),
        SourceConfig(id="legacymanual", name="Legacy Manual", type="public", origin=""),
        SourceConfig(id="healthy", name="Healthy", type="public"),
    ]
    with patch("agent.tools.TorznabClient.search_indexer") as mock_search:
        def fake(indexer_id, query, category=""):
            if indexer_id in ("zombie", "legacymanual"):
                return {"success": False, "results": [], "error": f"Unknown indexer: {indexer_id}"}
            return {"success": True, "results": [{"name": "Ubuntu", "seeders": 30}]}
        mock_search.side_effect = fake
        tools = ToolRegistry(mock_engine, config)
        for n in range(ZOMBIE_FAILURES + 2):
            tools.call("search_indexers", {"query": f"Ubuntu {n}", "deep": True})
        # Nothing is ever disabled by a search — both "zombie-ish" sources
        # stay enabled (their web-search tier keeps working).
        assert all(s.enabled for s in config.sources.sources)
        # ... and after striking out they are no longer queried via Jackett.
        mock_search.reset_mock()
        tools.call("search_indexers", {"query": "Debian", "deep": True})
        queried_now = {c.args[0] for c in mock_search.call_args_list}
        assert "zombie" not in queried_now
        assert "legacymanual" not in queried_now
        assert "healthy" in queried_now
        # The result tells the agent about the skipped zombies compactly.
        result = tools.call("search_indexers", {"query": "Fedora", "deep": True})
        assert result["failure_summary"]["zombie_skipped"]["count"] == 2


def test_manual_sources_not_queried_against_jackett(mock_engine, tmp_path, monkeypatch):
    """origin='manual' website sources are not Jackett indexers — the Jackett
    tier must not query them (they'd always 'Unknown indexer'). Exception: a
    manual re-add of a genuine Jackett id (present in the definitions sidecar)
    IS queried through Jackett."""
    from agent.tools import ToolRegistry
    from infra import jackett

    monkeypatch.setenv("DF_JACKETT_DEFINITIONS", str(tmp_path / "defs.json"))
    jackett.save_definition_ids({"jackettid"})

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="myfansite", name="My Fan Site", type="public", origin="manual"),
        SourceConfig(id="jackettid", name="1337x re-added", type="public", origin="manual"),
        SourceConfig(id="jackettone", name="JackettOne", type="public", origin="jackett"),
    ]
    with patch("agent.tools.TorznabClient.search_indexer") as mock_search:
        mock_search.return_value = {"success": True, "results": [{"name": "X", "seeders": 5}]}
        tools = ToolRegistry(mock_engine, config)
        tools.call("search_indexers", {"query": "Ubuntu", "deep": True})
        assert {c.args[0] for c in mock_search.call_args_list} == {"jackettid", "jackettone"}


def test_category_caps_skip_off_category_indexers(mock_engine):
    from agent.tools import ToolRegistry

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="moviesite", name="MovieSite", type="public",
                     origin="jackett", torznab_caps=[2000, 2010, 2030]),
        SourceConfig(id="animesite", name="AnimeSite", type="public",
                     origin="jackett", torznab_caps=[5000, 5070]),  # TV only
        SourceConfig(id="nocapsite", name="NoCapSite", type="public",
                     origin="jackett", torznab_caps=[]),  # unknown caps — never skipped
    ]
    with patch("agent.tools.TorznabClient.search_indexer") as mock_search:
        mock_search.return_value = {"success": True, "results": [{"name": "Ubuntu", "seeders": 3}]}
        tools = ToolRegistry(mock_engine, config)
        tools.call("search_indexers", {"query": "Ubuntu", "category": "movies", "deep": True})
        ids = {c.args[0] for c in mock_search.call_args_list}
        assert ids == {"moviesite", "nocapsite"}  # anime-only skipped for a movie query


def test_caps_support_parent_matching():
    from agent.tools import ToolRegistry
    supports = ToolRegistry._caps_support
    assert supports([2000, 5000], "2000") is True
    assert supports([2010], "2000") is True      # subcat covers the parent query
    assert supports([2000], "2020") is True      # parent covers a subcat query
    assert supports([5000, 5070], "2000") is False
    assert supports([100026], "100026") is True
    assert supports([2000], "not-a-number") is True  # garbage → conservative


def test_zombie_suspects_probed_first_via_definitions_sidecar(mock_engine, tmp_path, monkeypatch):
    """Legacy ""-origin sources absent from the definitions sidecar sit at the
    popularity tail — early-exiting sweeps would never reach them. The tier
    probes them FIRST each search, so strikes land in ZOMBIE_FAILURES searches."""
    from agent.tools import ToolRegistry
    from infra import jackett

    monkeypatch.setenv("DF_JACKETT_DEFINITIONS", str(tmp_path / "defs.json"))
    jackett.save_definition_ids({"iptorrents", "1337x"})

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="u2p", name="U2P", type="public", origin=""),          # zombie suspect
        SourceConfig(id="iptorrents", name="IPTorrents", type="private", origin="jackett"),
    ]
    with patch("agent.tools.TorznabClient.search_indexer") as mock_search:
        def fake(indexer_id, query, category=""):
            if indexer_id == "u2p":
                return {"success": False, "results": [], "error": "Unknown indexer: u2p"}
            return {"success": True, "results": [{"name": "Ubuntu", "seeders": 40}]}
        mock_search.side_effect = fake
        tools = ToolRegistry(mock_engine, config)
        for n in range(ZOMBIE_FAILURES):
            tools.call("search_indexers", {"query": f"Ubuntu {n}"})
        by_id = {s.id: s for s in config.sources.sources}
        assert by_id["u2p"].enabled is True  # skipped, never disabled
        # iptorrents was queried (private tier) and still enabled
        assert by_id["iptorrents"].enabled is True
        assert "iptorrents" in {c.args[0] for c in mock_search.call_args_list}
        # After striking out, the suspect is no longer probed at all.
        mock_search.reset_mock()
        tools.call("search_indexers", {"query": "Debian"})
        assert "u2p" not in {c.args[0] for c in mock_search.call_args_list}


def test_unconfigured_jackett_indexer_recovers_after_sync(mock_engine, tmp_path, monkeypatch):
    """A defined-but-unconfigured Jackett indexer answers 'is not configured'
    (classified unknown → strikes → Jackett-tier skip). Once the user
    re-configures it in Jackett, the sync clears its strikes and the tier
    queries it again — the skip must never be a dead end."""
    from agent.tools import ToolRegistry
    from infra import jackett

    monkeypatch.setenv("DF_JACKETT_DEFINITIONS", str(tmp_path / "defs.json"))
    jackett.save_definition_ids({"iptorrents", "somesite"})

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.sources.sources = [
        SourceConfig(id="somesite", name="SomeSite", type="public", origin="jackett"),
    ]
    with patch("agent.tools.TorznabClient.search_indexer") as mock_search:
        state = {"configured": False}

        def fake(indexer_id, query, category=""):
            if not state["configured"]:
                return {"success": False, "results": [], "error": "Indexer somesite is not configured"}
            return {"success": True, "results": [{"name": "Ubuntu", "seeders": 9}]}
        mock_search.side_effect = fake
        tools = ToolRegistry(mock_engine, config)
        for n in range(ZOMBIE_FAILURES + 1):
            tools.call("search_indexers", {"query": f"Ubuntu {n}", "deep": True})
        mock_search.reset_mock()
        tools.call("search_indexers", {"query": "Debian", "deep": True})
        assert "somesite" not in {c.args[0] for c in mock_search.call_args_list}  # struck out

        # User re-configures it in Jackett → next sync clears the strikes.
        state["configured"] = True
        with patch("infra.jackett.fetch_indexers_detailed") as mock_fetch:
            mock_fetch.return_value = (
                [SourceConfig(id="somesite", name="SomeSite", type="public", origin="jackett")],
                {"iptorrents", "somesite"},
            )
            jackett.sync_sources(config, str(tmp_path / "config.json"))
        tools.call("search_indexers", {"query": "Fedora", "deep": True})
        assert "somesite" in {c.args[0] for c in mock_search.call_args_list}


# ----------------------------------------------------------------------
# TorznabClient._interpret_response — Jackett reports per-indexer failures
# as HTTP 400/500 WITH an error body; the real reason must survive
# ----------------------------------------------------------------------

def _resp(status, text, reason=""):
    import json as _json_module

    def _json(_self=None):  # bound as a class attr → receives self when called
        return _json_module.loads(text)
    return type("R", (), {"status_code": status, "text": text,
                          "reason": reason, "json": _json})()


def test_response_400_with_torznab_error_body_keeps_real_reason():
    """Live 2026-10-13: a Cloudflare-challenged indexer (1337x) answers
    HTTP 400 + <error code="900" description="...Challenge detected...">.
    raise_for_status-first collapsed this to '400 Client Error'."""
    from agent.tools import TorznabClient
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<error code="900" description="Jackett.Common.IndexerException: '
            'Exception (1337x): Challenge detected but FlareSolverr is not configured"/>')
    client = TorznabClient(DeeptorrentConfig().indexer)
    out = client._interpret_response(_resp(400, body))
    assert out["success"] is False
    assert "Challenge detected" in out["error"]
    assert classify_indexer_error(out["error"]) == KIND_FLARE


def test_response_500_with_json_envelope_surfaces_unknown_indexer():
    """Live: a removed-from-Jackett source answers HTTP 500 + JSON
    {"result": "error", "error": "Unknown indexer: u2p"}."""
    from agent.tools import TorznabClient
    client = TorznabClient(DeeptorrentConfig().indexer)
    out = client._interpret_response(_resp(500, '{"result": "error", "error": "Unknown indexer: u2p"}'))
    assert out["success"] is False
    assert out["error"] == "Unknown indexer: u2p"
    assert classify_indexer_error(out["error"]) == KIND_UNKNOWN


def test_response_plain_http_error_without_parsable_body():
    from agent.tools import TorznabClient
    client = TorznabClient(DeeptorrentConfig().indexer)
    out = client._interpret_response(_resp(404, "Not Found", "Not Found"))
    assert out["success"] is False
    assert "404" in out["error"]


def test_response_200_rss_passes_through_to_parser():
    from agent.tools import TorznabClient
    client = TorznabClient(DeeptorrentConfig().indexer)
    rss = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<rss version="2.0"><channel><item><title>Debian 12</title></item></channel></rss>')
    out = client._interpret_response(_resp(200, rss))
    assert out["success"] is True
    assert out["count"] == 1


def test_response_body_error_text_is_scrubbed():
    """A hostile/echoing server at the configured indexer URL can reflect the
    request (query string carries the apikey) inside an error body — the
    body-parsed paths must scrub like the exception paths do."""
    from agent.tools import TorznabClient
    client = TorznabClient(DeeptorrentConfig().indexer)
    leak = "boom for /api?v2.0/indexers/x/results/torznab?apikey=SECRET123&q=a"
    body = f'<?xml version="1.0"?><error code="900" description="{leak}"/>'
    out = client._interpret_response(_resp(400, body))
    assert "SECRET123" not in out["error"]
    out2 = client._interpret_response(_resp(500, '{"error": "failed at ?apikey=SECRET123"}'))
    assert "SECRET123" not in out2["error"]


# ----------------------------------------------------------------------
# set_flaresolverr_url — Jackett admin API push
# ----------------------------------------------------------------------

class _FakeSession:
    def __init__(self, fail_post=False):
        self.calls = []
        self.fail_post = fail_post

    @staticmethod
    def _resp(status=200, payload=None):
        return type("R", (), {"status_code": status, "ok": status < 400,
                              "json": lambda s: payload or {}})()

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self._resp(payload={"flaresolverrurl": ""})

    def post(self, url, **kw):
        self.calls.append(("POST", url, json.loads(kw["data"])["flaresolverrurl"]))
        return self._resp(status=500 if self.fail_post else 200)

    def close(self):
        pass


def test_set_flaresolverr_url_round_trip():
    config = DeeptorrentConfig()
    config.indexer.url = "http://127.0.0.1:9117"
    # The GET that verifies the write must return the NEW url — fake it by
    # making the second GET see what was posted.
    class _Session2(_FakeSession):
        def get(self, url, **kw):
            self.calls.append(("GET", url))
            val = "http://127.0.0.1:8191/" if len([c for c in self.calls if c[0] == "GET"]) > 1 else ""
            return self._resp(payload={"flaresolverrurl": val})
    session = _Session2()
    with patch("infra.jackett_setup.open_admin_session", return_value=session):
        result = jackett.set_flaresolverr_url(config, "http://127.0.0.1:8191/")
    assert result["success"] is True
    posted = [c for c in session.calls if c[0] == "POST"]
    assert posted and posted[0][2] == "http://127.0.0.1:8191/"


def test_set_flaresolverr_url_requires_url():
    assert jackett.set_flaresolverr_url(DeeptorrentConfig(), "  ")["success"] is False


def test_set_flaresolverr_url_admin_blocked():
    config = DeeptorrentConfig()
    with patch("infra.jackett_setup.open_admin_session", return_value=None):
        result = jackett.set_flaresolverr_url(config, "http://127.0.0.1:8191/")
    assert result["success"] is False
    assert "admin" in result["message"].lower()
