"""Tests for the IPTV subsystem: M3U parsing, classification, title cleaning,
Xtream client (mocked), cache, and manager orchestration.

These run without a GUI or network. Network-touching code (Xtream, metadata,
artwork) is exercised through mocks/monkeypatching.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from unittest import mock

import pytest
import requests

from iptv import classify, local_folder, m3u_parser, xtream
from iptv.cache import IPTVCache
from iptv.classify import classify_entry, classify, populate_years
from iptv.m3u_parser import parse_m3u, parse_season_episode
from iptv.manager import YEAR_OTHERS, IPTVManager
from iptv.metadata import clean_title, extract_year, metadata_key
from iptv.models import (
    SECTION_LIVE,
    SECTION_MOVIES,
    SECTION_SERIES,
    Category,
    Channel,
    Movie,
    Playlist,
    PlaylistSource,
    Series,
    make_id,
)


# ---------------------------------------------------------------------------
# M3U parser
# ---------------------------------------------------------------------------

SAMPLE_M3U = """#EXTM3U url-tvg="http://example.com/epg.xml"
#EXTINF:-1 tvg-id="cnn" tvg-name="CNN" tvg-logo="http://logo/cnn.png" group-title="News",CNN International
http://stream/cnn.m3u8
#EXTINF:-1 tvg-id="hbo" group-title="Movies",HBO Movie 1080p
http://vod/movie/hbo.mp4
#EXTINF:-1 group-title="Series",My Show S01E02 Pilot 720p
http://vod/series/myshow.mkv
#EXTINF:-1,Raw Channel
http://stream/raw.ts
#EXTGRP:Misc
http://stream/grp.ts
"""


def test_parse_m3u_extracts_attrs_and_url_tvg():
    pl = parse_m3u("src1", text=SAMPLE_M3U)
    assert pl.url_tvg == "http://example.com/epg.xml"
    assert len(pl.channels) == 5
    cnn = pl.channels[0]
    assert cnn.tvg_id == "cnn"
    assert cnn.tvg_name == "CNN"
    assert cnn.logo == "http://logo/cnn.png"
    assert cnn.group == "News"
    assert cnn.name == "CNN International"
    assert cnn.url == "http://stream/cnn.m3u8"


def test_parse_m3u_handles_extgrp_and_raw_url():
    pl = parse_m3u("src1", text=SAMPLE_M3U)
    # #EXTGRP applies to the next URL line.
    grp = pl.channels[4]
    assert grp.group == "Misc"
    # Raw URL with no #EXTINF gets a synthesized name.
    raw = pl.channels[3]
    assert raw.url == "http://stream/raw.ts"
    assert raw.name  # non-empty


def test_parse_m3u_progress_and_cancel(tmp_path):
    p = tmp_path / "pl.m3u"
    p.write_text(SAMPLE_M3U, encoding="utf-8")
    seen = []
    pl = parse_m3u("s", path=str(p), on_progress=lambda c, t: seen.append((c, t)))
    assert len(pl.channels) == 5
    assert seen  # progress callback fired


def test_parse_m3u_cancel_stops_early(tmp_path):
    lines = []
    for i in range(2000):
        lines.append(f"#EXTINF:-1,Channel {i}")
        lines.append(f"http://stream/{i}.ts")
    p = tmp_path / "big.m3u"
    p.write_text("\n".join(lines), encoding="utf-8")
    pl = parse_m3u("s", path=str(p), is_cancelled=lambda: True)
    # Cancellation happens before any entries are committed (checked per line).
    assert len(pl.channels) <= 1


def test_parse_season_episode():
    assert parse_season_episode("Show S01E02 Pilot") == (1, 2)
    assert parse_season_episode("Show s03e12") == (3, 12)
    assert parse_season_episode("Show Season 5") == (0, 0)
    assert parse_season_episode("No episode info") == (0, 0)


def test_parse_m3u_comma_inside_quoted_attr():
    # A comma inside a quoted attribute must not corrupt the display name.
    text = '#EXTINF:-1 tvg-name="Foo, Bar" group-title="News",Real Name\nhttp://s/1.ts\n'
    pl = parse_m3u("s", text=text)
    assert pl.channels[0].name == "Real Name"
    assert pl.channels[0].tvg_name == "Foo, Bar"


def test_parse_m3u_x_tvg_url():
    text = '#EXTM3U x-tvg-url="http://example.com/epg.xml"\n#EXTINF:-1,Ch\nhttp://s/1.ts\n'
    pl = parse_m3u("s", text=text)
    assert pl.url_tvg == "http://example.com/epg.xml"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_classify_entry_live_vs_movie_vs_series():
    assert classify_entry("News", "http://s/cnn.m3u8", "CNN") == SECTION_LIVE
    assert classify_entry("Movies", "http://vod/movie/x.mp4", "X") == SECTION_MOVIES
    assert classify_entry("Series", "http://vod/series/y.mkv", "Y") == SECTION_SERIES
    # URL pattern alone (no group).
    assert classify_entry("", "http://host/movie/abc.mkv", "ABC") == SECTION_MOVIES
    assert classify_entry("", "http://host/series/def.mkv", "DEF") == SECTION_SERIES
    # SxxExx in name with non-live extension -> series.
    assert classify_entry("", "http://host/x.mkv", "Show S01E02") == SECTION_SERIES


def test_classify_live_movie_channels_beat_group_keywords():
    # 24/7 movie *channels* in groups like "UK Movies" are live, not VOD:
    # country-prefixed name, no release year.
    assert classify_entry("UK Movies", "http://s/123", "UK: SKY CINEMA GREATS FHD") == SECTION_LIVE
    assert classify_entry("UK Movies", "http://s/123", "UK: FILM4 HD") == SECTION_LIVE
    # Real VOD in the same kind of group still classifies as movies.
    assert classify_entry("Movie VOD", "http://s/456", "Some Film (2021) [1080p]") == SECTION_MOVIES
    assert classify_entry("Movie VOD", "http://s/789", "Yearless Film [1080p] [WEBRip]") == SECTION_MOVIES


def test_classify_promotes_movies_and_groups_series():
    pl = parse_m3u("src1", text=SAMPLE_M3U)
    classify(pl)
    # CNN + raw .ts + grp .ts stay live; HBO .mp4 -> movie; series -> grouped.
    assert any(c.name.startswith("CNN") for c in pl.channels)
    assert any(isinstance(m, type(pl.movies[0])) for m in pl.movies) if pl.movies else True
    assert len(pl.movies) >= 1
    assert len(pl.series) >= 1
    # Series episodes collapse into one Series object.
    s = pl.series[0]
    assert len(s.episodes) == 1
    assert s.episodes[0].season == 1
    assert s.episodes[0].episode == 2


YEAR_M3U = """#EXTM3U
#EXTINF:-1 group-title="Movies",Some Movie 2021 1080p WEBRip
http://vod/movie/a.mp4
#EXTINF:-1 group-title="Movies",Omkara (2006) [1080p] [BluRay]
http://vod/movie/c.mp4
#EXTINF:-1 group-title="Movies",Yearless Film [1080p] [WEBRip]
http://vod/movie/b.mp4
#EXTINF:-1 group-title="Series",New Show 2024 S01E01 Pilot
http://vod/series/e.mkv
#EXTINF:-1 group-title="Series",Old Show S01E01 720p
http://vod/series/d.mkv
"""


def test_populate_years_from_names():
    """Movies/series get a release year from their names at load time."""
    pl = parse_m3u("src1", text=YEAR_M3U)
    classify(pl)
    populate_years(pl)
    by_name = {m.name: m.year for m in pl.movies}
    assert by_name["Some Movie 2021 1080p WEBRip"] == "2021"
    assert by_name["Omkara (2006) [1080p] [BluRay]"] == "2006"
    assert by_name["Yearless Film [1080p] [WEBRip]"] == ""
    by_series = {s.name: s.year for s in pl.series}
    assert by_series["New Show 2024"] == "2024"
    assert by_series["Old Show"] == ""


def test_populate_years_keeps_provider_years():
    """An Xtream releaseDate already on the item wins over name parsing."""
    series = Series(id=make_id("x", "series", "1"), name="Lost",
                    section=SECTION_SERIES, year="2004")
    pl = Playlist(source_id="x")
    pl.series.append(series)
    populate_years(pl)
    assert series.year == "2004"


# ---------------------------------------------------------------------------
# Adult VOD year extraction + JAV separation
# ---------------------------------------------------------------------------

def test_extract_adult_vod_year_2_digit_date():
    """Adult VOD entries use YY MM DD format — extracted as 4-digit year."""
    from iptv.classify import _extract_adult_vod_year
    assert _extract_adult_vod_year("Tushy 26 08 09 Kubera Fortuna") == "2026"
    assert _extract_adult_vod_year("SexMex 22 09 25 Camila Henao") == "2022"
    assert _extract_adult_vod_year("WowGirls 25 10 24 Bella Spark") == "2025"


def test_extract_adult_vod_year_90s():
    """2-digit years 50-99 map to 19xx."""
    from iptv.classify import _extract_adult_vod_year
    assert _extract_adult_vod_year("Vintage 98 03 15 Old Film") == "1998"


def test_extract_adult_vod_year_rejects_invalid_dates():
    """Random 2-digit numbers that aren't valid dates are not years."""
    from iptv.classify import _extract_adult_vod_year
    # Month 13 is invalid.
    assert _extract_adult_vod_year("Studio 26 13 09 Title") == ""
    # Day 32 is invalid.
    assert _extract_adult_vod_year("Studio 26 08 32 Title") == ""
    # No date pattern at all.
    assert _extract_adult_vod_year("Some Movie Title") == ""


def test_populate_years_extracts_adult_vod_dates():
    """populate_years uses the YY MM DD extractor for adult VOD entries."""
    pl = Playlist(source_id="src1")
    pl.movies.append(Movie(
        id=make_id("src1", "m1"), name="Tushy 26 08 09 Kubera Fortuna",
        url="http://x/1", group="XXX VOD", section=SECTION_MOVIES,
    ))
    pl.movies.append(Movie(
        id=make_id("src1", "m2"), name="Blacked 25 12 01 Dolly Dyson",
        url="http://x/2", group="XXX VOD", section=SECTION_MOVIES,
    ))
    populate_years(pl)
    by_name = {m.name: m.year for m in pl.movies}
    assert by_name["Tushy 26 08 09 Kubera Fortuna"] == "2026"
    assert by_name["Blacked 25 12 01 Dolly Dyson"] == "2025"


def test_populate_years_adult_vod_no_date_lands_in_others():
    """Adult VOD entries without a YY MM DD date still get empty year
    (landing in 'Others'), not a false positive from random numbers."""
    pl = Playlist(source_id="src1")
    pl.movies.append(Movie(
        id=make_id("src1", "m1"), name="PORNBOX: BRAZZERS",
        url="http://x/1", group="XXX VOD", section=SECTION_MOVIES,
    ))
    populate_years(pl)
    assert pl.movies[0].year == ""


def test_populate_years_non_adult_ignores_2_digit_dates():
    """Non-adult entries with 2-digit numbers must NOT be interpreted as
    dates — 'Movie 24 01 15 Title' in a regular group stays yearless."""
    pl = Playlist(source_id="src1")
    pl.movies.append(Movie(
        id=make_id("src1", "m1"), name="Some Film 24 01 15 Title",
        url="http://x/1", group="Movie VOD", section=SECTION_MOVIES,
    ))
    populate_years(pl)
    # The 4-digit extractor is used for non-adult groups, and "24" is not
    # a 4-digit year, so this stays empty.
    assert pl.movies[0].year == ""


def test_populate_years_keeps_jav_in_same_group():
    """All adult content shares ONE folder structure (user decision
    2026-09-10): JAV-looking entries are NOT re-grouped — they stay in
    the same group as western adult content."""
    pl = Playlist(source_id="src1")
    pl.movies.append(Movie(
        id=make_id("src1", "m1"), name="JapanHDV 26 08 28 Reika Ayano",
        url="http://x/1", group="XXX VOD", section=SECTION_MOVIES,
    ))
    pl.movies.append(Movie(
        id=make_id("src1", "m2"), name="ABP-123 Some Title",
        url="http://x/2", group="XXX VOD", section=SECTION_MOVIES,
    ))
    pl.movies.append(Movie(
        id=make_id("src1", "m3"), name="Tushy 26 08 09 Kubera Fortuna",
        url="http://x/3", group="XXX VOD", section=SECTION_MOVIES,
    ))
    populate_years(pl)
    assert {m.group for m in pl.movies} == {"XXX VOD"}
    # No synthetic "XXX VOD JAV" category either.
    cat_names = [c.name for c in pl.categories if c.section == SECTION_MOVIES]
    assert "XXX VOD JAV" not in cat_names


def test_playlist_from_cache_merges_legacy_jav_groups():
    """Playlists cached before the JAV-split removal carry synthetic
    '<group> JAV' folders — the cache loader merges them back so all adult
    content shows under one folder."""
    from iptv.manager import _playlist_from_cache
    data = {
        "movies": [
            {"id": "a", "name": "JapanHDV 26 08 28 Reika Ayano",
             "url": "http://x/1", "group": "XXX VOD JAV", "section": SECTION_MOVIES},
            {"id": "b", "name": "Tushy 26 08 09 Kubera Fortuna",
             "url": "http://x/2", "group": "XXX VOD", "section": SECTION_MOVIES},
        ],
        "categories": [
            {"name": "XXX VOD JAV", "section": SECTION_MOVIES, "count": 1},
            {"name": "XXX VOD", "section": SECTION_MOVIES, "count": 1},
        ],
    }
    pl = _playlist_from_cache("src1", data)
    assert {m.group for m in pl.movies} == {"XXX VOD"}
    cat_names = [c.name for c in pl.categories if c.section == SECTION_MOVIES]
    assert cat_names == ["XXX VOD"]


# ---------------------------------------------------------------------------
# Bulk slicing (sidebar displays ≤500 items per leaf node)
# ---------------------------------------------------------------------------

def test_bulk_slice_first_chunk():
    """Bulk index 1 returns the first BULK_SIZE items."""
    from gui.iptv_tab import BULK_SIZE
    pl = Playlist(source_id="s1")
    for i in range(1200):
        pl.movies.append(Movie(
            id=make_id("s1", f"m{i}"), name=f"Movie {i}",
            url=f"http://x/{i}", group="VOD", section=SECTION_MOVIES,
        ))
    mgr = IPTVManager(sources=[], data_dir="")
    mgr._playlists["s1"] = pl
    mgr._active_source_id = "s1"
    all_items = mgr.items_for(SECTION_MOVIES, "VOD", source_id="s1")
    # Simulate _show_section bulk=1 slicing.
    bulk = 1
    start = (bulk - 1) * BULK_SIZE
    chunk = all_items[start:start + BULK_SIZE]
    assert len(chunk) == BULK_SIZE
    assert chunk[0].name == "Movie 0"
    assert chunk[-1].name == f"Movie {BULK_SIZE - 1}"


def test_bulk_slice_last_chunk_partial():
    """The last bulk may have fewer than BULK_SIZE items."""
    from gui.iptv_tab import BULK_SIZE
    pl = Playlist(source_id="s1")
    for i in range(1200):
        pl.movies.append(Movie(
            id=make_id("s1", f"m{i}"), name=f"Movie {i}",
            url=f"http://x/{i}", group="VOD", section=SECTION_MOVIES,
        ))
    mgr = IPTVManager(sources=[], data_dir="")
    mgr._playlists["s1"] = pl
    mgr._active_source_id = "s1"
    all_items = mgr.items_for(SECTION_MOVIES, "VOD", source_id="s1")
    bulk = 3
    start = (bulk - 1) * BULK_SIZE
    chunk = all_items[start:start + BULK_SIZE]
    assert len(chunk) == 200  # 1200 - 2*500
    assert chunk[0].name == "Movie 1000"
    assert chunk[-1].name == "Movie 1199"


def test_bulk_slice_with_year_filter():
    """Bulk slicing works on top of a year filter."""
    from gui.iptv_tab import BULK_SIZE
    pl = Playlist(source_id="s1")
    for i in range(600):
        pl.movies.append(Movie(
            id=make_id("s1", f"m{i}"), name=f"Movie {i} 2025",
            url=f"http://x/{i}", group="VOD", section=SECTION_MOVIES,
            year="2025",
        ))
    for i in range(600):
        pl.movies.append(Movie(
            id=make_id("s1", f"n{i}"), name=f"Other {i} 2024",
            url=f"http://x/n{i}", group="VOD", section=SECTION_MOVIES,
            year="2024",
        ))
    mgr = IPTVManager(sources=[], data_dir="")
    mgr._playlists["s1"] = pl
    mgr._active_source_id = "s1"
    year_items = mgr.items_for(SECTION_MOVIES, "VOD", source_id="s1",
                               year="2025")
    assert len(year_items) == 600
    bulk = 2
    start = (bulk - 1) * BULK_SIZE
    chunk = year_items[start:start + BULK_SIZE]
    assert len(chunk) == 100  # 600 - 500
    assert all(getattr(m, "year", "") == "2025" for m in chunk)


def test_bulk_no_slice_when_bulk_zero():
    """bulk=0 (no bulk specified) returns all items un-sliced."""
    from gui.iptv_tab import BULK_SIZE
    pl = Playlist(source_id="s1")
    for i in range(700):
        pl.movies.append(Movie(
            id=make_id("s1", f"m{i}"), name=f"Movie {i}",
            url=f"http://x/{i}", group="VOD", section=SECTION_MOVIES,
        ))
    mgr = IPTVManager(sources=[], data_dir="")
    mgr._playlists["s1"] = pl
    mgr._active_source_id = "s1"
    all_items = mgr.items_for(SECTION_MOVIES, "VOD", source_id="s1")
    bulk = 0
    if bulk > 0:
        start = (bulk - 1) * BULK_SIZE
        all_items = all_items[start:start + BULK_SIZE]
    assert len(all_items) == 700  # un-sliced


def test_bulk_node_count_calculation():
    """Verify the bulk count math matches the sidebar's _add_bulk_nodes."""
    from gui.iptv_tab import BULK_SIZE
    for count, expected in [
        (500, 0),   # at threshold → no bulk nodes
        (501, 2),   # just over → 2 bulks (1-500, 501-501)
        (1000, 2),  # exactly 2 bulks
        (1001, 3),  # 3 bulks, last one has 1 item
        (1200, 3),  # 3 bulks, last has 200
    ]:
        if count <= BULK_SIZE:
            n_bulks = 0
        else:
            n_bulks = (count + BULK_SIZE - 1) // BULK_SIZE
        assert n_bulks == expected, f"count={count}: got {n_bulks}, want {expected}"


# ---------------------------------------------------------------------------
# Title cleaning + metadata key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Movie.Name.2023.1080p.x264.BluRay", "Movie Name 2023"),
    ("US: Some Channel", "Some Channel"),
    ("[US] Show S01E02 Pilot 720p", "Show"),
    ("Film_2020_4K_HDR", "Film 2020 4K"),
    # Bracketed release tags and parenthesized years are stripped — both
    # break TMDb search queries if left in.
    ("Omkara (2006) [1080p] [BluRay] [WEB]", "Omkara"),
    ("Tiffany Haddish: Black Mitzvah (2019) [1080p] [WEBRip] [WEB]", "Tiffany Haddish: Black Mitzvah"),
    # Scene release-group suffixes and audio layouts wreck TMDb queries.
    ("Rhythm Is A Dancer (2025) 1080p WEBRip-LAMA [1080p]", "Rhythm Is A Dancer"),
    ("The Birthday Party (2025) 1080p WEBRip 5 1-LAMA", "The Birthday Party"),
    ("Leviticus (2026) 1080p WEBRip DDP5.1-LAMA", "Leviticus"),
    ("Pretty Young Love (2025) NORDIC 1080p WEBRip", "Pretty Young Love"),
    ("CMA Country Christmas 2025 -MeGusta", "CMA Country Christmas 2025"),
    # …but hyphens that belong to the title survive.
    ("Mission Impossible - Fallout", "Mission Impossible - Fallout"),
    ("Spider-Man", "Spider-Man"),
])
def test_clean_title(raw, expected):
    assert clean_title(raw) == expected


def test_extract_year():
    assert extract_year("Movie 2019 1080p") == "2019"
    assert extract_year("No year here") == ""


def test_metadata_key_stable():
    k1 = metadata_key("movies", "Movie 2020 1080p", "2020")
    k2 = metadata_key("movies", "Movie.2020.1080p", "2020")
    assert k1 == k2
    assert k1.startswith("movies:")


def test_tmdb_poster_uses_w780():
    """Posters are fetched at a resolution that survives the larger tile size."""
    from iptv.metadata import TMDBProvider
    prov = TMDBProvider("key")
    assert prov._img("/poster.jpg") == "https://image.tmdb.org/t/p/w500/poster.jpg"
    assert prov._img("/poster.jpg", "w780") == "https://image.tmdb.org/t/p/w780/poster.jpg"


def test_tmdb_prefers_result_matching_year():
    """TMDb ranks by popularity, so a remake can sit above the right film —
    when a year is known, the matching result must win over results[0]."""
    from iptv.metadata import TMDBProvider
    prov = TMDBProvider("key")
    search_resp = mock.Mock(status_code=200, json=lambda: {"results": [
        {"id": 1, "title": "Dune", "release_date": "2021-10-22",
         "poster_path": "/new.jpg", "vote_average": 8.0},
        {"id": 2, "title": "Dune", "release_date": "1984-12-14",
         "poster_path": "/old.jpg", "vote_average": 6.0},
    ]})
    detail_resp = mock.Mock(status_code=200, json=lambda: {"genres": [], "release_date": "1984-12-14"})
    with mock.patch.object(prov._session, "get", side_effect=[search_resp, detail_resp]):
        meta = prov.fetch("Dune", "1984", SECTION_MOVIES)
    assert meta is not None
    assert meta["year"] == "1984"
    assert meta["poster"].endswith("/old.jpg")


def test_tmdb_year_filter_falls_back_to_first_result():
    """No result matches the known year -> keep results[0] (year may be wrong
    in the playlist; an unmatched poster beats none)."""
    from iptv.metadata import TMDBProvider
    prov = TMDBProvider("key")
    search_resp = mock.Mock(status_code=200, json=lambda: {"results": [
        {"id": 1, "title": "Dune", "release_date": "2021-10-22", "poster_path": "/new.jpg"},
    ]})
    detail_resp = mock.Mock(status_code=200, json=lambda: {"genres": []})
    with mock.patch.object(prov._session, "get", side_effect=[search_resp, detail_resp]):
        meta = prov.fetch("Dune", "1984", SECTION_MOVIES)
    assert meta is not None
    assert meta["poster"].endswith("/new.jpg")


# ---------------------------------------------------------------------------
# Adult VOD -> ThePornDB
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("group,name,expected", [
    ("XXX", "Some Scene", True),
    ("For Adults", "Some Scene", True),
    ("18+ Movies", "Some Scene", True),
    ("Adult", "Some Scene", True),
    ("Erotic Cinema", "Some Scene", True),
    ("VOD | XXX", "Some Scene", True),
    # Unambiguous markers are trusted on the name too.
    ("Movies", "XXX Scene Title", True),
    ("Movies", "SOME JAV Title", True),
    # …but ambiguous words on a *name* must not divert a real film from TMDb.
    ("Movies", "Adult World", False),
    ("Movies", "Adults in the Room", False),
    ("Movies", "Erotica", False),
    ("Movies", "The Adultery Plot", False),
    ("Documentaries", "Regular Movie", False),
])
def test_looks_adult(group, name, expected):
    from iptv.metadata import looks_adult
    assert looks_adult(group, name) is expected


@pytest.mark.parametrize("raw,expected", [
    ("ABP-123", "ABP-123"),
    ("abp00123", "ABP-123"),
    ("SSNI 456 1080p", "SSNI-456"),
    ("Regular Movie 2023", ""),
])
def test_jav_code(raw, expected):
    from iptv.metadata import jav_code
    assert jav_code(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    # Studio prefix is dropped; the general cleaner would keep it.
    ("Brazzers - Late For Work", "Late For Work"),
    # Trailing scene date goes.
    ("Some Scene Title 2023-05-14", "Some Scene Title"),
    # A short remainder means the "prefix" was really the title — keep it.
    ("Vixen - Go", "Vixen - Go"),
])
def test_adult_clean_title(raw, expected):
    from iptv.metadata import adult_clean_title
    assert adult_clean_title(raw) == expected


def _tpdb_row():
    """Shape verified against the live api.theporndb.net /movies response."""
    return {
        "title": "Scene Title",
        "date": "2023-05-14",
        "rating": 7.5,
        "description": "A synopsis.",
        "tags": [{"name": "Tag One"}, {"name": "Tag Two"}],
        "poster": "http://cdn/fallback.jpg",
        "posters": {"full": "http://cdn/full.jpg", "large": "http://cdn/large.jpg",
                    "medium": None, "small": None},
        "background": {"full": "http://cdn/bg.jpg", "large": None,
                       "medium": None, "small": None},
    }


def test_tpdb_maps_response_to_pipeline_fields():
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    with mock.patch.object(prov._session, "get") as get:
        get.return_value = mock.Mock(
            status_code=200, ok=True,
            json=lambda: {"data": [_tpdb_row()]},
            raise_for_status=lambda: None,
        )
        meta = prov.fetch("Scene Title", "2023", SECTION_MOVIES)
    assert meta["title"] == "Scene Title"
    assert meta["year"] == "2023"
    assert meta["rating"] == 7.5
    assert meta["synopsis"] == "A synopsis."
    assert meta["genres"] == ["Tag One", "Tag Two"]
    assert meta["poster"] == "http://cdn/large.jpg"   # large wins
    assert meta["backdrop"] == "http://cdn/bg.jpg"    # nulls skipped
    assert meta["provider"] == "tpdb"


def test_tpdb_falls_back_when_posters_sizes_are_null():
    """`posters.*` can be all-null while the flat `poster` field is set."""
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    row = _tpdb_row()
    row["posters"] = {"full": None, "large": None, "medium": None, "small": None}
    row["background"] = {"full": None, "large": None}
    with mock.patch.object(prov._session, "get") as get:
        get.return_value = mock.Mock(
            status_code=200, ok=True,
            json=lambda: {"data": [row]},
            raise_for_status=lambda: None,
        )
        meta = prov.fetch("Scene Title", "", SECTION_MOVIES)
    assert meta["poster"] == "http://cdn/fallback.jpg"
    assert meta["backdrop"] == ""


def test_tpdb_prefers_year_match():
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    old, new = _tpdb_row(), _tpdb_row()
    old["date"], old["synopsis"] = "2011-01-01", "old"
    new["date"] = "2023-05-14"
    with mock.patch.object(prov._session, "get") as get:
        get.return_value = mock.Mock(
            status_code=200, ok=True,
            json=lambda: {"data": [old, new]},
            raise_for_status=lambda: None,
        )
        meta = prov.fetch("Scene Title", "2023", SECTION_MOVIES)
    assert meta["year"] == "2023"


def test_tpdb_rejects_dissimilar_keyword_matches():
    """TPDB's `q` is a loose keyword search — a short query of ordinary words
    returns unrelated scenes. A wrong poster is worse than no poster."""
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    row = _tpdb_row()
    row["title"] = "Something Totally Unrelated"
    with mock.patch.object(prov._session, "get") as get:
        get.return_value = mock.Mock(
            status_code=200, ok=True,
            json=lambda: {"data": [row]},
            raise_for_status=lambda: None,
        )
        assert prov.fetch("Scene Title", "", SECTION_MOVIES) is None


def test_tpdb_trusts_jav_code_without_title_check():
    """A JAV code is an exact catalogue key, so the returned title need not
    resemble the (often scene-junk) playlist name."""
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    row = _tpdb_row()
    row["title"] = "Completely Different Japanese Title"
    with mock.patch.object(prov._session, "get") as get:
        get.return_value = mock.Mock(
            status_code=200, ok=True,
            json=lambda: {"data": [row]},
            raise_for_status=lambda: None,
        )
        meta = prov.fetch("Whatever", "", SECTION_MOVIES, raw_name="ABP-123 Whatever")
    assert meta is not None
    assert meta["poster"] == "http://cdn/large.jpg"


@pytest.mark.parametrize("a,b,floor", [
    ("Scene Title", "Scene Title", 0.9),
    ("Scene Title", "scene  title!", 0.9),
    ("Scene Title", "Something Totally Unrelated", 0.0),
])
def test_title_similarity(a, b, floor):
    from iptv.metadata import title_similarity
    score = title_similarity(a, b)
    assert score >= floor
    if floor == 0.0:
        assert score < 0.5


def test_title_containment_ignores_studio_prefix():
    """Playlist names are studio-led and far longer than the scene title, so
    plain similarity scores near zero — containment is the right test."""
    from iptv.metadata import title_containment, title_similarity
    name = "SomeStudio Riding Lessons Part Two 2160p Multi Sub Extended"
    assert title_containment(name, "Riding Lessons Part Two") == 1.0
    assert title_similarity(name, "Riding Lessons Part Two") < 0.7  # why containment exists
    assert title_containment(name, "Completely Different Scene") < 0.7


def test_site_stripped_similarity_uses_the_rows_own_site():
    """The row carries site.name, so the studio can be stripped for free."""
    from iptv.metadata import site_stripped_similarity
    row = {"title": "Riding Lessons", "site": {"name": "SomeStudio"}}
    assert site_stripped_similarity("SomeStudio Riding Lessons", row) >= 0.9
    row_bad = {"title": "Unrelated Thing", "site": {"name": "SomeStudio"}}
    assert site_stripped_similarity("SomeStudio Riding Lessons", row_bad) < 0.5


def test_site_stripped_similarity_survives_missing_site():
    from iptv.metadata import site_stripped_similarity
    assert site_stripped_similarity("Riding Lessons", {"title": "Riding Lessons"}) >= 0.9
    assert site_stripped_similarity("x", {"title": "", "site": None}) == 0.0


def test_tpdb_uses_parse_mode_for_studio_led_names():
    """A studio-led name must go through /scenes?parse=, not a title query."""
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    row = _tpdb_row()
    row["title"] = "Riding Lessons"
    row["site"] = {"name": "SomeStudio"}
    with mock.patch.object(prov._session, "get") as get:
        get.return_value = mock.Mock(
            status_code=200, ok=True,
            json=lambda: {"data": [row]},
            raise_for_status=lambda: None,
        )
        meta = prov.fetch("SomeStudio Riding Lessons", "", SECTION_MOVIES,
                          raw_name="SomeStudio Riding Lessons 1080p")
    assert meta is not None
    assert meta["poster"] == "http://cdn/large.jpg"
    first = get.call_args_list[0]
    assert first.args[0].endswith("/scenes")
    assert "parse" in first.kwargs["params"]


def test_tpdb_skips_keyword_fallback_when_parse_returned_rows():
    """Parse answering with rows that failed verification is a definitive
    answer — re-querying with the shorter studio-stripped title is the
    loose-keyword noise mode, so no /movies or /scenes search may fire."""
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    row = _tpdb_row()
    row["title"] = "Totally Unrelated Scene"
    row["site"] = {"name": "OtherStudio"}
    with mock.patch.object(prov._session, "get") as get:
        get.return_value = mock.Mock(
            status_code=200, ok=True,
            json=lambda: {"data": [row]},
            raise_for_status=lambda: None,
        )
        assert prov.fetch("SomeStudio Riding Lessons", "", SECTION_MOVIES,
                          raw_name="SomeStudio Riding Lessons") is None
    assert get.call_count == 1  # the parse request only


def test_tpdb_keyword_fallback_when_parse_returns_nothing():
    """Parse coming back EMPTY means the name isn't a recognizable scene
    filename — the plain keyword search still gets its chance."""
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    row = _tpdb_row()
    row["title"] = "Scene Title"
    empty = mock.Mock(status_code=200, ok=True,
                      json=lambda: {"data": []},
                      raise_for_status=lambda: None)
    hit = mock.Mock(status_code=200, ok=True,
                    json=lambda: {"data": [row]},
                    raise_for_status=lambda: None)
    with mock.patch.object(prov._session, "get", side_effect=[empty, hit]) as get:
        meta = prov.fetch("Scene Title", "", SECTION_MOVIES,
                          raw_name="some non scene shaped name")
    assert meta is not None
    assert get.call_count == 2
    fallback = get.call_args_list[1]
    assert fallback.args[0].endswith("/movies")
    assert fallback.kwargs["params"].get("q") == "Scene Title"


# ---------------------------------------------------------------------------
# Frame-grab poster fallback
# ---------------------------------------------------------------------------

def _grabber(tmp_path, write_bytes=b"\xff\xd8\xff-not-a-real-jpeg"):
    """FrameGrabber whose ffmpeg is faked to write a file (or not)."""
    from iptv.artwork import ArtworkCache
    from iptv.framegrab import FrameGrabber
    cache = ArtworkCache(str(tmp_path / "artwork"))
    g = FrameGrabber(cache, ffmpeg_path="ffmpeg")

    def fake_run(cmd, **kwargs):
        dest = cmd[-1]
        if write_bytes is not None:
            with open(dest, "wb") as fh:
                fh.write(write_bytes)
        return mock.Mock(returncode=0 if write_bytes is not None else 1,
                         stderr=b"", stdout=b"")

    return g, cache, fake_run


def test_synthetic_url_is_stable_and_identifiable():
    from iptv.framegrab import is_framegrab_url, synthetic_url
    a = synthetic_url("http://host/movie.mkv")
    assert a == synthetic_url("http://host/movie.mkv")
    assert a != synthetic_url("http://host/other.mkv")
    assert is_framegrab_url(a)
    assert not is_framegrab_url("http://cdn/poster.jpg")


def test_artwork_cache_default_thumb_size_is_larger(tmp_path):
    """The default thumbnail is big enough for the larger IPTV grid tiles."""
    from iptv.artwork import ArtworkCache
    cache = ArtworkCache(str(tmp_path))
    assert cache.thumb_size == (300, 450)
    assert "_300x450.webp" in cache.thumb_path("http://host/poster.jpg")


def test_artwork_cache_get_thumb_accepts_legacy_png(tmp_path):
    """Thumbs written before the WebP switch stay readable."""
    from iptv.artwork import ArtworkCache
    cache = ArtworkCache(str(tmp_path))
    url = "http://host/poster.jpg"
    legacy = cache._legacy_thumb_path(url)
    with open(legacy, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    assert cache.get_thumb(url) == legacy


def test_artwork_cache_stats_and_clear(tmp_path):
    """stats() counts the footprint; clear() wipes fulls, thumbs and sidecars."""
    from iptv.artwork import ArtworkCache
    cache = ArtworkCache(str(tmp_path))
    url = "http://host/poster.jpg"
    with open(cache.full_path(url), "wb") as f:
        f.write(b"x" * 1000)
    with open(cache.thumb_path(url), "wb") as f:
        f.write(b"x" * 500)
    with open(cache._meta_path(url), "w") as f:
        f.write("{}")
    s = cache.stats()
    assert s["full_bytes"] == 1002 and s["thumb_bytes"] == 500  # .meta sidecar counts
    assert s["total_bytes"] == 1502
    removed, freed = cache.clear()
    assert removed == 3 and freed == 1502
    assert cache.get_cached(url) is None and cache.get_thumb(url) is None
    assert cache.stats()["total_bytes"] == 0


def test_artwork_cache_enforce_size_limit_evicts_lru_groups(tmp_path):
    """Over the cap, the least-recently-used URL group (full + thumb + meta)
    is evicted first; a cache hit (atime bump) protects fresh entries."""
    from iptv.artwork import ArtworkCache
    cache = ArtworkCache(str(tmp_path))
    urls = [f"http://host/p{i}.jpg" for i in range(4)]
    for url in urls:
        for make in (cache.full_path, cache.thumb_path):
            with open(make(url), "wb") as f:
                f.write(b"x" * 1000)
    total = cache.stats()["total_bytes"]
    assert total == 8000
    # Touch p2/p3 so they are the most recently used.
    cache.get_cached(urls[2])
    cache.get_thumb(urls[3])
    freed = cache.enforce_size_limit(5000)  # must evict 2 oldest groups
    assert freed == 4000
    assert cache.get_cached(urls[0]) is None and cache.get_cached(urls[1]) is None
    assert cache.get_thumb(urls[0]) is None and cache.get_thumb(urls[1]) is None
    assert cache.get_cached(urls[2]) is not None and cache.get_thumb(urls[3]) is not None
    # Under the cap: a no-op.
    assert cache.enforce_size_limit(10 ** 9) == 0


def test_framegrab_writes_into_the_artwork_cache(tmp_path):
    """The frame must land where ArtworkCache.get_cached will find it, so the
    normal artwork path serves it with no special-casing."""
    g, cache, fake_run = _grabber(tmp_path)
    with mock.patch("iptv.framegrab.subprocess.run", side_effect=fake_run):
        url = g.grab("http://host/movie.mkv")
    assert url.startswith("framegrab:")
    assert cache.get_cached(url), "grabbed frame is not visible to the artwork cache"


def test_framegrab_returns_cached_without_respawning_ffmpeg(tmp_path):
    g, cache, fake_run = _grabber(tmp_path)
    with mock.patch("iptv.framegrab.subprocess.run", side_effect=fake_run) as run:
        g.grab("http://host/movie.mkv")
        assert run.call_count >= 1
        run.reset_mock()
        g.grab("http://host/movie.mkv")
        assert run.call_count == 0


def test_framegrab_failure_is_not_retried(tmp_path):
    """A dead stream must be attempted once per session, not on every scroll."""
    g, _cache, fake_run = _grabber(tmp_path, write_bytes=None)
    with mock.patch("iptv.framegrab.subprocess.run", side_effect=fake_run) as run:
        assert g.grab("http://host/dead.mkv") == ""
        first = run.call_count
        assert first == 2  # both seek offsets tried
        assert g.grab("http://host/dead.mkv") == ""
        assert run.call_count == first  # no further attempts


def test_framegrab_falls_back_to_earlier_seek(tmp_path):
    """A clip shorter than the 120s seek must still yield a frame."""
    from iptv.artwork import ArtworkCache
    from iptv.framegrab import FrameGrabber
    cache = ArtworkCache(str(tmp_path / "artwork"))
    g = FrameGrabber(cache, ffmpeg_path="ffmpeg")
    seeks = []

    def fake_run(cmd, **kwargs):
        seek = int(cmd[cmd.index("-ss") + 1])
        seeks.append(seek)
        if seek == 120:
            return mock.Mock(returncode=1, stderr=b"", stdout=b"")
        with open(cmd[-1], "wb") as fh:
            fh.write(b"\xff\xd8\xff-frame")
        return mock.Mock(returncode=0, stderr=b"", stdout=b"")

    with mock.patch("iptv.framegrab.subprocess.run", side_effect=fake_run):
        url = g.grab("http://host/short.mkv")
    assert seeks == [120, 8]
    assert url and cache.get_cached(url)


def test_framegrab_retries_transient_reset_and_keeps_it_retryable(tmp_path):
    """CDN resets say nothing about whether content exists (measured live:
    HEAD 200 / GET 206 while ffmpeg's TLS handshake was reset). Memoizing one
    as a failure would blank a grabbable tile forever."""
    from iptv.artwork import ArtworkCache
    from iptv.framegrab import FrameGrabber, synthetic_url
    g = FrameGrabber(ArtworkCache(str(tmp_path / "a")), ffmpeg_path="ffmpeg")
    reset = mock.Mock(returncode=1, stdout=b"",
                      stderr=b"[tls] Failed to read handshake response\n"
                             b"Error number -10054 occurred")
    with mock.patch("iptv.framegrab.subprocess.run", return_value=reset) as run:
        with mock.patch("iptv.framegrab.time.sleep"):
            assert g.grab("http://host/flaky.mkv") == ""
    # Both seeks, both passes.
    assert run.call_count == 4
    assert synthetic_url("http://host/flaky.mkv") not in g._failed

    # A later attempt succeeds once the CDN behaves.
    def ok_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as fh:
            fh.write(b"\xff\xd8\xff-frame")
        return mock.Mock(returncode=0, stderr=b"", stdout=b"")

    with mock.patch("iptv.framegrab.subprocess.run", side_effect=ok_run):
        assert g.grab("http://host/flaky.mkv").startswith("framegrab:")


def test_framegrab_permanent_error_is_not_retried(tmp_path):
    """A genuine content error should be remembered, unlike a reset."""
    from iptv.artwork import ArtworkCache
    from iptv.framegrab import FrameGrabber, synthetic_url
    g = FrameGrabber(ArtworkCache(str(tmp_path / "a")), ffmpeg_path="ffmpeg")
    dead = mock.Mock(returncode=1, stdout=b"",
                     stderr=b"Server returned 404 Not Found")
    with mock.patch("iptv.framegrab.subprocess.run", return_value=dead) as run:
        assert g.grab("http://host/gone.mkv") == ""
    assert run.call_count == 2       # both seeks, no second pass
    assert synthetic_url("http://host/gone.mkv") in g._failed


def test_framegrab_timeout_is_survivable(tmp_path):
    import subprocess as _sp
    g, _cache, _ = _grabber(tmp_path)
    with mock.patch("iptv.framegrab.subprocess.run",
                    side_effect=_sp.TimeoutExpired("ffmpeg", 25)):
        assert g.grab("http://host/hang.mkv") == ""


def test_framegrab_noop_without_ffmpeg(tmp_path):
    from iptv.artwork import ArtworkCache
    from iptv.framegrab import FrameGrabber
    g = FrameGrabber(ArtworkCache(str(tmp_path / "a")), ffmpeg_path="")
    with mock.patch("iptv.framegrab.find_ffmpeg", create=True, return_value=""):
        with mock.patch("dlmgr.ffmpeg.find_ffmpeg", return_value=""):
            assert g.available is False
            assert g.grab("http://host/movie.mkv") == ""


def test_framegrab_uses_no_window_flag(tmp_path):
    """Windows: a console window must not flash in the windowed build."""
    import subprocess as _sp
    g, _cache, fake_run = _grabber(tmp_path)
    with mock.patch("iptv.framegrab.subprocess.run", side_effect=fake_run) as run:
        g.grab("http://host/movie.mkv")
    assert "creationflags" in run.call_args.kwargs
    assert run.call_args.kwargs["creationflags"] == getattr(_sp, "CREATE_NO_WINDOW", 0)


def test_framegrab_retries_without_unsupported_reconnect_options(tmp_path):
    g, cache, _ = _grabber(tmp_path)
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if "-reconnect" in cmd:
            return mock.Mock(returncode=1, stderr=b"Option reconnect not found.", stdout=b"")
        with open(cmd[-1], "wb") as fh:
            fh.write(b"\xff\xd8\xff-frame")
        return mock.Mock(returncode=0, stderr=b"", stdout=b"")

    with mock.patch("iptv.framegrab.subprocess.run", side_effect=fake_run):
        url = g.grab("http://host/redirected.mp4")
    assert url and cache.get_cached(url)
    assert len(commands) == 2
    assert "-reconnect" in commands[0]
    assert "-reconnect" not in commands[1]


def test_poster_fallback_only_when_allowed(tmp_path):
    """The 48k-entry background sweep must never open video connections."""
    mgr = IPTVManager(sources=[], tmdb_api_key="", data_dir=str(tmp_path))
    mgr.metadata.resolve_async = lambda section, name, year, cb, group="": cb("k", {})
    mgr.framegrab = mock.Mock()
    item = Movie(id="m1", name="Some Movie", url="http://host/m.mkv",
                 section=SECTION_MOVIES)

    done = threading.Event()
    mgr.resolve_poster_async(item, lambda i, u: done.set(), allow_framegrab=False)
    assert done.wait(10)
    assert not mgr.framegrab.grab_async.called, "sweep triggered a frame grab"

    done2 = threading.Event()
    mgr.framegrab.grab_async.side_effect = lambda url, cb: cb("framegrab:abc")
    got = {}
    mgr.resolve_poster_async(item, lambda i, u: (got.update(url=u), done2.set()),
                             allow_framegrab=True)
    assert done2.wait(10)
    assert mgr.framegrab.grab_async.called
    assert got["url"] == "framegrab:abc"
    mgr.shutdown()


def test_poster_fallback_skipped_when_provider_matched(tmp_path):
    """A real poster must win — no reason to open the stream."""
    mgr = IPTVManager(sources=[], tmdb_api_key="", data_dir=str(tmp_path))
    mgr.metadata.resolve_async = (
        lambda section, name, year, cb, group="": cb("k", {"poster": "http://cdn/p.jpg"}))
    mgr.framegrab = mock.Mock()
    item = Movie(id="m1", name="Some Movie", url="http://host/m.mkv",
                 section=SECTION_MOVIES)
    got = {}
    done = threading.Event()
    mgr.resolve_poster_async(item, lambda i, u: (got.update(url=u), done.set()),
                             allow_framegrab=True)
    assert done.wait(10)
    assert got["url"] == "http://cdn/p.jpg"
    assert not mgr.framegrab.grab_async.called
    mgr.shutdown()


def test_framegrab_disabled_by_config(tmp_path):
    mgr = IPTVManager(sources=[], data_dir=str(tmp_path), framegrab_posters=False)
    assert mgr.framegrab is None
    mgr.metadata.resolve_async = lambda section, name, year, cb, group="": cb("k", {})
    item = Movie(id="m1", name="X", url="http://host/m.mkv", section=SECTION_MOVIES)
    got = {}
    done = threading.Event()
    mgr.resolve_poster_async(item, lambda i, u: (got.update(url=u), done.set()),
                             allow_framegrab=True)
    assert done.wait(10)
    assert got["url"] == ""
    mgr.shutdown()


def test_tpdb_parse_rejects_unverified_rows():
    """Parse returns loose candidates — an unrelated row must not be used."""
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    row = _tpdb_row()
    row["title"] = "Totally Unrelated Scene"
    row["site"] = {"name": "OtherStudio"}
    with mock.patch.object(prov._session, "get") as get:
        get.return_value = mock.Mock(
            status_code=200, ok=True,
            json=lambda: {"data": [row]},
            raise_for_status=lambda: None,
        )
        assert prov.fetch("SomeStudio Riding Lessons", "", SECTION_MOVIES,
                          raw_name="SomeStudio Riding Lessons") is None


def test_tpdb_queries_jav_endpoint_first_for_a_code():
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    with mock.patch.object(prov._session, "get") as get:
        get.return_value = mock.Mock(
            status_code=200, ok=True,
            json=lambda: {"data": [_tpdb_row()]},
            raise_for_status=lambda: None,
        )
        prov.fetch("Some Title", "", SECTION_MOVIES, raw_name="ABP-123 Some Title")
    url = get.call_args_list[0].args[0]
    assert url.endswith("/jav")
    assert get.call_args_list[0].kwargs["params"]["q"] == "ABP-123"


def test_tpdb_401_returns_none_and_does_not_raise():
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("bad-token")
    with mock.patch.object(prov._session, "get") as get:
        get.return_value = mock.Mock(status_code=401, ok=False)
        assert prov.fetch("Scene Title", "", SECTION_MOVIES) is None


def test_tpdb_retries_once_on_connection_reset():
    """The API resets connections under rapid-fire requests."""
    import requests as _requests
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    ok = mock.Mock(status_code=200, ok=True,
                   json=lambda: {"data": [_tpdb_row()]},
                   raise_for_status=lambda: None)
    with mock.patch.object(prov._session, "get",
                           side_effect=[_requests.ConnectionError("reset"), ok]) as get:
        with mock.patch("iptv.metadata.time.sleep"):
            meta = prov.fetch("Scene Title", "", SECTION_MOVIES)
    assert get.call_count == 2
    assert meta["title"] == "Scene Title"


def test_pipeline_routes_adult_entries_to_tpdb(tmp_path):
    """Adult VOD must hit TPDB, not TMDb (which filters adult titles out)."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb", tpdb_api_key="tpdb")
    pipe.tmdb = mock.Mock(**{"fetch.return_value": None})
    pipe.tpdb = mock.Mock(**{"fetch.return_value": {
        "title": "S", "year": "2023", "rating": 0, "synopsis": "",
        "genres": [], "poster": "http://cdn/p.jpg", "backdrop": "",
        "provider": "tpdb",
    }})
    done = threading.Event()
    got = {}

    def _on_done(key, meta):
        got.update(meta or {})
        done.set()

    pipe.resolve_async(SECTION_MOVIES, "Some Scene", "2023", _on_done, group="XXX")
    assert done.wait(10)
    assert got.get("provider") == "tpdb"
    assert pipe.tpdb.fetch.called
    pipe.shutdown()


def test_pipeline_leaves_normal_movies_on_tmdb(tmp_path):
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb", tpdb_api_key="tpdb")
    pipe.tmdb = mock.Mock(**{"fetch.return_value": {
        "title": "M", "year": "2023", "rating": 0, "synopsis": "",
        "genres": [], "poster": "http://cdn/m.jpg", "backdrop": "",
        "provider": "tmdb",
    }})
    pipe.tpdb = mock.Mock()
    done = threading.Event()
    got = {}

    def _on_done(key, meta):
        got.update(meta or {})
        done.set()

    pipe.resolve_async(SECTION_MOVIES, "Regular Movie", "2023", _on_done, group="Movies")
    assert done.wait(10)
    assert got.get("provider") == "tmdb"
    assert not pipe.tpdb.fetch.called
    pipe.shutdown()


def test_pipeline_year_goes_in_the_param_not_the_query(tmp_path):
    """Verified live against TMDb: "Dunki 2023" as query text returns ZERO
    results; "Dunki" + year=2023 hits. Scene filenames carry bare years and
    only release junk after them, so the worker must cut the query at the
    year and pass the year separately."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb")
    pipe.tmdb = mock.Mock(**{"fetch.return_value": {
        "title": "Dunki", "year": "2023", "rating": 0, "synopsis": "",
        "genres": [], "poster": "http://cdn/m.jpg", "backdrop": "",
        "provider": "tmdb",
    }})
    done = threading.Event()
    pipe.resolve_async(SECTION_MOVIES,
                       "Dunki.2023.1080p.WEBRip.x264.AAC5.1-WORLD", "",
                       lambda k, m: done.set())
    assert done.wait(10)
    assert pipe.tmdb.fetch.call_args[0] == ("Dunki", "2023", SECTION_MOVIES)
    pipe.shutdown()

    # No year in the name -> query stays the full cleaned title, year ''.
    pipe2 = MetadataPipeline(IPTVCache(str(tmp_path / "b")), tmdb_api_key="tmdb")
    pipe2.tmdb = mock.Mock(**{"fetch.return_value": None})
    done2 = threading.Event()
    pipe2.resolve_async(SECTION_MOVIES, "Some.Title.1080p.WEBRip", "",
                        lambda k, m: done2.set())
    assert done2.wait(10)
    assert pipe2.tmdb.fetch.call_args[0] == ("Some Title", "", SECTION_MOVIES)
    pipe2.shutdown()


# ---------------------------------------------------------------------------
# Phase 1: shared retry layer + provider-chain refactor
# ---------------------------------------------------------------------------

def test_retry_request_retries_on_503_then_succeeds():
    """A 503 is transient — the helper must retry with backoff and succeed."""
    import requests as _requests
    from iptv.metadata import _retry_request
    session = _requests.Session()
    ok = mock.Mock(status_code=200, json=lambda: {"ok": True})
    ok.close = mock.Mock()
    with mock.patch.object(session, "get",
                           side_effect=[mock.Mock(status_code=503, close=mock.Mock()), ok]) as get:
        with mock.patch("iptv.metadata.time.sleep"):
            data, status = _retry_request(session, "http://x/api")
    assert status == "ok"
    assert data == {"ok": True}
    assert get.call_count == 2


def test_retry_request_bails_immediately_on_404():
    """A 404 is a permanent 'no match', not an error — no wasted retries."""
    from iptv.metadata import _retry_request
    session = requests.Session()
    with mock.patch.object(session, "get",
                           return_value=mock.Mock(status_code=404, close=mock.Mock())) as get:
        with mock.patch("iptv.metadata.time.sleep") as sleep:
            data, status = _retry_request(session, "http://x/api")
    assert status == "not_found"
    assert data is None
    assert get.call_count == 1
    assert not sleep.called


def test_retry_request_bails_immediately_on_401():
    """A 401 is a permanent auth failure — no retries."""
    from iptv.metadata import _retry_request
    session = requests.Session()
    with mock.patch.object(session, "get",
                           return_value=mock.Mock(status_code=401, close=mock.Mock())) as get:
        with mock.patch("iptv.metadata.time.sleep") as sleep:
            data, status = _retry_request(session, "http://x/api")
    assert status == "auth"
    assert data is None
    assert get.call_count == 1
    assert not sleep.called


def test_retry_request_exhausts_retries_on_persistent_503():
    """When every attempt returns 503, the helper exhausts and reports transient."""
    from iptv.metadata import _retry_request, _DEFAULT_MAX_ATTEMPTS
    session = requests.Session()
    with mock.patch.object(session, "get",
                           return_value=mock.Mock(status_code=503, close=mock.Mock())) as get:
        with mock.patch("iptv.metadata.time.sleep"):
            data, status = _retry_request(session, "http://x/api")
    assert status == "transient"
    assert data is None
    assert get.call_count == _DEFAULT_MAX_ATTEMPTS


def test_retry_request_retries_on_connection_error():
    """A ConnectionError is always transient (network-level), not permanent."""
    import requests as _requests
    from iptv.metadata import _retry_request
    session = _requests.Session()
    ok = mock.Mock(status_code=200, json=lambda: {"ok": True})
    ok.close = mock.Mock()
    with mock.patch.object(session, "get",
                           side_effect=[_requests.ConnectionError("reset"), ok]):
        with mock.patch("iptv.metadata.time.sleep"):
            data, status = _retry_request(session, "http://x/api")
    assert status == "ok"
    assert data == {"ok": True}


def test_retry_request_uses_jittered_backoff():
    """Backoff must be jittered (0.5 + random multiplier) so a burst doesn't
    return in lockstep — the same lesson the artwork fetcher learned."""
    from iptv.metadata import _retry_request, _DEFAULT_BACKOFF_BASE, _DEFAULT_MAX_ATTEMPTS
    session = requests.Session()
    with mock.patch.object(session, "get",
                           return_value=mock.Mock(status_code=503, close=mock.Mock())):
        with mock.patch("iptv.metadata.time.sleep") as sleep, \
             mock.patch("iptv.metadata.random.random", return_value=0.5):
            _retry_request(session, "http://x/api")
    # 3 sleeps for 4 attempts (last attempt doesn't sleep).
    assert sleep.call_count == _DEFAULT_MAX_ATTEMPTS - 1
    # First backoff: base * 2^0 * (0.5 + 0.5) = base * 1.0
    assert sleep.call_args_list[0][0][0] == _DEFAULT_BACKOFF_BASE * 1.0


def test_tmdb_retries_on_503_for_search():
    """TMDb search must retry on 503 (previously zero retries)."""
    from iptv.metadata import TMDBProvider
    prov = TMDBProvider("key")
    ok = mock.Mock(status_code=200, json=lambda: {"results": [
        {"id": 1, "title": "Movie", "release_date": "2023-01-01", "poster_path": "/p.jpg"}
    ]})
    ok.close = mock.Mock()
    with mock.patch.object(prov._session, "get",
                           side_effect=[mock.Mock(status_code=503, close=mock.Mock()), ok]):
        with mock.patch("iptv.metadata.time.sleep"):
            meta = prov.fetch("Movie", "2023", SECTION_MOVIES)
    assert meta is not None
    assert meta["title"] == "Movie"


def test_tvmaze_retries_on_connection_reset():
    """TVmaze must retry on ConnectionError (previously zero retries)."""
    import requests as _requests
    from iptv.metadata import TVMazeProvider
    prov = TVMazeProvider()
    ok = mock.Mock(status_code=200, json=lambda: {
        "name": "Show", "premiered": "2020-01-01", "image": {"original": "http://x/p.jpg"},
        "summary": "", "genres": [], "rating": {"average": 8},
    })
    ok.close = mock.Mock()
    with mock.patch.object(prov._session, "get",
                           side_effect=[_requests.ConnectionError("reset"), ok]):
        with mock.patch("iptv.metadata.time.sleep"):
            meta = prov.fetch("Show", "", SECTION_SERIES)
    assert meta is not None
    assert meta["title"] == "Show"


def test_tpdb_retries_on_503_not_just_connection_reset():
    """TPDB previously only retried ConnectionError — now 503 is retried too."""
    from iptv.metadata import TPDBProvider
    prov = TPDBProvider("token")
    ok = mock.Mock(status_code=200, json=lambda: {"data": [_tpdb_row()]})
    ok.close = mock.Mock()
    with mock.patch.object(prov._session, "get",
                           side_effect=[mock.Mock(status_code=503, close=mock.Mock()), ok]):
        with mock.patch("iptv.metadata.time.sleep"):
            meta = prov.fetch("Scene Title", "", SECTION_MOVIES)
    assert meta is not None
    assert meta["title"] == "Scene Title"


def test_pipeline_chain_adult_goes_tpdb_then_tmdb(tmp_path):
    """Adult entries try TPDB first, then fall through to TMDb if TPDB misses."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb", tpdb_api_key="tpdb")
    pipe.tpdb = mock.Mock(**{"fetch.return_value": None})
    pipe.tmdb = mock.Mock(**{"fetch.return_value": {
        "title": "M", "year": "2023", "rating": 0, "synopsis": "",
        "genres": [], "poster": "http://cdn/m.jpg", "backdrop": "",
        "provider": "tmdb",
    }})
    done = threading.Event()
    got = {}
    def _on_done(key, meta):
        got.update(meta or {})
        done.set()
    pipe.resolve_async(SECTION_MOVIES, "Some Scene", "2023", _on_done, group="XXX")
    assert done.wait(10)
    assert got.get("provider") == "tmdb"
    assert pipe.tpdb.fetch.called
    assert pipe.tmdb.fetch.called
    pipe.shutdown()


def test_pipeline_chain_series_goes_tmdb_then_tvmaze(tmp_path):
    """Series try TMDb first, then TVMaze fallback if TMDb misses."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb")
    pipe.tmdb = mock.Mock(**{"fetch.return_value": None})
    pipe.tvmaze = mock.Mock(**{"fetch.return_value": {
        "title": "S", "year": "2020", "rating": 0, "synopsis": "",
        "genres": [], "poster": "http://cdn/s.jpg", "backdrop": "",
        "provider": "tvmaze",
    }})
    done = threading.Event()
    got = {}
    def _on_done(key, meta):
        got.update(meta or {})
        done.set()
    pipe.resolve_async(SECTION_SERIES, "Some Show", "", _on_done, group="Series")
    assert done.wait(10)
    assert got.get("provider") == "tvmaze"
    assert pipe.tmdb.fetch.called
    assert pipe.tvmaze.fetch.called
    pipe.shutdown()


def test_pipeline_chain_first_provider_hit_skips_rest(tmp_path):
    """When the first provider in the chain returns a poster, later ones are
    not called — first-art-wins."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb")
    pipe.tmdb = mock.Mock(**{"fetch.return_value": {
        "title": "M", "year": "2023", "poster": "http://cdn/m.jpg",
        "backdrop": "", "provider": "tmdb", "genres": [], "rating": 0, "synopsis": "",
    }})
    pipe.tvmaze = mock.Mock()
    done = threading.Event()
    pipe.resolve_async(SECTION_SERIES, "Some Show", "2023",
                       lambda k, m: done.set())
    assert done.wait(10)
    assert pipe.tmdb.fetch.called
    assert not pipe.tvmaze.fetch.called
    pipe.shutdown()


def test_pipeline_per_provider_negative_cache_skips_missed_provider(tmp_path):
    """A TPDB miss is recorded per-provider — on the next visit TPDB is
    skipped but a newly-added TMDb (not in the miss list) is still tried.
    This is the core value of per-provider negative caching: a new provider
    added to the chain gets tried even when others already missed."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    # First visit: only TPDB configured (no TMDb key) — TPDB misses.
    pipe = MetadataPipeline(cache, tpdb_api_key="tpdb")
    pipe.tpdb = mock.Mock(**{"fetch.return_value": None})
    done1 = threading.Event()
    pipe.resolve_async(SECTION_MOVIES, "Unknown Scene", "2023",
                       lambda k, m: done1.set(), group="XXX")
    assert done1.wait(10)
    pipe.shutdown()

    # Second visit: TMDb key now configured. TPDB should be skipped (recent
    # miss), but TMDb — which wasn't in the chain last time — gets tried.
    pipe2 = MetadataPipeline(cache, tmdb_api_key="tmdb", tpdb_api_key="tpdb")
    pipe2.tpdb = mock.Mock(**{"fetch.return_value": {
        "title": "S", "poster": "http://cdn/s.jpg", "backdrop": "",
        "provider": "tpdb", "genres": [], "rating": 0, "synopsis": "", "year": "2023",
    }})
    pipe2.tmdb = mock.Mock(**{"fetch.return_value": {
        "title": "M", "poster": "http://cdn/m.jpg", "backdrop": "",
        "provider": "tmdb", "genres": [], "rating": 0, "synopsis": "", "year": "2023",
    }})
    done2 = threading.Event()
    got = {}
    def _on_done(key, meta):
        got.update(meta or {})
        done2.set()
    pipe2.resolve_async(SECTION_MOVIES, "Unknown Scene", "2023",
                        _on_done, group="XXX")
    assert done2.wait(10)
    # TPDB was skipped (recent miss), TMDb was tried and hit.
    assert not pipe2.tpdb.fetch.called
    assert pipe2.tmdb.fetch.called
    assert got.get("provider") == "tmdb"
    pipe2.shutdown()


def test_pipeline_per_provider_negative_cache_all_missed_short_circuits(tmp_path):
    """When every provider in the chain has a recent miss, resolve_async
    returns empty immediately without calling any provider."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb")
    pipe.tmdb = mock.Mock(**{"fetch.return_value": None})
    done1 = threading.Event()
    pipe.resolve_async(SECTION_MOVIES, "Unknown Movie", "2023",
                       lambda k, m: done1.set())
    assert done1.wait(10)
    pipe.shutdown()

    # Second visit: TMDb has a recent miss, no other providers for movies.
    # resolve_async should short-circuit without calling TMDb again.
    pipe2 = MetadataPipeline(cache, tmdb_api_key="tmdb")
    pipe2.tmdb = mock.Mock()
    done2 = threading.Event()
    pipe2.resolve_async(SECTION_MOVIES, "Unknown Movie", "2023",
                        lambda k, m: done2.set())
    assert done2.wait(10)
    assert not pipe2.tmdb.fetch.called
    pipe2.shutdown()


def test_pipeline_legacy_negative_cache_still_works(tmp_path):
    """A negative cache entry written before the per-provider refactor (no
    ``provider_misses`` field) must still short-circuit within the TTL."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    # Write a legacy-style negative record directly.
    key = metadata_key(SECTION_MOVIES, "Old Movie", "2023")
    cache.save_metadata(key, SECTION_MOVIES, "Old Movie", "2023", "none",
                        {"negative": True, "updated_at": time.time()})
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb")
    pipe.tmdb = mock.Mock()
    done = threading.Event()
    pipe.resolve_async(SECTION_MOVIES, "Old Movie", "2023",
                       lambda k, m: done.set())
    assert done.wait(10)
    assert not pipe.tmdb.fetch.called
    pipe.shutdown()


# ---------------------------------------------------------------------------
# Phase 2: JAV code extraction + JAV provider chain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_code,expected_censored", [
    # Basic codes (existing behaviour preserved).
    ("ABP-123", "ABP-123", True),
    ("abp00123", "ABP-123", True),
    ("SSNI 456 1080p", "SSNI-456", True),
    # Suffix stripping: -c, -leak, _5 (part 5), -hd.
    ("ABP-123-c", "ABP-123", True),
    ("SSIS-456.leak", "SSIS-456", True),
    ("abp00123_5", "ABP-123", True),
    ("SSNI-456-hd", "SSNI-456", True),
    # Uncensored codes.
    ("HEYZO-1234", "HEYZO-1234", False),
    ("carib-123", "CARIB-123", False),
    # Non-JAV (year only, lowercase "some 123").
    ("Regular Movie 2023", "", True),
])
def test_jav_code_info(raw, expected_code, expected_censored):
    from iptv.metadata import jav_code_info
    info = jav_code_info(raw)
    if expected_code:
        assert info is not None
        assert info[0] == expected_code
        assert info[1] == expected_censored
    else:
        # Non-JAV names return None (no code found).
        assert info is None


def test_jav_code_strips_censored_suffix():
    """"-c" is a censored marker suffix — it must be stripped before lookup."""
    from iptv.metadata import jav_code
    assert jav_code("ABP-123-c") == "ABP-123"


def test_jav_code_strips_leak_suffix():
    """"-leak" is a leaked-release suffix — stripped so the code matches the
    catalogue key."""
    from iptv.metadata import jav_code
    assert jav_code("SSIS-456.leak") == "SSIS-456"


def test_jav_code_strips_part_number():
    """A trailing part number ("_5" = part 5) is stripped — the cover is the
    same across parts."""
    from iptv.metadata import jav_code
    assert jav_code("abp00123_5") == "ABP-123"


def test_jav_code_info_uncensored_routing():
    """Uncensored prefixes (heyzo, carib) are classified as is_censored=False
    so FANZA (censored-only) skips them and the chain falls through to
    JavBus/JavLibrary."""
    from iptv.metadata import jav_code_info
    assert jav_code_info("HEYZO-1234") == ("HEYZO-1234", False)
    assert jav_code_info("CARIB-123") == ("CARIB-123", False)


def test_jav_code_info_censored_default():
    """Unknown prefixes default to censored (the common case for JAV)."""
    from iptv.metadata import jav_code_info
    # "XYZ" is not in either prefix set -> defaults to censored.
    info = jav_code_info("XYZ-456")
    assert info is not None
    assert info[1] is True


# -- JavBus provider --

JAVBUS_HTML = """
<html>
<head><title>ABP-123 Some Title - JavBus</title></head>
<body>
  <a class="bigImage" href="https://pics.dmm.co.jp/abp00123/abp00123pl.jpg">
    <img src="https://pics.dmm.co.jp/abp00123/abp00123pl.jpg" alt="Some Title">
  </a>
  <div class="col-md-3">
    <p><span>製作商:</span></p>
    <p><a href="/studio/s1">S1 NO.1 STYLE</a></p>
    <p>2023-05-14</p>
  </div>
  <div class="star-name"><a href="/star/1">Yua Mikami</a></div>
  <div class="star-name"><a href="/star/2">Actress 2</a></div>
  <div id="sample-waterfall">
    <a href="https://pics.dmm.co.jp/abp00123/abp00123jp-1.jpg"><img src="thumb.jpg"></a>
  </div>
</body>
</html>
"""


def test_javbus_parses_cover_and_metadata():
    from iptv.metadata import JavBusProvider
    prov = JavBusProvider()
    meta = prov._parse(JAVBUS_HTML, "ABP-123")
    assert meta is not None
    assert meta["poster"] == "https://pics.dmm.co.jp/abp00123/abp00123pl.jpg"
    assert meta["title"] == "Some Title"
    assert meta["year"] == "2023"
    assert "Yua Mikami" in meta["genres"]
    assert meta["backdrop"] == "https://pics.dmm.co.jp/abp00123/abp00123jp-1.jpg"
    assert meta["provider"] == "javbus"


def test_javbus_returns_none_without_cover():
    """No cover image = not a real match — return None so the chain continues."""
    from iptv.metadata import JavBusProvider
    prov = JavBusProvider()
    html = "<html><body>No cover here</body></html>"
    assert prov._parse(html, "ABP-123") is None


def test_javbus_relative_cover_url_is_absolutized():
    """Relative cover URLs (/pics/...) are prefixed with the javbus domain."""
    from iptv.metadata import JavBusProvider
    prov = JavBusProvider()
    html = '<a class="bigImage" href="/pics/cover/x.jpg"><img></a>'
    meta = prov._parse(html, "ABP-123")
    assert meta is not None
    assert meta["poster"] == "https://www.javbus.com/pics/cover/x.jpg"


def test_javbus_fetch_uses_jav_code_and_retries():
    """JavBus fetch extracts the code from raw_name and retries on transient
    failures."""
    from iptv.metadata import JavBusProvider
    prov = JavBusProvider()
    ok_html = mock.Mock(text=JAVBUS_HTML, status_code=200)
    with mock.patch.object(prov._session, "get",
                           side_effect=[requests.ConnectionError("reset"), ok_html]):
        with mock.patch("iptv.metadata.time.sleep"):
            with mock.patch("iptv.metadata._jav_rate_limiter"):
                meta = prov.fetch("", "", SECTION_MOVIES, raw_name="ABP-123")
    assert meta is not None
    assert meta["poster"] == "https://pics.dmm.co.jp/abp00123/abp00123pl.jpg"


def test_javbus_fetch_returns_none_for_non_jav():
    """No JAV code in the name -> None (chain falls through to other providers)."""
    from iptv.metadata import JavBusProvider
    prov = JavBusProvider()
    assert prov.fetch("", "", SECTION_MOVIES, raw_name="Regular Movie 2023") is None


# -- JavLibrary provider --

JAVLIBRARY_HTML = """
<html>
<head><title>ABP-123 Some Japanese Title - JAVLibrary</title></head>
<body>
  <img id="video_jacket_img" src="https://pics.dmm.co.jp/abp/abp00123pl.jpg">
  <div class="videoinfo">
    <a href="vl_star.php?mode=2&star=1">Yua Mikami</a>
    <a href="vl_maker.php?mode=2&maker=1">S1 NO.1 STYLE</a>
    2023-05-14
  </div>
</body>
</html>
"""


def test_javlibrary_parses_cover_and_metadata():
    from iptv.metadata import JavLibraryProvider
    prov = JavLibraryProvider()
    meta = prov._parse(JAVLIBRARY_HTML, "ABP-123")
    assert meta is not None
    assert meta["poster"] == "https://pics.dmm.co.jp/abp/abp00123pl.jpg"
    assert meta["title"] == "Some Japanese Title"
    assert meta["year"] == "2023"
    assert "Yua Mikami" in meta["genres"]
    assert meta["provider"] == "javlibrary"


def test_javlibrary_returns_none_without_cover():
    from iptv.metadata import JavLibraryProvider
    prov = JavLibraryProvider()
    html = "<html><body>No jacket image</body></html>"
    assert prov._parse(html, "ABP-123") is None


def test_javlibrary_fetch_tries_multiple_bases():
    """When the first base (en) returns 404, the scraper tries the next (cn)."""
    from iptv.metadata import JavLibraryProvider
    prov = JavLibraryProvider()
    ok = mock.Mock(text=JAVLIBRARY_HTML, status_code=200)
    with mock.patch.object(prov._session, "get",
                           side_effect=[mock.Mock(status_code=404, close=mock.Mock()), ok]):
        with mock.patch("iptv.metadata.time.sleep"):
            with mock.patch("iptv.metadata._jav_rate_limiter"):
                meta = prov.fetch("", "", SECTION_MOVIES, raw_name="ABP-123")
    assert meta is not None
    assert meta["poster"] == "https://pics.dmm.co.jp/abp/abp00123pl.jpg"


# -- FANZA provider --

def test_fanza_predicts_cover_url_for_censored():
    """FANZA cover URLs follow a predictable pattern — the provider returns a
    poster even when the HTML scrape fails (zero-parse poster fallback)."""
    from iptv.metadata import FanzaProvider
    prov = FanzaProvider()
    # Mock the HTML fetch to return empty (simulates a failed scrape).
    with mock.patch.object(prov, "_fetch_html", return_value=""):
        with mock.patch("iptv.metadata._jav_rate_limiter"):
            meta = prov.fetch("", "", SECTION_MOVIES, raw_name="ABP-123")
    assert meta is not None
    # Cover URL: https://pics.dmm.co.jp/digital/video/abp00123/abp00123pl.jpg
    assert "abp00123" in meta["poster"]
    assert meta["poster"].endswith("pl.jpg")
    assert meta["provider"] == "fanza"


def test_fanza_skips_uncensored_codes():
    """FANZA only carries censored JAV — uncensored codes return None so the
    chain falls through to JavBus/JavLibrary."""
    from iptv.metadata import FanzaProvider
    prov = FanzaProvider()
    with mock.patch("iptv.metadata._jav_rate_limiter"):
        meta = prov.fetch("", "", SECTION_MOVIES, raw_name="HEYZO-1234")
    assert meta is None


def test_fanza_parses_detail_page():
    """When the HTML scrape succeeds, full metadata (title, actresses, studio)
    is extracted from the FANZA detail page."""
    from iptv.metadata import FanzaProvider
    prov = FanzaProvider()
    fanza_html = """
    <html><head><title>ABP-123 Some Title - FANZA Digital</title></head>
    <body>
      <a class="floatleft" href="https://pics.dmm.co.jp/digital/video/abp00123/abp00123pl.jpg">
        <img src="https://pics.dmm.co.jp/digital/video/abp00123/abp00123pl.jpg">
      </a>
      <a href="/digital/videoa/-/list/=/article=actress/id=1/">Yua Mikami</a>
      <a href="/digital/videoa/-/list/=/article=maker/id=1/">S1 NO.1 STYLE</a>
      2023/05/14
    </body></html>
    """
    with mock.patch.object(prov, "_fetch_html", return_value=fanza_html):
        with mock.patch("iptv.metadata._jav_rate_limiter"):
            meta = prov.fetch("", "", SECTION_MOVIES, raw_name="ABP-123")
    assert meta is not None
    assert meta["title"] == "ABP-123 Some Title"
    assert meta["year"] == "2023"
    assert "Yua Mikami" in meta["genres"]
    assert meta["poster"] == "https://pics.dmm.co.jp/digital/video/abp00123/abp00123pl.jpg"


def test_fanza_returns_none_for_non_jav():
    from iptv.metadata import FanzaProvider
    prov = FanzaProvider()
    with mock.patch("iptv.metadata._jav_rate_limiter"):
        assert prov.fetch("", "", SECTION_MOVIES, raw_name="Regular Movie") is None


# -- JAV chain integration --

def test_pipeline_jav_chain_order_fanza_javbus_javlibrary_tpdb(tmp_path):
    """A JAV code in an adult entry routes through the dedicated JAV chain:
    FANZA → JavBus → JavLibrary → TPDB (in that order)."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tpdb_api_key="tpdb")
    # All providers miss — verify the call order via call_args_list.
    pipe.fanza = mock.Mock(**{"fetch.return_value": None})
    pipe.javbus = mock.Mock(**{"fetch.return_value": None})
    pipe.javlibrary = mock.Mock(**{"fetch.return_value": None})
    pipe.tpdb = mock.Mock(**{"fetch.return_value": None})
    done = threading.Event()
    pipe.resolve_async(SECTION_MOVIES, "ABP-123 Some Title", "",
                       lambda k, m: done.set(), group="XXX")
    assert done.wait(10)
    # All four JAV providers were called (chain didn't short-circuit on miss).
    assert pipe.fanza.fetch.called
    assert pipe.javbus.fetch.called
    assert pipe.javlibrary.fetch.called
    assert pipe.tpdb.fetch.called
    pipe.shutdown()


def test_pipeline_jav_chain_first_hit_skips_rest(tmp_path):
    """When FANZA returns a poster, JavBus/JavLibrary/TPDB are not called."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tpdb_api_key="tpdb")
    pipe.fanza = mock.Mock(**{"fetch.return_value": {
        "title": "T", "poster": "http://cdn/p.jpg", "backdrop": "",
        "provider": "fanza", "genres": [], "rating": 0, "synopsis": "", "year": "",
    }})
    pipe.javbus = mock.Mock()
    pipe.javlibrary = mock.Mock()
    pipe.tpdb = mock.Mock()
    done = threading.Event()
    got = {}
    def _on_done(key, meta):
        got.update(meta or {})
        done.set()
    pipe.resolve_async(SECTION_MOVIES, "ABP-123", "", _on_done, group="XXX")
    assert done.wait(10)
    assert got.get("provider") == "fanza"
    assert pipe.fanza.fetch.called
    assert not pipe.javbus.fetch.called
    assert not pipe.javlibrary.fetch.called
    assert not pipe.tpdb.fetch.called
    pipe.shutdown()


def test_pipeline_jav_chain_javbus_fallback_when_fanza_misses(tmp_path):
    """FANZA misses (uncensored code) → JavBus hits → JavLibrary/TPDB skipped."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tpdb_api_key="tpdb")
    pipe.fanza = mock.Mock(**{"fetch.return_value": None})  # FANZA skips uncensored
    pipe.javbus = mock.Mock(**{"fetch.return_value": {
        "title": "T", "poster": "http://cdn/p.jpg", "backdrop": "",
        "provider": "javbus", "genres": [], "rating": 0, "synopsis": "", "year": "",
    }})
    pipe.javlibrary = mock.Mock()
    pipe.tpdb = mock.Mock()
    done = threading.Event()
    got = {}
    def _on_done(key, meta):
        got.update(meta or {})
        done.set()
    pipe.resolve_async(SECTION_MOVIES, "HEYZO-1234", "", _on_done, group="XXX")
    assert done.wait(10)
    assert got.get("provider") == "javbus"
    assert pipe.fanza.fetch.called
    assert pipe.javbus.fetch.called
    assert not pipe.javlibrary.fetch.called
    pipe.shutdown()


def test_pipeline_adult_without_jav_code_still_uses_tpdb(tmp_path):
    """Adult entries without a JAV code still route to TPDB (western adult),
    not the JAV chain."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tpdb_api_key="tpdb")
    pipe.tpdb = mock.Mock(**{"fetch.return_value": {
        "title": "S", "poster": "http://cdn/p.jpg", "backdrop": "",
        "provider": "tpdb", "genres": [], "rating": 0, "synopsis": "", "year": "2023",
    }})
    pipe.fanza = mock.Mock()
    pipe.javbus = mock.Mock()
    pipe.javlibrary = mock.Mock()
    done = threading.Event()
    pipe.resolve_async(SECTION_MOVIES, "Brazzers - Some Scene", "2023",
                       lambda k, m: done.set(), group="XXX")
    assert done.wait(10)
    assert pipe.tpdb.fetch.called
    assert not pipe.fanza.fetch.called
    assert not pipe.javbus.fetch.called
    pipe.shutdown()


# ---------------------------------------------------------------------------
# Phase 3: StashDB provider + combined match scoring
# ---------------------------------------------------------------------------

def _stashdb_scene(title="Scene Title", date="2023-05-14", studio="Brazzers",
                   performers=None, images=None):
    """Build a StashDB scene row for tests."""
    return {
        "id": "1", "title": title, "code": "", "release_date": date,
        "duration": 1800,
        "images": images or [{"id": "1", "url": "http://cdn/cover.jpg",
                              "width": 800, "height": 1200}],
        "studio": {"id": "s1", "name": studio},
        "performers": [{"as": None, "performer": {"id": "p1", "name": n}}
                       for n in (performers or ["Actress 1"])],
    }


def test_stashdb_parses_response_to_pipeline_fields():
    from iptv.metadata import StashDBProvider
    prov = StashDBProvider("key")
    row = _stashdb_scene()
    meta = prov._to_meta(row)
    assert meta["poster"] == "http://cdn/cover.jpg"
    assert meta["title"] == "Scene Title"
    assert meta["year"] == "2023"
    assert "Actress 1" in meta["genres"]
    assert meta["provider"] == "stashdb"


def test_stashdb_landscape_image_is_backdrop():
    """Landscape images (w > h) are backdrops; portrait are posters."""
    from iptv.metadata import StashDBProvider
    prov = StashDBProvider("key")
    row = _stashdb_scene(images=[
        {"id": "1", "url": "http://cdn/back.jpg", "width": 1920, "height": 1080},
        {"id": "2", "url": "http://cdn/cover.jpg", "width": 800, "height": 1200},
    ])
    meta = prov._to_meta(row)
    assert meta["poster"] == "http://cdn/cover.jpg"
    assert meta["backdrop"] == "http://cdn/back.jpg"


def test_stashdb_combined_score_ranks_year_and_studio_match():
    """The combined score (0.5*title + 0.3*year + 0.2*studio) should rank a
    year+studio match above a loose title-only match."""
    from iptv.metadata import StashDBProvider
    prov = StashDBProvider("key")
    # Row 1: perfect title, wrong year.
    row1 = _stashdb_scene(title="Riding Lessons", date="2020-01-01", studio="Vixen")
    # Row 2: slightly different title, correct year + matching studio.
    row2 = _stashdb_scene(title="Riding Lesson", date="2023-05-14", studio="Vixen")
    raw = "Vixen - Riding Lessons 2023-05-14"
    s1 = prov._combined_score(raw, "2023", row1)
    s2 = prov._combined_score(raw, "2023", row2)
    # Row 2 should score higher (year match + studio match outweigh the
    # slightly weaker title).
    assert s2 > s1


def test_stashdb_rejects_below_floor():
    """A row scoring below MIN_SCORE (0.5) is rejected — a wrong poster is
    worse than no poster."""
    from iptv.metadata import StashDBProvider
    prov = StashDBProvider("key")
    # Completely unrelated title, wrong year, wrong studio.
    row = _stashdb_scene(title="Totally Unrelated", date="1999-01-01", studio="Other")
    score = prov._combined_score("Vixen - Riding Lessons 2023", "2023", row)
    assert score < prov.MIN_SCORE


def test_stashdb_fetch_returns_none_without_key():
    """No API key -> None (chain falls through to other providers)."""
    from iptv.metadata import StashDBProvider
    prov = StashDBProvider("")
    assert prov.fetch("Scene", "2023", SECTION_MOVIES) is None


def test_stashdb_fetch_retries_on_503():
    """StashDB POST must retry on 503 (transient)."""
    from iptv.metadata import StashDBProvider
    prov = StashDBProvider("key")
    # Use a scene that matches the query so it clears the score floor.
    scene = _stashdb_scene(title="Riding Lessons", date="2023-05-14", studio="Vixen")
    ok = mock.Mock(status_code=200, json=lambda: {"data": {"searchScenes": {
        "count": 1, "scenes": [scene]}}})
    with mock.patch.object(prov._session, "post",
                           side_effect=[mock.Mock(status_code=503, close=mock.Mock()), ok]):
        with mock.patch("iptv.metadata.time.sleep"):
            meta = prov.fetch("Riding Lessons", "2023", SECTION_MOVIES,
                              raw_name="Vixen - Riding Lessons 2023-05-14")
    assert meta is not None
    assert meta["provider"] == "stashdb"


def test_stashdb_401_returns_none_and_does_not_raise():
    from iptv.metadata import StashDBProvider
    prov = StashDBProvider("bad-key")
    with mock.patch.object(prov._session, "post",
                           return_value=mock.Mock(status_code=401, close=mock.Mock())):
        with mock.patch("iptv.metadata.time.sleep"):
            assert prov.fetch("Scene", "", SECTION_MOVIES) is None


def test_pipeline_adult_chain_tpdb_then_stashdb(tmp_path):
    """Adult entries without a JAV code try TPDB first, then StashDB."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tpdb_api_key="tpdb", stashdb_api_key="stash")
    pipe.tpdb = mock.Mock(**{"fetch.return_value": None})
    pipe.stashdb = mock.Mock(**{"fetch.return_value": {
        "title": "S", "poster": "http://cdn/s.jpg", "backdrop": "",
        "provider": "stashdb", "genres": [], "rating": 0, "synopsis": "", "year": "2023",
    }})
    done = threading.Event()
    got = {}
    def _on_done(key, meta):
        got.update(meta or {})
        done.set()
    pipe.resolve_async(SECTION_MOVIES, "Brazzers - Some Scene", "2023",
                       _on_done, group="XXX")
    assert done.wait(10)
    assert got.get("provider") == "stashdb"
    assert pipe.tpdb.fetch.called
    assert pipe.stashdb.fetch.called
    pipe.shutdown()


def test_pipeline_stashdb_skipped_when_no_key(tmp_path):
    """Without a StashDB key, the chain skips it (stashdb is None)."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tpdb_api_key="tpdb")  # no stashdb key
    assert pipe.stashdb is None
    pipe.tpdb = mock.Mock(**{"fetch.return_value": None})
    done = threading.Event()
    pipe.resolve_async(SECTION_MOVIES, "Some Scene", "2023",
                       lambda k, m: done.set(), group="XXX")
    assert done.wait(10)
    assert pipe.tpdb.fetch.called
    pipe.shutdown()


# ---------------------------------------------------------------------------
# Phase 4: Generic fallback providers (OMDb, Wikipedia, Fanart.tv)
# ---------------------------------------------------------------------------

def test_omdb_parses_response():
    from iptv.metadata import OMDbProvider
    prov = OMDbProvider("key")
    fake = mock.Mock(status_code=200, json=lambda: {
        "Response": "True", "Title": "Inception",
        "Year": "2010", "imdbRating": "8.8",
        "Genre": "Action, Sci-Fi", "Plot": "A thief who steals dreams.",
        "Poster": "http://cdn/inception.jpg",
    })
    with mock.patch.object(prov._session, "get", return_value=fake):
        meta = prov.fetch("Inception", "2010", SECTION_MOVIES)
    assert meta is not None
    assert meta["title"] == "Inception"
    assert meta["year"] == "2010"
    assert meta["poster"] == "http://cdn/inception.jpg"
    assert "Action" in meta["genres"]
    assert meta["provider"] == "omdb"


def test_omdb_response_false_is_miss():
    """OMDb signals a miss with Response: "False" (HTTP 200), not an error."""
    from iptv.metadata import OMDbProvider
    prov = OMDbProvider("key")
    fake = mock.Mock(status_code=200, json=lambda: {
        "Response": "False", "Error": "Movie not found!",
    })
    with mock.patch.object(prov._session, "get", return_value=fake):
        assert prov.fetch("Unknown Movie", "", SECTION_MOVIES) is None


def test_omdb_na_poster_is_empty():
    """OMDb returns "N/A" for missing posters — must be normalized to empty."""
    from iptv.metadata import OMDbProvider
    prov = OMDbProvider("key")
    fake = mock.Mock(status_code=200, json=lambda: {
        "Response": "True", "Title": "Old Film", "Year": "1950",
        "Poster": "N/A", "Genre": "", "Plot": "",
    })
    with mock.patch.object(prov._session, "get", return_value=fake):
        meta = prov.fetch("Old Film", "1950", SECTION_MOVIES)
    assert meta is not None
    assert meta["poster"] == ""


def test_omdb_returns_none_without_key():
    from iptv.metadata import OMDbProvider
    prov = OMDbProvider("")
    assert prov.fetch("Title", "", SECTION_MOVIES) is None


def test_omdb_retries_on_503():
    from iptv.metadata import OMDbProvider
    prov = OMDbProvider("key")
    ok = mock.Mock(status_code=200, json=lambda: {
        "Response": "True", "Title": "T", "Year": "2020",
        "Poster": "http://cdn/p.jpg", "Genre": "", "Plot": "",
    })
    with mock.patch.object(prov._session, "get",
                           side_effect=[mock.Mock(status_code=503, close=mock.Mock()), ok]):
        with mock.patch("iptv.metadata.time.sleep"):
            meta = prov.fetch("T", "2020", SECTION_MOVIES)
    assert meta is not None


def test_wikipedia_fetch_returns_none_without_title():
    from iptv.metadata import WikipediaProvider
    prov = WikipediaProvider()
    assert prov.fetch("", "", SECTION_MOVIES) is None


def test_wikipedia_fetch_uses_page_summary():
    """The poster comes from the summary endpoint's structured originalimage,
    not from scraping HTML."""
    from iptv.metadata import WikipediaProvider
    prov = WikipediaProvider()
    search_resp = mock.Mock(status_code=200, json=lambda: {
        "pages": [{"key": "Inception", "title": "Inception"}],
    })
    summary_resp = mock.Mock(status_code=200, json=lambda: {
        "type": "standard", "title": "Inception",
        "extract": "A 2010 science fiction film directed by Christopher Nolan.",
        "originalimage": {"source": "https://upload.wikimedia.org/poster.jpg"},
        "thumbnail": {"source": "https://upload.wikimedia.org/thumb.jpg"},
    })
    with mock.patch.object(prov._session, "get",
                           side_effect=[search_resp, summary_resp]):
        meta = prov.fetch("Inception", "2010", SECTION_MOVIES)
    assert meta is not None
    assert meta["poster"] == "https://upload.wikimedia.org/poster.jpg"
    assert meta["synopsis"].startswith("A 2010 science fiction film")
    assert meta["provider"] == "wikipedia"


def test_wikipedia_fetch_rejects_disambiguation_pages():
    from iptv.metadata import WikipediaProvider
    prov = WikipediaProvider()
    search_resp = mock.Mock(status_code=200, json=lambda: {
        "pages": [{"key": "Mercury", "title": "Mercury"}],
    })
    summary_resp = mock.Mock(status_code=200, json=lambda: {
        "type": "disambiguation", "title": "Mercury",
        "originalimage": {"source": "https://upload.wikimedia.org/x.jpg"},
    })
    with mock.patch.object(prov._session, "get",
                           side_effect=[search_resp, summary_resp]):
        assert prov.fetch("Mercury", "", SECTION_MOVIES) is None


def test_wikipedia_prefers_media_titled_pages():
    """'Dune' must resolve to the film page, not the landform — Wikipedia
    ranks the primary topic first, so media-suffixed titles win."""
    from iptv.metadata import WikipediaProvider
    prov = WikipediaProvider()
    search_resp = mock.Mock(status_code=200, json=lambda: {"pages": [
        {"key": "Dune", "title": "Dune"},  # landform — primary topic
        {"key": "Dune_(1984_film)", "title": "Dune (1984 film)"},
    ]})
    summary_resp = mock.Mock(status_code=200, json=lambda: {
        "type": "standard", "title": "Dune (1984 film)", "extract": "Film.",
        "originalimage": {"source": "https://upload.wikimedia.org/dune84.jpg"},
    })
    with mock.patch.object(prov._session, "get",
                           side_effect=[search_resp, summary_resp]) as get:
        meta = prov.fetch("Dune", "1984", SECTION_MOVIES)
    assert meta is not None
    assert meta["poster"] == "https://upload.wikimedia.org/dune84.jpg"
    assert get.call_count == 2  # media page ranked first — no retry needed


def test_wikipedia_fetch_falls_back_to_thumbnail():
    from iptv.metadata import WikipediaProvider
    prov = WikipediaProvider()
    search_resp = mock.Mock(status_code=200, json=lambda: {
        "pages": [{"key": "Some_Film", "title": "Some Film"}],
    })
    summary_resp = mock.Mock(status_code=200, json=lambda: {
        "type": "standard", "title": "Some Film", "extract": "",
        "thumbnail": {"source": "https://upload.wikimedia.org/thumb.jpg"},
    })
    with mock.patch.object(prov._session, "get",
                           side_effect=[search_resp, summary_resp]):
        meta = prov.fetch("Some Film", "", SECTION_MOVIES)
    assert meta is not None
    assert meta["poster"] == "https://upload.wikimedia.org/thumb.jpg"


def test_pipeline_generic_chain_tmdb_omdb_wikipedia(tmp_path):
    """Non-adult movies: TMDb → OMDb → Wikipedia (when configured)."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb", omdb_api_key="omdb")
    pipe.tmdb = mock.Mock(**{"fetch.return_value": None})
    pipe.omdb = mock.Mock(**{"fetch.return_value": None})
    pipe.wikipedia = mock.Mock(**{"fetch.return_value": {
        "title": "T", "poster": "http://cdn/p.jpg", "backdrop": "",
        "provider": "wikipedia", "genres": [], "rating": 0, "synopsis": "", "year": "",
    }})
    done = threading.Event()
    got = {}
    def _on_done(key, meta):
        got.update(meta or {})
        done.set()
    pipe.resolve_async(SECTION_MOVIES, "Some Movie", "2023", _on_done, group="Films")
    assert done.wait(10)
    assert got.get("provider") == "wikipedia"
    assert pipe.tmdb.fetch.called
    assert pipe.omdb.fetch.called
    assert pipe.wikipedia.fetch.called
    pipe.shutdown()


def test_pipeline_wikipedia_skipped_for_adult(tmp_path):
    """Wikipedia is not tried for adult entries (it doesn't cover adult VOD)."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tpdb_api_key="tpdb")
    pipe.tpdb = mock.Mock(**{"fetch.return_value": None})
    pipe.wikipedia = mock.Mock()
    done = threading.Event()
    pipe.resolve_async(SECTION_MOVIES, "Some Adult Scene", "2023",
                       lambda k, m: done.set(), group="XXX")
    assert done.wait(10)
    assert not pipe.wikipedia.fetch.called
    pipe.shutdown()


def test_pipeline_omdb_skipped_when_no_key(tmp_path):
    """Without an OMDb key, the chain skips it (omdb is None)."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache)  # no keys at all
    assert pipe.omdb is None
    assert pipe.tmdb is None
    # Wikipedia is keyless, so it's always available.
    assert pipe.wikipedia is not None
    pipe.shutdown()


# ---------------------------------------------------------------------------
# Phase 5: Channel logo fallback chain + SVG support
# ---------------------------------------------------------------------------

def test_channel_logo_chain_uses_iptvorg_first():
    """When iptv-org has a logo, the chain returns it without trying favicons."""
    from iptv.metadata import ChannelLogoChain
    iptvorg = mock.Mock(**{"lookup.return_value": "http://cdn/bbc.png"})
    chain = ChannelLogoChain(iptvorg)
    url = chain.lookup("BBC News")
    assert url == "http://cdn/bbc.png"


def test_channel_logo_chain_falls_back_to_google_favicon():
    """When iptv-org misses, the chain tries Google S2 favicon."""
    from iptv.metadata import ChannelLogoChain
    iptvorg = mock.Mock(**{"lookup.return_value": ""})
    chain = ChannelLogoChain(iptvorg)
    ok = mock.Mock(status_code=200, headers={"Content-Type": "image/png"})
    with mock.patch.object(chain._session, "head", return_value=ok):
        url = chain.lookup("BBC News")
    assert "google.com/s2/favicons" in url
    assert "bbcnews.com" in url


def test_channel_logo_chain_falls_back_to_ddg_when_google_fails():
    """When Google S2 returns 404, the chain tries DuckDuckGo favicon."""
    from iptv.metadata import ChannelLogoChain
    iptvorg = mock.Mock(**{"lookup.return_value": ""})
    chain = ChannelLogoChain(iptvorg)
    google_404 = mock.Mock(status_code=404)
    ddg_ok = mock.Mock(status_code=200, headers={"Content-Type": "image/x-icon"})
    with mock.patch.object(chain._session, "head", side_effect=[google_404, ddg_ok]):
        url = chain.lookup("Sky Sports")
    assert "duckduckgo.com" in url
    assert "skysports.com" in url


def test_channel_logo_chain_returns_empty_when_all_sources_fail():
    from iptv.metadata import ChannelLogoChain
    iptvorg = mock.Mock(**{"lookup.return_value": ""})
    chain = ChannelLogoChain(iptvorg)
    with mock.patch.object(chain._session, "head",
                           return_value=mock.Mock(status_code=404)):
        assert chain.lookup("Unknown Channel XYZ") == ""


def test_channel_logo_chain_guess_domain_strips_decorations():
    """Domain guessing strips HD/FHD/country decorations from channel names."""
    from iptv.metadata import ChannelLogoChain
    # "BBC News HD" -> "bbcnews.com" (HD stripped).
    assert ChannelLogoChain._guess_domain("BBC News HD") == "bbcnews.com"
    # "UK: Sky Sports" -> "skysports.com" (UK: stripped, country prefix).
    assert ChannelLogoChain._guess_domain("UK: Sky Sports") == "skysports.com"


def test_channel_logo_chain_returns_empty_for_short_names():
    """Very short channel names (after decoration stripping) get no favicon."""
    from iptv.metadata import ChannelLogoChain
    assert ChannelLogoChain._guess_domain("HD") == ""


def test_channel_logo_chain_skips_favicon_for_empty_name():
    from iptv.metadata import ChannelLogoChain
    iptvorg = mock.Mock(**{"lookup.return_value": ""})
    chain = ChannelLogoChain(iptvorg)
    assert chain.lookup("") == ""


def test_pipeline_channel_logo_fallback_uses_chain(tmp_path):
    """MetadataPipeline.channel_logo_fallback delegates to the logo chain."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache)
    pipe.logo_chain = mock.Mock(**{"lookup.return_value": "http://cdn/logo.png"})
    assert pipe.channel_logo_fallback("BBC News") == "http://cdn/logo.png"
    pipe.shutdown()


# -- SVG support --

def test_is_svg_detects_xml_prefixed_svg():
    from iptv.artwork import _is_svg
    data = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
    assert _is_svg(data) is True


def test_is_svg_detects_bare_svg():
    from iptv.artwork import _is_svg
    data = b'<svg width="100" height="100"></svg>'
    assert _is_svg(data) is True


def test_is_svg_rejects_png():
    from iptv.artwork import _is_svg
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    assert _is_svg(data) is False


def test_is_svg_rejects_html():
    from iptv.artwork import _is_svg
    data = b"<html><body>Not an image</body></html>"
    assert _is_svg(data) is False


def test_is_image_file_accepts_svg(tmp_path):
    """_is_image_file returns True for SVG files (rasterized later by the cache)."""
    from iptv.artwork import _is_image_file
    p = tmp_path / "logo.svg"
    p.write_text('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"></svg>')
    assert _is_image_file(str(p)) is True


# ---------------------------------------------------------------------------
# Phase 6: ETag revalidation, progressive JPEG, provider enable/disable,
#           backdrop enrichment
# ---------------------------------------------------------------------------

def test_artwork_meta_save_and_load(tmp_path):
    """ETag/Last-Modified sidecar is saved and loaded for revalidation."""
    from iptv.artwork import ArtworkCache
    cache = ArtworkCache(str(tmp_path / "art"))
    cache._save_meta("http://cdn/img.jpg", etag='"abc123"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")
    meta = cache._load_meta("http://cdn/img.jpg")
    assert meta["etag"] == '"abc123"'
    assert "Mon, 01 Jan 2024" in meta["last_modified"]


def test_artwork_meta_missing_returns_empty(tmp_path):
    from iptv.artwork import ArtworkCache
    cache = ArtworkCache(str(tmp_path / "art"))
    assert cache._load_meta("http://cdn/nonexistent.jpg") == {}


def test_pipeline_javbus_disabled_skips_in_chain(tmp_path):
    """When enable_javbus=False, JavBus is None and skipped in the JAV chain."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tpdb_api_key="tpdb", enable_javbus=False)
    assert pipe.javbus is None
    assert pipe.fanza is not None  # FANZA still enabled
    pipe.tpdb = mock.Mock(**{"fetch.return_value": None})
    pipe.fanza = mock.Mock(**{"fetch.return_value": None})
    pipe.javlibrary = mock.Mock(**{"fetch.return_value": None})
    done = threading.Event()
    pipe.resolve_async(SECTION_MOVIES, "ABP-123", "", lambda k, m: done.set(), group="XXX")
    assert done.wait(10)
    # JavBus is None, so it can't be called — the chain skips it.
    assert pipe.fanza.fetch.called
    assert pipe.javlibrary.fetch.called
    pipe.shutdown()


def test_pipeline_wikipedia_disabled_skips_in_chain(tmp_path):
    """When enable_wikipedia=False, Wikipedia is None and skipped."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, enable_wikipedia=False)
    assert pipe.wikipedia is None
    pipe.shutdown()


def test_pipeline_fanarttv_enriches_backdrop(tmp_path):
    """When a provider returns a poster but no backdrop, Fanart.tv is tried
    to supplement the backdrop."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb", fanarttv_api_key="fanart")
    pipe.tmdb = mock.Mock(**{"fetch.return_value": {
        "title": "T", "poster": "http://image.tmdb.org/t/p/w780/123.jpg",
        "backdrop": "", "provider": "tmdb", "genres": [], "rating": 0,
        "synopsis": "", "year": "2023", "_tmdb_id": "12345", "_is_series": False,
    }})
    pipe.fanarttv = mock.Mock(**{"fetch_backdrop.return_value": "http://cdn/fanart.jpg"})
    done = threading.Event()
    got = {}
    def _on_done(key, meta):
        got.update(meta or {})
        done.set()
    pipe.resolve_async(SECTION_MOVIES, "Some Movie", "2023", _on_done, group="Films")
    assert done.wait(10)
    assert got.get("backdrop") == "http://cdn/fanart.jpg"
    assert pipe.fanarttv.fetch_backdrop.called
    pipe.shutdown()


def test_pipeline_fanarttv_skipped_when_backdrop_already_present(tmp_path):
    """When the primary provider already returned a backdrop, Fanart.tv
    enrichment is skipped (no need to supplement)."""
    from iptv.metadata import MetadataPipeline
    cache = IPTVCache(str(tmp_path))
    pipe = MetadataPipeline(cache, tmdb_api_key="tmdb", fanarttv_api_key="fanart")
    pipe.tmdb = mock.Mock(**{"fetch.return_value": {
        "title": "T", "poster": "http://image.tmdb.org/t/p/w780/123.jpg",
        "backdrop": "http://image.tmdb.org/t/p/w1280/123b.jpg",
        "provider": "tmdb", "genres": [], "rating": 0, "synopsis": "", "year": "2023",
    }})
    pipe.fanarttv = mock.Mock()
    done = threading.Event()
    pipe.resolve_async(SECTION_MOVIES, "Some Movie", "2023",
                       lambda k, m: done.set(), group="Films")
    assert done.wait(10)
    assert not pipe.fanarttv.fetch_backdrop.called
    pipe.shutdown()


def _fake_xtream_response(action):
    if action == "get_live_categories":
        return [{"category_id": "1", "category_name": "News"}]
    if action == "get_live_streams":
        return [{"stream_id": 100, "name": "CNN", "stream_icon": "http://l/cnn", "epg_channel_id": "cnn"}]
    if action == "get_vod_categories":
        return [{"category_id": "2", "category_name": "Films"}]
    if action == "get_vod_streams":
        return [{"stream_id": 200, "name": "Inception", "stream_icon": "", "container_extension": "mkv"}]
    if action == "get_series_categories":
        return [{"category_id": "3", "category_name": "Drama"}]
    if action == "get_series":
        return [{"series_id": 300, "name": "Lost", "cover": "http://l/lost",
                 "releaseDate": "2004-09-22"}]
    if action == "get_series_info":
        return {"episodes": {"1": [{"id": 301, "title": "Pilot", "episode_num": 1, "container_extension": "mp4"}]}}
    # auth call (no action)
    return {"user_info": {"auth": 1}, "server_info": {"url": "http://host:8080"}}


def test_xtream_load_playlist_mocked():
    src = PlaylistSource(id="x1", name="X", kind="xtream", url="http://host:8080",
                         username="u", password="p")
    with mock.patch("iptv.xtream.requests.get") as g:
        def _fake_get(url, headers=None, timeout=30):
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock()
            # Parse action from query string.
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(url).query)
            action = (qs.get("action", [""])[0]) or ""
            resp.json = lambda: _fake_xtream_response(action)
            return resp
        g.side_effect = _fake_get
        pl = xtream.load_playlist(src)
    assert len(pl.channels) == 1 and pl.channels[0].name == "CNN"
    assert len(pl.movies) == 1 and pl.movies[0].url.endswith(".mkv")
    assert len(pl.series) == 1 and len(pl.series[0].episodes) == 1
    assert pl.series[0].episodes[0].url.endswith(".mp4")
    # The series list payload's releaseDate maps to the year field.
    assert pl.series[0].year == "2004"


def test_xtream_auth_failure_returns_empty():
    src = PlaylistSource(id="x2", name="X", kind="xtream", url="http://h",
                         username="u", password="bad")
    with mock.patch("iptv.xtream.requests.get") as g:
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.json = lambda: {"user_info": {"auth": 0}, "server_info": {}}
        g.return_value = resp
        pl = xtream.load_playlist(src)
    assert pl.total == 0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_cache_playlist_roundtrip(tmp_path):
    cache = IPTVCache(str(tmp_path))
    payload = {"channels": [{"id": "1"}], "movies": [], "series": [], "categories": [], "url_tvg": "u"}
    cache.save_playlist("s1", payload, url_tvg="u")
    assert cache.load_playlist("s1") == payload
    assert cache.playlist_age("s1") is not None


def test_cache_favorites_and_recent(tmp_path):
    cache = IPTVCache(str(tmp_path))
    cache.add_favorite("s1", "i1", "live")
    assert cache.is_favorite("s1", "i1")
    cache.remove_favorite("s1", "i1")
    assert not cache.is_favorite("s1", "i1")
    cache.add_recent("s1", "i2", "movies", "Film", "http://x")
    rec = cache.recent("s1")
    assert len(rec) == 1 and rec[0]["name"] == "Film"


def test_cache_epg_now_next(tmp_path):
    import time as _time
    cache = IPTVCache(str(tmp_path))
    now = _time.time()
    cache.save_epg("url", [
        {"channel_id": "cnn", "start": int(now - 60), "end": int(now + 600), "title": "Now Show", "desc": ""},
        {"channel_id": "cnn", "start": int(now + 600), "end": int(now + 1200), "title": "Next Show", "desc": ""},
    ])
    nn = cache.epg_now_next("cnn")
    assert nn["now"] == "Now Show"
    assert nn["next"] == "Next Show"


def test_cache_epg_scoped_per_source(tmp_path):
    import time as _time
    cache = IPTVCache(str(tmp_path))
    now = _time.time()
    cache.save_epg("url-a", [
        {"channel_id": "a1", "start": int(now - 60), "end": int(now + 600), "title": "Show A", "desc": ""},
    ])
    cache.save_epg("url-b", [
        {"channel_id": "b1", "start": int(now - 60), "end": int(now + 600), "title": "Show B", "desc": ""},
    ])
    # Updating source B must not wipe source A's programs.
    assert cache.epg_now_next("a1")["now"] == "Show A"
    assert cache.epg_now_next("b1")["now"] == "Show B"


def test_cache_clear_metadata_is_scoped(tmp_path):
    """clear_metadata wipes lookups (incl. negative rows) but keeps playlists,
    favorites and history — those are user data, not derived cache."""
    cache = IPTVCache(str(tmp_path))
    cache.save_metadata("movies:dune:1984", "movies", "Dune", "1984", "tmdb",
                        {"poster": "http://x/p.jpg"})
    cache.save_metadata("movies:nope:", "movies", "Nope", "", "none",
                        {"negative": True, "updated_at": 1, "provider_misses": {}})
    cache.save_playlist("s1", {"channels": [], "movies": [], "series": [],
                               "categories": [], "url_tvg": ""})
    cache.add_favorite("s1", "i1", "live")
    cache.add_recent("s1", "i1", "live", "CNN", "http://cnn")
    assert cache.metadata_count() == 2
    assert cache.clear_metadata() == 2
    assert cache.metadata_count() == 0
    assert cache.load_metadata("movies:dune:1984") is None
    assert cache.load_playlist("s1") is not None
    assert cache.is_favorite("s1", "i1")
    assert len(cache.recent("s1")) == 1
    cache.vacuum()  # must not raise


# ---------------------------------------------------------------------------
# Manager (favorites, search, recent — no network)
# ---------------------------------------------------------------------------

def _manager_with_playlist(tmp_path):
    mgr = IPTVManager(sources=[], tmdb_api_key="", data_dir=str(tmp_path))
    pl = Playlist(source_id="s1")
    pl.channels = [Channel(id="c1", name="CNN", url="http://cnn", group="News", section=SECTION_LIVE)]
    mgr._playlists["s1"] = pl
    mgr._active_source_id = "s1"
    return mgr, pl


def test_manager_favorites_toggle(tmp_path):
    mgr, pl = _manager_with_playlist(tmp_path)
    ch = pl.channels[0]
    assert mgr.toggle_favorite(ch) is True
    assert ch.favorite
    assert mgr.toggle_favorite(ch) is False
    assert not ch.favorite


def test_manager_search(tmp_path):
    mgr, pl = _manager_with_playlist(tmp_path)
    res = mgr.search("cnn")
    assert SECTION_LIVE in res and res[SECTION_LIVE][0].name == "CNN"
    assert mgr.search("zzz") == {}


def test_manager_recent(tmp_path):
    mgr, pl = _manager_with_playlist(tmp_path)
    ch = pl.channels[0]
    mgr.record_recent(ch)
    rec = mgr.recent()
    assert len(rec) == 1 and rec[0]["name"] == "CNN"


def test_manager_cache_stats(tmp_path):
    mgr, _pl = _manager_with_playlist(tmp_path)
    url = "http://host/p.jpg"
    with open(mgr.artwork.full_path(url), "wb") as f:
        f.write(b"x" * 100)
    mgr.cache.save_metadata("movies:dune:1984", "movies", "Dune", "1984", "tmdb", {"poster": "x"})
    s = mgr.cache_stats()
    assert s["full_bytes"] >= 100
    assert s["metadata_rows"] == 1
    assert s["db_bytes"] > 0
    assert s["total_bytes"] == s["full_bytes"] + s["thumb_bytes"] + s["db_bytes"]
    mgr.shutdown()


def test_manager_clear_caches_keeps_user_data(tmp_path):
    """The delete-all button's backend: artwork + metadata go, playlists,
    favorites and history survive, framegrab failures are forgotten."""
    mgr, pl = _manager_with_playlist(tmp_path)
    url = "http://host/p.jpg"
    with open(mgr.artwork.full_path(url), "wb") as f:
        f.write(b"x" * 100)
    mgr.cache.save_metadata("movies:dune:1984", "movies", "Dune", "1984", "tmdb", {"poster": "x"})
    mgr.cache.save_playlist("s1", {"channels": [], "movies": [], "series": [],
                                   "categories": [], "url_tvg": ""})
    mgr.toggle_favorite(pl.channels[0])
    mgr.record_recent(pl.channels[0])
    if mgr.framegrab:
        mgr.framegrab._failed.add("framegrab:dead")
    done = threading.Event()
    got = {}
    mgr.clear_caches_async(lambda s: (got.update(s), done.set()))
    assert done.wait(20)
    assert got["artwork_files"] >= 1 and got["artwork_bytes"] >= 100
    assert got["metadata_rows"] == 1
    assert mgr.artwork.get_cached(url) is None
    assert mgr.cache.metadata_count() == 0
    assert mgr.cache.load_playlist("s1") is not None
    assert mgr.cache.is_favorite("s1", "c1")
    assert len(mgr.cache.recent("s1")) == 1
    if mgr.framegrab:
        assert "framegrab:dead" not in mgr.framegrab._failed
    mgr.shutdown()


def test_manager_framegrab_toggle_at_runtime(tmp_path):
    """The settings checkbox toggles the FrameGrabber without a restart."""
    mgr, _pl = _manager_with_playlist(tmp_path)
    assert mgr.framegrab is not None  # default on
    mgr.set_framegrab_enabled(False)
    assert mgr.framegrab is None
    mgr.set_framegrab_enabled(False)  # idempotent
    assert mgr.framegrab is None
    mgr.set_framegrab_enabled(True)
    assert mgr.framegrab is not None
    mgr.set_framegrab_enabled(True)  # idempotent
    assert mgr.framegrab is not None
    mgr.shutdown()


def test_manager_enforce_cache_limit(tmp_path):
    mgr, _pl = _manager_with_playlist(tmp_path)
    mgr.cache_limit_mb = 0.0001  # 104 bytes
    url = "http://host/p.jpg"
    with open(mgr.artwork.full_path(url), "wb") as f:
        f.write(b"x" * 1000)
    t = mgr.enforce_cache_limit_async()
    t.join(20)
    assert mgr.artwork.get_cached(url) is None
    mgr.shutdown()


# ---------------------------------------------------------------------------
# EPG date parsing (timezone-aware)
# ---------------------------------------------------------------------------

def test_parse_xmltv_extracts_titles(tmp_path):
    """Regression: child <title>/<desc> end events fire before the parent
    <programme>'s — clearing them early used to wipe every title/desc."""
    from iptv.epg import parse_xmltv
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="cnn.us"><display-name>US: CNN</display-name></channel>
  <programme start="20260101060000 +0000" stop="20260101070000 +0000" channel="cnn.us">
    <title>Newsroom</title>
    <desc>Live news coverage.</desc>
  </programme>
  <programme start="20260101070000 +0000" stop="20260101080000 +0000" channel="cnn.us">
    <title>Anderson Cooper 360</title>
  </programme>
</tv>"""
    p = tmp_path / "epg.xml"
    p.write_text(xml, encoding="utf-8")
    programs = parse_xmltv(str(p))
    assert len(programs) == 2
    assert programs[0]["title"] == "Newsroom"
    assert programs[0]["desc"] == "Live news coverage."
    assert programs[0]["channel_id"] == "cnn.us"
    assert programs[1]["title"] == "Anderson Cooper 360"
    assert programs[1]["desc"] == ""


def test_epg_date_honors_timezone_offset():
    from datetime import datetime, timezone
    from iptv.epg import _parse_xmltv_date
    # Same wall time, different offsets -> different epochs.
    utc = _parse_xmltv_date("20240101120000 +0000")
    plus2 = _parse_xmltv_date("20240101120000 +0200")
    assert utc == int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    assert utc - plus2 == 7200
    # Missing offset treated as UTC.
    assert _parse_xmltv_date("20240101120000") == utc
    assert _parse_xmltv_date("garbage") == 0


# ---------------------------------------------------------------------------
# M3U download (mocked HTTP): retries + UTF-8 decoding
# ---------------------------------------------------------------------------

def test_download_m3u_retries_and_decodes_utf8():
    from iptv.manager import IPTVManager
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/pl.m3u")
    body = "#EXTINF:-1,24/7 HOW IT’S MADE\nhttp://s/1.ts\n".encode("utf-8")

    calls = {"n": 0}

    def _fake_get(url, headers=None, timeout=30):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("reset")
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.content = body
        return resp

    with mock.patch("iptv.manager.requests.get", side_effect=_fake_get):
        with mock.patch("iptv.manager.time.sleep") as slp:
            text = IPTVManager._download_m3u(src, {}, None)
    assert calls["n"] == 3  # retried until success
    assert slp.call_count == 2
    assert "HOW IT’S MADE" in text  # UTF-8, not mojibake


def test_download_m3u_gives_up_after_attempts():
    from iptv.manager import IPTVManager
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/pl.m3u")
    with mock.patch("iptv.manager.requests.get", side_effect=ConnectionError("down")):
        with mock.patch("iptv.manager.time.sleep"):
            assert IPTVManager._download_m3u(src, {}, None, attempts=3) is None


def test_looks_like_m3u():
    from iptv.m3u_parser import looks_like_m3u
    assert looks_like_m3u("#EXTM3U\n#EXTINF:-1,CNN\nhttp://s/1.ts")
    assert looks_like_m3u("http://s/1.ts\nhttp://s/2.ts")       # bare URL list
    assert looks_like_m3u("\ufeff\n  #EXTM3U\n")               # BOM + whitespace
    assert not looks_like_m3u("")                              # empty body
    assert not looks_like_m3u("   \n  ")                       # whitespace only
    assert not looks_like_m3u('<?xml version="1.0"?><tv>...</tv>')  # XMLTV EPG
    assert not looks_like_m3u("<!DOCTYPE html><html>...</html>")    # error page


def test_download_m3u_rejects_non_playlist_payload():
    """An XMLTV/HTML payload (EPG URL pasted as playlist URL) is rejected
    immediately — no retries, no garbage entries."""
    from iptv.manager import IPTVManager
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/epg.xml")
    body = b'<?xml version="1.0" encoding="UTF-8"?>\n<tv><channel id="a"/></tv>'

    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.content = body
    with mock.patch("iptv.manager.requests.get", return_value=resp) as get:
        with mock.patch("iptv.manager.time.sleep") as slp:
            assert IPTVManager._download_m3u(src, {}, None) is None
    assert get.call_count == 1   # fail fast — retrying a 200-OK XML is pointless
    assert slp.call_count == 0


def test_download_m3u_backs_off_progressively():
    """Retries wait 2s/5s/15s so a provider rate-limit window can pass."""
    from iptv.manager import IPTVManager
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/pl.m3u")
    with mock.patch("iptv.manager.requests.get", side_effect=ConnectionError("down")):
        with mock.patch("iptv.manager.time.sleep") as slp:
            assert IPTVManager._download_m3u(src, {}, None, attempts=4) is None
    assert [c.args[0] for c in slp.call_args_list] == [2, 5, 15]


def test_failed_refresh_keeps_the_cached_playlist(tmp_path):
    """A failed background refresh must NOT blank the source: the cached copy
    stays in memory, flagged stale, instead of being replaced by an empty one."""
    mgr = IPTVManager(sources=[], data_dir=str(tmp_path))
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/pl.m3u")
    cached = Playlist(source_id="s")
    cached.channels.append(Channel(id=make_id("s", "CNN"), name="CNN",
                                   url="http://s/1.ts", section=SECTION_LIVE))
    mgr._playlists["s"] = cached
    with mock.patch("iptv.manager.requests.get", side_effect=ConnectionError("RST")):
        with mock.patch("iptv.manager.time.sleep"):
            pl = mgr._refresh_source(src, None, None, True)
    assert pl is cached and pl.stale is True
    assert mgr.playlist_for("s").total == 1
    mgr.shutdown()


def test_failed_refresh_without_cache_returns_empty(tmp_path):
    """With nothing cached there is nothing to keep — the source is empty."""
    mgr = IPTVManager(sources=[], data_dir=str(tmp_path))
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/pl.m3u")
    with mock.patch("iptv.manager.requests.get", side_effect=ConnectionError("RST")):
        with mock.patch("iptv.manager.time.sleep"):
            pl = mgr._refresh_source(src, None, None, False)
    assert pl.total == 0 and pl.stale is False
    assert mgr.playlist_for("s").total == 0
    mgr.shutdown()


def test_load_all_async_coalesces_a_second_call(tmp_path):
    """A second load_all_async while one is running must not download in
    parallel — it flags a re-run with the latest sources instead."""
    a = PlaylistSource(id="a", name="A", kind="m3u_url", url="http://a/p.m3u")
    mgr = IPTVManager(sources=[a], data_dir=str(tmp_path))
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.content = b"#EXTM3U\n#EXTINF:-1,CNN\nhttp://s/1.ts\n"
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            entered.set()
            release.wait(timeout=30)
        return resp

    with mock.patch("iptv.manager.requests.get", side_effect=slow_get):
        t1 = mgr.load_all_async(use_cache=False)
        assert entered.wait(timeout=10)
        t2 = mgr.load_all_async(use_cache=False)   # no second sweep
        assert t2 is t1
        assert mgr._reload_requested is True
        release.set()
        t1.join(timeout=30)
    assert not t1.is_alive()
    assert len(calls) == 2   # one download per sequential pass
    assert mgr.playlist_for("a").total == 1
    mgr.shutdown()


def test_parallel_refresh_of_same_source_is_deduped(tmp_path):
    """Two threads refreshing one source = one download; the follower waits
    for the leader and serves its result."""
    a = PlaylistSource(id="a", name="A", kind="m3u_url", url="http://a/p.m3u")
    mgr = IPTVManager(sources=[a], data_dir=str(tmp_path))
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.content = b"#EXTM3U\n#EXTINF:-1,CNN\nhttp://s/1.ts\n"
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_get(url, **kwargs):
        calls.append(url)
        entered.set()
        release.wait(timeout=30)
        return resp

    results = []
    with mock.patch("iptv.manager.requests.get", side_effect=slow_get):
        t1 = threading.Thread(
            target=lambda: results.append(mgr._refresh_source(a, None, None, False)))
        t1.start()
        assert entered.wait(timeout=10)
        t2 = threading.Thread(
            target=lambda: results.append(mgr._refresh_source(a, None, None, False)))
        t2.start()
        # t2 must not start a second download; give it a brief window to try.
        entered.clear()
        assert not entered.wait(timeout=1)
        release.set()
        t1.join(timeout=30)
        t2.join(timeout=30)
    assert len(calls) == 1
    assert len(results) == 2 and all(pl.total == 1 for pl in results)
    mgr.shutdown()


# ---------------------------------------------------------------------------
# Multiple sources loaded at once (Source > Section > Category)
# ---------------------------------------------------------------------------

def _two_source_manager(tmp_path):
    """Manager with two sources whose playlists are already in memory."""
    from iptv.manager import IPTVManager
    a = PlaylistSource(id="a", name="Provider A", kind="m3u_url", url="http://a/p.m3u")
    b = PlaylistSource(id="b", name="Provider B", kind="m3u_url", url="http://b/p.m3u")
    mgr = IPTVManager(sources=[a, b], data_dir=str(tmp_path))
    for sid, chan_name, cat in (("a", "A News", "News"), ("b", "B Sport", "Sport")):
        pl = Playlist(source_id=sid)
        pl.channels.append(Channel(id=make_id(sid, chan_name), name=chan_name,
                                   url=f"http://{sid}/1.ts", group=cat,
                                   section=SECTION_LIVE))
        pl.movies.append(Movie(id=make_id(sid, "film"), name=f"{sid} Film",
                               url=f"http://{sid}/f.mkv", group="Films",
                               section=SECTION_MOVIES))
        pl.categories = [Category(name=cat, section=SECTION_LIVE, count=1),
                         Category(name="Films", section=SECTION_MOVIES, count=1)]
        mgr._playlists[sid] = pl
    mgr._active_source_id = "a"
    return mgr


def test_source_id_recoverable_from_item_id():
    """Item ids are '<source_id>::<hash>' — that is how a merged view knows
    which provider an entry belongs to."""
    from iptv.manager import IPTVManager
    item = Channel(id=make_id("srcA", "CNN"), name="CNN", url="http://s/1.ts")
    assert IPTVManager.source_id_of(item) == "srcA"
    assert IPTVManager.source_id_of(
        Channel(id="no-separator", name="x", url="http://s/2.ts")) == ""


def test_items_and_categories_are_per_source(tmp_path):
    mgr = _two_source_manager(tmp_path)
    assert [i.name for i in mgr.items_for(SECTION_LIVE, source_id="a")] == ["A News"]
    assert [i.name for i in mgr.items_for(SECTION_LIVE, source_id="b")] == ["B Sport"]
    assert [c.name for c in mgr.categories_for(SECTION_LIVE, "a")] == ["News"]
    assert [c.name for c in mgr.categories_for(SECTION_LIVE, "b")] == ["Sport"]
    # Omitting source_id keeps the old behaviour (active source).
    assert [i.name for i in mgr.items_for(SECTION_LIVE)] == ["A News"]
    mgr.shutdown()


def test_category_filter_applies_within_the_chosen_source(tmp_path):
    mgr = _two_source_manager(tmp_path)
    assert len(mgr.items_for(SECTION_LIVE, "News", source_id="a")) == 1
    assert mgr.items_for(SECTION_LIVE, "News", source_id="b") == []
    mgr.shutdown()


def test_loaded_source_ids_and_playlist_for(tmp_path):
    mgr = _two_source_manager(tmp_path)
    assert set(mgr.loaded_source_ids()) == {"a", "b"}
    assert mgr.playlist_for("b").source_id == "b"
    assert mgr.playlist_for("missing") is None
    mgr.shutdown()


# ---------------------------------------------------------------------------
# Group-by-year (Movies/Series sidebar)
# ---------------------------------------------------------------------------

def _year_manager(tmp_path):
    """Manager with one source whose movies carry known/unknown years.

    Groups matter: "Films" vs "XXX VOD" must keep separate year buckets."""
    a = PlaylistSource(id="a", name="A", kind="m3u_url", url="http://a/p.m3u")
    mgr = IPTVManager(sources=[a], data_dir=str(tmp_path))
    pl = Playlist(source_id="a")
    for i, (name, year, group) in enumerate((
            ("Film One 2024", "2024", "Films"), ("Film Two 2021", "2021", "Films"),
            ("Adult Three 2024", "2024", "XXX VOD"), ("Yearless", "", "Films"))):
        pl.movies.append(Movie(id=make_id("a", f"m{i}"), name=name,
                               url=f"http://a/{i}.mkv", section=SECTION_MOVIES,
                               year=year, group=group))
    mgr._playlists["a"] = pl
    mgr._active_source_id = "a"
    return mgr


def test_years_for_sorted_desc_others_last(tmp_path):
    mgr = _year_manager(tmp_path)
    cats = mgr.years_for(SECTION_MOVIES)
    assert [(c.name, c.count) for c in cats] == [
        ("2024", 2), ("2021", 1), (YEAR_OTHERS, 1)]
    # Live TV has no year grouping.
    assert mgr.years_for(SECTION_LIVE) == []
    mgr.shutdown()


def test_years_for_is_scoped_per_category(tmp_path):
    """Year buckets must not merge the provider's categories (VOD vs XXX)."""
    mgr = _year_manager(tmp_path)
    assert [(c.name, c.count) for c in mgr.years_for(SECTION_MOVIES, category="Films")] == [
        ("2024", 1), ("2021", 1), (YEAR_OTHERS, 1)]
    assert [(c.name, c.count) for c in mgr.years_for(SECTION_MOVIES, category="XXX VOD")] == [
        ("2024", 1)]
    mgr.shutdown()


def test_years_by_category_one_pass(tmp_path):
    """The sidebar's whole-section bucket map matches the per-category API."""
    mgr = _year_manager(tmp_path)
    by_cat = mgr.years_by_category(SECTION_MOVIES)
    for cat in ("Films", "XXX VOD"):
        assert [(c.name, c.count) for c in by_cat[cat]] == [
            (c.name, c.count)
            for c in mgr.years_for(SECTION_MOVIES, category=cat)]
    assert mgr.years_by_category(SECTION_LIVE) == {}
    mgr.shutdown()


def test_items_for_year_filter(tmp_path):
    mgr = _year_manager(tmp_path)
    assert [i.name for i in mgr.items_for(SECTION_MOVIES, year="2024")] == [
        "Film One 2024", "Adult Three 2024"]
    assert [i.name for i in mgr.items_for(SECTION_MOVIES, year=YEAR_OTHERS)] == ["Yearless"]
    assert mgr.items_for(SECTION_MOVIES, year="1999") == []
    # Category and year filters combine.
    assert [i.name for i in mgr.items_for(SECTION_MOVIES, "XXX VOD", year="2024")] == [
        "Adult Three 2024"]
    # No year argument keeps the old unfiltered behaviour.
    assert len(mgr.items_for(SECTION_MOVIES)) == 4
    mgr.shutdown()


def test_playlist_cache_backfills_years(tmp_path):
    """Playlists cached before years existed get them from names on load."""
    from iptv.manager import _playlist_from_cache
    data = {
        "channels": [],
        "movies": [{"id": "m", "name": "Old Film 1999 1080p", "url": "u"}],
        "series": [{"id": "s", "name": "Old Show S01E01", "episodes": []}],
        "categories": [],
    }
    pl = _playlist_from_cache("a", data)
    assert pl.movies[0].year == "1999"
    assert pl.series[0].year == ""


def test_refresh_populates_years(tmp_path):
    """The refresh path (m3u download) fills years via populate_years."""
    src = PlaylistSource(id="a", name="A", kind="m3u_url", url="http://a/p.m3u")
    mgr = IPTVManager(sources=[src], data_dir=str(tmp_path))
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.content = YEAR_M3U.encode("utf-8")
    with mock.patch("iptv.manager.requests.get", return_value=resp):
        pl = mgr._refresh_source(src, None, None, False)
    by_name = {m.name: m.year for m in pl.movies}
    assert by_name["Some Movie 2021 1080p WEBRip"] == "2021"
    assert by_name["Yearless Film [1080p] [WEBRip]"] == ""
    mgr.shutdown()


def test_favorite_files_against_the_items_own_source(tmp_path):
    """Active source is 'a'; favouriting a 'b' item must not be filed under 'a'."""
    mgr = _two_source_manager(tmp_path)
    b_item = mgr.items_for(SECTION_LIVE, source_id="b")[0]
    assert mgr.toggle_favorite(b_item) is True
    assert mgr.cache.is_favorite("b", b_item.id)
    assert not mgr.cache.is_favorite("a", b_item.id)
    mgr.shutdown()


def test_recent_files_against_the_items_own_source(tmp_path):
    mgr = _two_source_manager(tmp_path)
    b_item = mgr.items_for(SECTION_LIVE, source_id="b")[0]
    mgr.record_recent(b_item)
    assert [r["item_id"] for r in mgr.recent("b")] == [b_item.id]
    assert mgr.recent("a") == []
    mgr.shutdown()


def test_background_load_does_not_steal_the_active_source(tmp_path):
    """The user picks provider B; A's slower load must not yank the view back."""
    from iptv.manager import IPTVManager
    a = PlaylistSource(id="a", name="A", kind="m3u_url", url="http://a/p.m3u")
    b = PlaylistSource(id="b", name="B", kind="m3u_url", url="http://b/p.m3u")
    mgr = IPTVManager(sources=[a, b], data_dir=str(tmp_path))
    mgr.set_active_source("b")
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.content = b"#EXTM3U\n#EXTINF:-1,CNN\nhttp://s/1.ts\n"
    with mock.patch("iptv.manager.requests.get", return_value=resp):
        mgr._refresh_source(a, None, None, False)
    assert mgr._active_source_id == "b"
    mgr.shutdown()


def test_load_all_async_loads_every_enabled_source(tmp_path):
    from iptv.manager import IPTVManager
    a = PlaylistSource(id="a", name="A", kind="m3u_url", url="http://a/p.m3u")
    b = PlaylistSource(id="b", name="B", kind="m3u_url", url="http://b/p.m3u")
    off = PlaylistSource(id="c", name="C", kind="m3u_url", url="http://c/p.m3u",
                         enabled=False)
    mgr = IPTVManager(sources=[a, b, off], data_dir=str(tmp_path))
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.content = b"#EXTM3U\n#EXTINF:-1,CNN\nhttp://s/1.ts\n"
    done = []
    with mock.patch("iptv.manager.requests.get", return_value=resp):
        mgr.load_all_async(on_done=lambda ok, pl: done.append(pl.source_id),
                           use_cache=False).join(timeout=30)
    assert set(mgr.loaded_source_ids()) == {"a", "b"}   # disabled one skipped
    assert set(done) == {"a", "b"}
    mgr.shutdown()


def _iptv_tab_with_two_sources(tmp_path):
    """An IPTVTab whose manager already holds two playlists (no network)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from config import DeeptorrentConfig, IPTVSourceConfig
    from gui import iptv_tab as tab_mod

    cfg = DeeptorrentConfig()
    cfg.iptv.cache_dir = str(tmp_path)
    cfg.iptv.sources = [
        IPTVSourceConfig(id="a", name="Provider A", kind="m3u_url", url="http://a/p.m3u"),
        IPTVSourceConfig(id="b", name="Provider B", kind="m3u_url", url="http://b/p.m3u"),
    ]
    # Never touch the network during construction.
    with mock.patch.object(tab_mod.IPTVTab, "_refresh", lambda self: None):
        tab = tab_mod.IPTVTab(cfg)
    for sid, cat in (("a", "News"), ("b", "Sport")):
        pl = Playlist(source_id=sid)
        pl.channels.append(Channel(id=make_id(sid, cat), name=f"{sid} chan",
                                   url=f"http://{sid}/1.ts", group=cat,
                                   section=SECTION_LIVE))
        pl.categories = [Category(name=cat, section=SECTION_LIVE, count=1)]
        tab._manager._playlists[sid] = pl
    return tab


def _close_tab(tab):
    """Full teardown: the tab owns a player as well as the manager, and
    leaving its threads running can crash the interpreter at exit."""
    try:
        tab.shutdown()
    except Exception:
        tab._manager.shutdown()
    tab.deleteLater()


def _node_key(node):
    """The (source_id, section, category) tuple a tree node carries."""
    from PySide6.QtCore import Qt
    return node.data(0, Qt.UserRole)


def _child_by_key(parent, key):
    return next(parent.child(i) for i in range(parent.childCount())
                if _node_key(parent.child(i)) == key)


def test_sidebar_is_source_then_section_then_category(tmp_path):
    """Both providers are listed at once — switching is a click, not a reload."""
    tab = _iptv_tab_with_two_sources(tmp_path)
    tab._rebuild_sidebar()
    tree = tab._tree
    assert tree.topLevelItemCount() == 2
    names = [tree.topLevelItem(i).text(0) for i in range(2)]
    assert names[0].startswith("Provider A") and names[1].startswith("Provider B")

    src_a = tree.topLevelItem(0)
    # Sections hang off the source.
    sections = [_node_key(src_a.child(i)) for i in range(src_a.childCount())]
    assert ("a", SECTION_LIVE, "") in sections
    # Categories hang off the section, and belong to that source.
    live = _child_by_key(src_a, ("a", SECTION_LIVE, ""))
    assert [_node_key(live.child(i)) for i in range(live.childCount())] \
        == [("a", SECTION_LIVE, "News")]

    src_b = tree.topLevelItem(1)
    live_b = _child_by_key(src_b, ("b", SECTION_LIVE, ""))
    assert [_node_key(live_b.child(i)) for i in range(live_b.childCount())] \
        == [("b", SECTION_LIVE, "Sport")]
    _close_tab(tab)


def test_unloaded_source_shows_as_loading(tmp_path):
    """Sources still downloading are visible immediately, not missing."""
    tab = _iptv_tab_with_two_sources(tmp_path)
    del tab._manager._playlists["b"]
    tab._rebuild_sidebar()
    second = tab._tree.topLevelItem(1)
    assert "loading" in second.text(0).lower()
    assert second.childCount() == 0
    _close_tab(tab)


def _add_year_movies(pl, sid):
    """One category with two movies (one yearless) + a separate XXX one."""
    pl.movies.append(Movie(id=make_id(sid, "m1"), name="Film One 2024",
                           url=f"http://{sid}/f1.mkv", section=SECTION_MOVIES,
                           year="2024", group="Films"))
    pl.movies.append(Movie(id=make_id(sid, "m2"), name="Yearless",
                           url=f"http://{sid}/f2.mkv", section=SECTION_MOVIES,
                           group="Films"))
    pl.movies.append(Movie(id=make_id(sid, "m3"), name="Adult Film 2025",
                           url=f"http://{sid}/f3.mkv", section=SECTION_MOVIES,
                           year="2025", group="XXX VOD"))
    pl.categories += [Category(name="Films", section=SECTION_MOVIES, count=2),
                      Category(name="XXX VOD", section=SECTION_MOVIES, count=1)]


def test_sidebar_groups_movies_by_year(tmp_path):
    """Group:Years nests year buckets under each category (VOD vs XXX kept
    apart), and a year node is a 4-tuple (src, section, category, year)."""
    tab = _iptv_tab_with_two_sources(tmp_path)
    _add_year_movies(tab._manager._playlists["a"], "a")
    tab._config.iptv.vod_group_mode = "year"
    tab._rebuild_sidebar()

    src_a = tab._tree.topLevelItem(0)
    movies = _child_by_key(src_a, ("a", SECTION_MOVIES, ""))
    # Categories stay the first level in year mode.
    assert [_node_key(movies.child(i)) for i in range(movies.childCount())] \
        == [("a", SECTION_MOVIES, "Films"), ("a", SECTION_MOVIES, "XXX VOD")]
    films = _child_by_key(movies, ("a", SECTION_MOVIES, "Films"))
    assert [_node_key(films.child(i)) for i in range(films.childCount())] == [
        ("a", SECTION_MOVIES, "Films", "2024"),
        ("a", SECTION_MOVIES, "Films", YEAR_OTHERS)]
    xxx = _child_by_key(movies, ("a", SECTION_MOVIES, "XXX VOD"))
    assert [_node_key(xxx.child(i)) for i in range(xxx.childCount())] == [
        ("a", SECTION_MOVIES, "XXX VOD", "2025")]
    # Live TV is unaffected by the year grouping.
    live = _child_by_key(src_a, ("a", SECTION_LIVE, ""))
    assert [_node_key(live.child(i)) for i in range(live.childCount())] \
        == [("a", SECTION_LIVE, "News")]

    # A year node filters by category AND year; its parent category by
    # category alone.
    tab._on_tree_click(_child_by_key(films, ("a", SECTION_MOVIES, "Films", "2024")), 0)
    assert [i.name for i in tab._current_items] == ["Film One 2024"]
    assert tab._current_category == "Films" and tab._current_year == "2024"
    tab._on_tree_click(_child_by_key(films, ("a", SECTION_MOVIES, "Films", YEAR_OTHERS)), 0)
    assert [i.name for i in tab._current_items] == ["Yearless"]
    tab._on_tree_click(films, 0)
    assert [i.name for i in tab._current_items] == ["Film One 2024", "Yearless"]
    tab._on_tree_click(_child_by_key(xxx, ("a", SECTION_MOVIES, "XXX VOD", "2025")), 0)
    assert [i.name for i in tab._current_items] == ["Adult Film 2025"]
    _close_tab(tab)


def test_group_mode_toggle_persists_and_reshows(tmp_path):
    """Flipping the Group combo saves the mode and rebuilds the tree.

    default_config_path is patched to tmp_path — the handler must never
    write the real ~/.deeptorrent/config.json from a test."""
    tab = _iptv_tab_with_two_sources(tmp_path)
    _add_year_movies(tab._manager._playlists["a"], "a")
    # Years is the default mode.
    assert tab._group_combo.currentData() == "year"
    from config import DeeptorrentConfig
    cfg_path = str(tmp_path / "config.json")
    with mock.patch.object(DeeptorrentConfig, "default_config_path",
                           return_value=cfg_path):
        tab._group_combo.setCurrentIndex(0)  # "Categories"
    assert tab._config.iptv.vod_group_mode == "category"
    assert DeeptorrentConfig.from_file(cfg_path).iptv.vod_group_mode == "category"
    src_a = tab._tree.topLevelItem(0)
    movies = _child_by_key(src_a, ("a", SECTION_MOVIES, ""))
    # Flat categories again, with no year grandchildren.
    films = _child_by_key(movies, ("a", SECTION_MOVIES, "Films"))
    assert films.childCount() == 0
    _close_tab(tab)


def test_clicking_another_sources_category_switches_view(tmp_path):
    tab = _iptv_tab_with_two_sources(tmp_path)
    tab._current_source_id = "a"
    tab._rebuild_sidebar()
    src_b = tab._tree.topLevelItem(1)
    live_b = _child_by_key(src_b, ("b", SECTION_LIVE, ""))
    tab._on_tree_click(live_b.child(0), 0)
    assert tab._current_source_id == "b"
    assert tab._current_category == "Sport"
    assert [i.name for i in tab._current_items] == ["b chan"]
    _close_tab(tab)


def test_sidebar_rebuild_preserves_expansion_and_selection(tmp_path):
    """A source finishing its load must not collapse the tree under the user."""
    tab = _iptv_tab_with_two_sources(tmp_path)
    tab._rebuild_sidebar()
    src_b = tab._tree.topLevelItem(1)
    live_b = _child_by_key(src_b, ("b", SECTION_LIVE, ""))
    src_b.setExpanded(True)
    live_b.setExpanded(True)
    tab._tree.setCurrentItem(live_b)

    tab._rebuild_sidebar()   # e.g. another source just landed

    src_b2 = tab._tree.topLevelItem(1)
    live_b2 = _child_by_key(src_b2, ("b", SECTION_LIVE, ""))
    assert src_b2.isExpanded()
    assert live_b2.isExpanded()
    cur = tab._tree.currentItem()
    assert cur is not None and _node_key(cur) == ("b", SECTION_LIVE, "")
    _close_tab(tab)


def test_refresh_source_applies_epg_url_override(tmp_path):
    """A per-source EPG URL fills in when the playlist declares no url-tvg."""
    from iptv.manager import IPTVManager
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/pl.m3u",
                         epg_url="http://x/epg.xml")
    mgr = IPTVManager(sources=[src], tmdb_api_key="", data_dir=str(tmp_path))
    body = b"#EXTM3U\n#EXTINF:-1,CNN\nhttp://s/1.ts\n"

    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.content = body
    with mock.patch("iptv.manager.requests.get", return_value=resp):
        with mock.patch.object(mgr.epg, "update_async") as epg_update:
            pl = mgr._refresh_source(src, None, None, False)
    assert pl.url_tvg == "http://x/epg.xml"
    epg_update.assert_called_once()
    assert epg_update.call_args[0][0] == "http://x/epg.xml"


def _m3u_resp(body: bytes):
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.content = body
    return resp


def test_refresh_source_global_epg_url_applies(tmp_path):
    """The global EPG URL (Metadata & Cache) applies to sources without their
    own per-source URL — and wins over the playlist's own url-tvg."""
    from iptv.manager import IPTVManager
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/pl.m3u")
    mgr = IPTVManager(sources=[src], tmdb_api_key="", data_dir=str(tmp_path),
                      epg_url="http://x/global-epg.xml")
    body = b'#EXTM3U url-tvg="http://x/list-epg.xml"\n#EXTINF:-1,CNN\nhttp://s/1.ts\n'
    with mock.patch("iptv.manager.requests.get", return_value=_m3u_resp(body)):
        with mock.patch.object(mgr.epg, "update_async") as epg_update:
            pl = mgr._refresh_source(src, None, None, False)
    assert pl.url_tvg == "http://x/global-epg.xml"
    assert epg_update.call_args[0][0] == "http://x/global-epg.xml"


def test_refresh_source_epg_precedence_per_source_beats_global(tmp_path):
    from iptv.manager import IPTVManager
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/pl.m3u",
                         epg_url="http://x/per-source.xml")
    mgr = IPTVManager(sources=[src], tmdb_api_key="", data_dir=str(tmp_path),
                      epg_url="http://x/global-epg.xml")
    body = b"#EXTM3U\n#EXTINF:-1,CNN\nhttp://s/1.ts\n"
    with mock.patch("iptv.manager.requests.get", return_value=_m3u_resp(body)):
        with mock.patch.object(mgr.epg, "update_async") as epg_update:
            pl = mgr._refresh_source(src, None, None, False)
    assert pl.url_tvg == "http://x/per-source.xml"
    assert epg_update.call_args[0][0] == "http://x/per-source.xml"


def test_refresh_source_playlist_tvg_when_no_override(tmp_path):
    from iptv.manager import IPTVManager
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/pl.m3u")
    mgr = IPTVManager(sources=[src], tmdb_api_key="", data_dir=str(tmp_path))
    body = b'#EXTM3U url-tvg="http://x/list-epg.xml"\n#EXTINF:-1,CNN\nhttp://s/1.ts\n'
    with mock.patch("iptv.manager.requests.get", return_value=_m3u_resp(body)):
        with mock.patch.object(mgr.epg, "update_async") as epg_update:
            pl = mgr._refresh_source(src, None, None, False)
    assert pl.url_tvg == "http://x/list-epg.xml"
    assert epg_update.call_args[0][0] == "http://x/list-epg.xml"


def test_refresh_source_epg_disabled_skips_update(tmp_path):
    """The enable_epg toggle gates the guide download."""
    from iptv.manager import IPTVManager
    src = PlaylistSource(id="s", name="S", kind="m3u_url", url="http://x/pl.m3u")
    mgr = IPTVManager(sources=[src], tmdb_api_key="", data_dir=str(tmp_path),
                      enable_epg=False)
    body = b'#EXTM3U url-tvg="http://x/list-epg.xml"\n#EXTINF:-1,CNN\nhttp://s/1.ts\n'
    with mock.patch("iptv.manager.requests.get", return_value=_m3u_resp(body)):
        with mock.patch.object(mgr.epg, "update_async") as epg_update:
            mgr._refresh_source(src, None, None, False)
    epg_update.assert_not_called()


def test_manager_set_epg_fetches_only_on_change(tmp_path):
    from iptv.manager import IPTVManager
    mgr, _pl = _manager_with_playlist(tmp_path)
    with mock.patch.object(mgr.epg, "update_async") as epg_update:
        mgr.set_epg("http://x/epg.xml", True)
        assert epg_update.call_count == 1
        mgr.set_epg("http://x/epg.xml", True)   # unchanged — no refetch
        assert epg_update.call_count == 1
        mgr.set_epg("http://x/other.xml", True)
        assert epg_update.call_count == 2
        mgr.set_epg("http://x/third.xml", False)  # disabled — no fetch
        assert epg_update.call_count == 2
    mgr.shutdown()


def test_parse_xmltv_tolerates_truncation(tmp_path):
    """A server that closes mid-document (verified: epg.mybunny.tv stops
    cold at line 112659) yields the partial guide instead of nothing."""
    from iptv.epg import parse_xmltv
    p = tmp_path / "epg.xml"
    p.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?><tv>'
        b'<programme start="20260830120000 +0000" stop="20260830130000 +0000" channel="c1">'
        b"<title>Kept</title></programme>"
        b'<programme start="20260830130000 +0000" stop="20260830140000 +0000" channel="c1">'
        b"<title>Cut off mid-wa"  # truncated mid-tag: ParseError at EOF
    )
    programs = parse_xmltv(str(p))
    assert len(programs) == 1
    assert programs[0]["title"] == "Kept"


def test_parse_xmltv_latin1_payload_despite_utf8_declaration(tmp_path):
    """Providers that declare UTF-8 but serve latin-1 bytes get decoded as
    latin-1 — otherwise iterparse dies on the first accented title."""
    from iptv.epg import parse_xmltv
    p = tmp_path / "epg.xml"
    p.write_bytes(
        '<?xml version="1.0" encoding="UTF-8"?><tv>'
        '<programme start="20260830120000 +0000" stop="20260830130000 +0000" channel="c1">'
        "<title>Memória</title></programme></tv>".encode("latin-1")
    )
    programs = parse_xmltv(str(p))
    assert programs[0]["title"] == "Memória"


def test_parse_xmltv_collects_channels(tmp_path):
    """with_channels=True returns the guide's <channel> directory too — the
    surface for name-based tvg-id fallback matching."""
    from iptv.epg import parse_xmltv
    p = tmp_path / "e.xml"
    p.write_bytes(
        b'<?xml version="1.0"?><tv>'
        b'<channel id="c1"><display-name>CNN International</display-name>'
        b'<icon src="http://x/i.png"/></channel>'
        b'<programme start="20260830120000 +0000" stop="20260830130000 +0000" channel="c1">'
        b"<title>T</title></programme></tv>"
    )
    programs, channels = parse_xmltv(str(p), with_channels=True)
    assert programs[0]["title"] == "T"
    assert channels == [("c1", "CNN International")]
    # Default return shape unchanged (programmes list only).
    assert isinstance(parse_xmltv(str(p)), list)


def test_manager_epg_name_fallback_resolution(tmp_path):
    """No tvg-id: the channel name is normalized and matched against the
    guide's display names ('PT: SPORT TV 1 FHD' ~ 'PT: SPORT TV 1 2K')."""
    mgr, _pl = _manager_with_playlist(tmp_path)
    now = time.time()
    mgr.cache.save_epg("url", [
        {"channel_id": "SPORT.TV1.HD.pt", "start": int(now - 60),
         "end": int(now + 600), "title": "Football", "desc": ""},
    ], channels=[("SPORT.TV1.HD.pt", "PT: SPORT TV 1 2K")])
    assert mgr.epg_channel_id("", "PT: SPORT TV 1 FHD") == "SPORT.TV1.HD.pt"
    nn = mgr.epg_now_next_for("", "PT: SPORT TV 1 FHD")
    assert nn["now"] == "Football"
    assert mgr.epg_channel_id("", "Nonexistent Channel") == ""  # miss, memoized
    mgr.shutdown()


def test_manager_epg_tvg_id_wins_over_name(tmp_path):
    mgr, _pl = _manager_with_playlist(tmp_path)
    now = time.time()
    mgr.cache.save_epg("url", [
        {"channel_id": "tvg-1", "start": int(now - 60), "end": int(now + 600),
         "title": "Show", "desc": ""},
    ], channels=[("tvg-1", "Some Other Name")])
    assert mgr.epg_channel_id("tvg-1", "Unrelated Playlist Name") == "tvg-1"
    mgr.shutdown()


def test_manager_epg_guide_for(tmp_path):
    mgr, _pl = _manager_with_playlist(tmp_path)
    now = time.time()
    mgr.cache.save_epg("url", [
        {"channel_id": "c1", "start": int(now - 60), "end": int(now + 600), "title": "On Air", "desc": ""},
        {"channel_id": "c1", "start": int(now + 600), "end": int(now + 1200), "title": "Later", "desc": ""},
        {"channel_id": "c1", "start": int(now - 7200), "end": int(now - 3600), "title": "Gone", "desc": ""},
    ])
    guide = mgr.epg_guide_for("c1")
    assert guide["now_next"]["now"] == "On Air"
    titles = [p["title"] for p in guide["programmes"]]
    assert titles == ["On Air", "Later"]  # past programme excluded
    assert mgr.epg_guide_for("", "")["programmes"] == []
    mgr.shutdown()


def test_manager_maybe_refresh_epg(tmp_path):
    """Stale guides re-fetch; fresh ones are skipped; the toggle gates it."""
    mgr, _pl = _manager_with_playlist(tmp_path)
    mgr.epg_url = "http://x/epg.xml"
    with mock.patch.object(mgr.epg, "update_async") as upd:
        mgr.maybe_refresh_epg()          # never fetched -> stale
        assert upd.call_count == 1
        mgr.cache.save_epg("http://x/epg.xml", [
            {"channel_id": "c1", "start": 1, "end": 2, "title": "t", "desc": ""},
        ])
        mgr.maybe_refresh_epg()          # just fetched -> fresh
        assert upd.call_count == 1
        mgr.cache._conn().execute("UPDATE epg_meta SET updated_at=0 WHERE url=?",
                                  ("http://x/epg.xml",))
        mgr.cache._conn().commit()
        mgr.maybe_refresh_epg()          # stale again -> refetch
        assert upd.call_count == 2
        mgr.enable_epg = False
        mgr.maybe_refresh_epg()          # disabled -> skip
        assert upd.call_count == 2
    mgr.shutdown()


def test_config_epg_url_roundtrip(tmp_path):
    """The global EPG URL persists through save/load (temp path — never the
    real ~/.deeptorrent/config.json)."""
    from config import DeeptorrentConfig
    path = str(tmp_path / "config.json")
    cfg = DeeptorrentConfig()
    cfg.iptv.epg_url = "https://epg.example.com/guide.xml"
    cfg.to_file(path)
    assert DeeptorrentConfig.from_file(path).iptv.epg_url == "https://epg.example.com/guide.xml"
    assert DeeptorrentConfig().iptv.epg_url == ""  # default empty


def test_epg_timezone_offsets_are_honored(tmp_path):
    """'+0200' must land on the same epoch as the equivalent UTC time —
    otherwise every programme shifts by the offset."""
    from iptv.epg import _parse_xmltv_date
    assert _parse_xmltv_date("20260830120000 +0200") == _parse_xmltv_date("20260830100000 +0000")
    assert _parse_xmltv_date("20260830100000") == _parse_xmltv_date("20260830100000 +0000")


def test_epg_now_next_returns_times(tmp_path):
    import time as _time
    cache = IPTVCache(str(tmp_path))
    now = _time.time()
    cache.save_epg("url", [
        {"channel_id": "c1", "start": int(now - 60), "end": int(now + 600), "title": "On Air", "desc": ""},
        {"channel_id": "c1", "start": int(now + 600), "end": int(now + 1200), "title": "Later", "desc": ""},
    ])
    nn = cache.epg_now_next("c1")
    assert nn["now"] == "On Air" and nn["next"] == "Later"
    assert nn["now_start"] == int(now - 60) and nn["now_end"] == int(now + 600)
    assert nn["next_start"] == int(now + 600)


def test_fmt_now_title_uses_system_local_time():
    """Rendering goes through time.localtime — automatically the system TZ."""
    from gui.iptv_tab import _fmt_now_title
    start = 1788000000
    end = start + 3600
    expected = (f"{time.strftime('%H:%M', time.localtime(start))}–"
                f"{time.strftime('%H:%M', time.localtime(end))}  Show")
    assert _fmt_now_title({"now": "Show", "now_start": start, "now_end": end}) == expected
    assert _fmt_now_title({"now": "Show"}) == "Show"
    assert _fmt_now_title({"now": ""}) == ""


def test_detail_panel_channel_shows_epg_now_next(tmp_path):
    """The channel detail panel reads the EPG store live — the epg_now item
    attribute was never populated anywhere, so the panel was blank before."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui.iptv_tab import DetailPanel

    mgr, pl = _manager_with_playlist(tmp_path)
    ch = pl.channels[0]
    ch.tvg_id = "cnn"
    now = time.time()
    mgr.cache.save_epg("url", [
        {"channel_id": "cnn", "start": int(now - 60), "end": int(now + 600), "title": "On Air", "desc": ""},
        {"channel_id": "cnn", "start": int(now + 600), "end": int(now + 1200), "title": "Later", "desc": ""},
    ])
    panel = DetailPanel(mgr)
    panel._show_channel(ch)
    text = panel.synopsis.text()
    assert "Now:" in text and "On Air" in text
    assert "Next:" in text and "Later" in text
    assert time.strftime("%H:%M", time.localtime(int(now - 60))) in text
    # The 24h guide list is populated and display-only (clicking plays nothing).
    from PySide6.QtCore import Qt
    assert panel.episodes_label.isHidden() is False
    assert panel.episodes.count() == 2
    assert "On Air" in panel.episodes.item(0).text()
    assert panel.episodes.item(0).data(Qt.UserRole) is None
    mgr.shutdown()


def test_grid_live_tiles_show_now_playing(tmp_path):
    """Visible live tiles get the current programme as their second text line."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui.iptv_tab import ContentGrid, _EPG_ROLE

    mgr, pl = _manager_with_playlist(tmp_path)
    ch = pl.channels[0]
    ch.tvg_id = "cnn"
    now = time.time()
    mgr.cache.save_epg("url", [
        {"channel_id": "cnn", "start": int(now - 60), "end": int(now + 600),
         "title": "On Air", "desc": ""},
    ])
    grid = ContentGrid(mgr)
    grid.set_items(pl.channels)
    grid._update_epg_tiles(full=True)
    assert "On Air" in (grid.item(0).data(_EPG_ROLE) or "")
    mgr.shutdown()


# ---------------------------------------------------------------------------
# Channel logos: artwork download hardening + iptv-org name matching
# ---------------------------------------------------------------------------

def _artwork_response(status=200, body=b"", ctype="image/png"):
    resp = mock.Mock()
    resp.status_code = status
    resp.headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
    resp.iter_content = lambda n: [body]
    resp.close = mock.Mock()
    return resp


_PNG_2PX = bytes.fromhex(  # 2x2 RGBA PNG
    "89504e470d0a1a0a0000000d494844520000000200000002080600000072b60d24"
    "0000001549444154789c63fccfc0f09f8181818109448030001f1702020247b314"
    "0000000049454e44ae426082"
)


def test_artwork_retries_connection_resets(tmp_path):
    """Logo CDNs reset bursty connections — a fetch must retry, not give up."""
    from iptv.artwork import ArtworkCache
    import requests as _rq
    cache = ArtworkCache(str(tmp_path))
    calls = {"n": 0}

    def _get(url, headers=None, timeout=None, stream=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _rq.ConnectionError("reset")
        return _artwork_response(body=_PNG_2PX)

    with mock.patch("iptv.artwork._session") as sess:
        sess.return_value.get.side_effect = _get
        with mock.patch("iptv.artwork.time.sleep"):
            path = cache._download("http://cdn/logo.png", None)
    assert calls["n"] == 3
    assert path and os.path.isfile(path)


def test_artwork_no_retry_on_404(tmp_path):
    from iptv.artwork import ArtworkCache
    cache = ArtworkCache(str(tmp_path))
    with mock.patch("iptv.artwork._session") as sess:
        sess.return_value.get.return_value = _artwork_response(status=404)
        assert cache._download("http://cdn/gone.png", None) is None
        assert sess.return_value.get.call_count == 1


def test_artwork_rejects_html_error_page(tmp_path):
    """A 200 carrying an HTML error page must not be cached as artwork."""
    from iptv.artwork import ArtworkCache
    cache = ArtworkCache(str(tmp_path))
    with mock.patch("iptv.artwork._session") as sess:
        sess.return_value.get.return_value = _artwork_response(
            body=b"<!DOCTYPE html><html>404</html>" * 4, ctype="text/html")
        assert cache._download("http://cdn/x.png", None) is None
    assert not os.path.isfile(cache.full_path("http://cdn/x.png"))


def test_artwork_rejects_undecodable_payload(tmp_path):
    """image/* content type but garbage bytes — reject instead of caching."""
    from iptv.artwork import ArtworkCache
    cache = ArtworkCache(str(tmp_path))
    with mock.patch("iptv.artwork._session") as sess:
        sess.return_value.get.return_value = _artwork_response(body=b"not an image" * 10)
        assert cache._download("http://cdn/y.png", None) is None
    assert not os.path.isfile(cache.full_path("http://cdn/y.png"))


def test_iptvorg_logo_index_joins_channels_and_logos(tmp_path):
    """logos.json is keyed by channel id and has no names — it must be joined
    against channels.json, preferring raster formats over SVG."""
    from iptv.cache import IPTVCache
    from iptv.metadata import IptvOrgLogos
    logos = [
        {"channel": "SkyNews.uk", "feed": None, "in_use": True, "format": "SVG",
         "url": "http://x/sky.svg"},
        {"channel": "SkyNews.uk", "feed": None, "in_use": True, "format": "PNG",
         "url": "http://x/sky.png"},
    ]
    channels = [{"id": "SkyNews.uk", "name": "Sky News", "alt_names": ["Sky News UK"],
                 "country": "UK"}]

    def _get(url, timeout=None):
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.json = lambda: logos if "logos" in url else channels
        return resp

    org = IptvOrgLogos(IPTVCache(str(tmp_path)))
    with mock.patch("iptv.metadata.requests.get", side_effect=_get):
        # Playlist decorations (channel number, country prefix, quality tag)
        # must all be stripped before matching.
        assert org.lookup("60 UK: SKY NEWS FHD") == "http://x/sky.png"
    assert org.lookup("Sky News") == "http://x/sky.png"   # served from memory
    assert org.lookup("Some Random Event 47") == ""


@pytest.mark.parametrize("raw,expected", [
    ("60 UK: SKY SPORTS MAIN EVENT FHD", ("skysportsmainevent", "uk")),
    ("Portugal  NICKELODEON", ("nickelodeon", "pt")),
    ("(PT) (Meo)  Discovery Channel", ("discoverychannel", "")),
    ("Discovery Channel", ("discoverychannel", "")),  # no country word to eat
    ("BBC One HD", ("bbcone", "")),
])
def test_iptvorg_name_normalization(raw, expected):
    from iptv.metadata import IptvOrgLogos
    assert IptvOrgLogos._split_country(raw) == expected


def test_placeholder_pixmap_is_per_channel():
    """No-logo channels get initials, not an identical grey box."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui.iptv_tab import _initials, _placeholder_pixmap
    assert _initials("BBC News HD") == "BN"
    assert _initials("Eurosport") == "EUR"
    a = _placeholder_pixmap("BBC News")
    b = _placeholder_pixmap("Sky Sports F1")
    assert not a.isNull() and not b.isNull()
    assert a.toImage() != b.toImage()
    assert _placeholder_pixmap("BBC News") is a  # cached


def test_grid_shares_one_logo_across_channels(tmp_path):
    """Hundreds of channels share a single logo URL — every tile must get the
    icon, not just whichever one requested it first."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui.iptv_tab import ContentGrid

    png = tmp_path / "logo.png"
    png.write_bytes(_PNG_2PX)
    url = "http://cdn/shared.png"
    manager = mock.Mock()
    grid = ContentGrid(manager)
    grid.set_items([Channel(id=f"c{i}", name=f"Chan {i}", url="http://s/x", logo=url)
                    for i in range(3)])
    grid.resize(600, 800)
    grid.show()

    grid._load_visible_artwork()
    assert manager.fetch_artwork.call_count == 1  # deduped
    assert len(grid._artwork_requests[url]) == 3  # …but all tiles are attached
    grid._apply_artwork(url, str(png))
    grid._decode_step()  # decoding is spread over event-loop turns
    assert all(not grid.item(i).icon().isNull() for i in range(3))

    # A tile discovered after the fetch finished still gets the cached pixmap.
    grid._load_visible_artwork()
    assert manager.fetch_artwork.call_count == 1
    assert url in grid._pixmaps
    grid.hide()


def test_grid_falls_back_to_iptvorg_when_logo_url_dead(tmp_path):
    """A dead tvg-logo must trigger the iptv-org lookup instead of leaving a
    placeholder forever — and must not be re-queued endlessly."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui.iptv_tab import ContentGrid

    url = "http://cdn/dead.png"
    manager = mock.Mock()
    grid = ContentGrid(manager)
    ch = Channel(id="c1", name="Sky News", url="http://s/x", logo=url)
    grid.set_items([ch])
    grid.resize(400, 400)
    grid.show()
    grid._load_visible_artwork()

    # One failure can just be the CDN resetting a burst — the URL gets
    # another pass before we declare it dead.
    grid._on_artwork_failed(url)
    manager.resolve_channel_logo_async.assert_not_called()
    assert url not in grid._failed_urls
    grid._load_visible_artwork()
    assert manager.fetch_artwork.call_count == 2  # retried

    grid._on_artwork_failed(url)
    manager.resolve_channel_logo_async.assert_called_once()
    assert ch.logo == ""              # cleared so the fallback can run
    assert url in grid._failed_urls   # now blacklisted
    grid._load_visible_artwork()
    assert manager.fetch_artwork.call_count == 2  # never re-queued
    grid.hide()


def test_grid_resolves_missing_vod_poster_via_metadata(tmp_path):
    """A movie with no artwork in the playlist must fall back to TMDb/TVmaze
    (channels get iptv-org) instead of keeping the placeholder forever."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui.iptv_tab import ContentGrid

    manager = mock.Mock()
    grid = ContentGrid(manager)
    mv = Movie(id="m1", name="Leviticus (2026) 1080p WEBRip", url="http://s/m.mkv")
    grid.set_items([mv])
    grid.resize(400, 400)
    grid.show()
    grid._load_visible_artwork()

    manager.resolve_poster_async.assert_called_once()
    manager.resolve_channel_logo_async.assert_not_called()
    # Resolved posters land in .poster (what the detail panel reads) and are
    # fetched exactly once.
    grid._apply_logo(mv, "http://tmdb/poster.jpg")
    assert mv.poster == "http://tmdb/poster.jpg"
    manager.fetch_artwork.assert_called_once_with("http://tmdb/poster.jpg", grid._on_artwork)
    grid._load_visible_artwork()
    assert manager.resolve_poster_async.call_count == 1  # not retried in a loop
    grid.hide()


def test_grid_sweeps_all_artworkless_entries():
    """Every artwork-less entry in the section gets resolved in the
    background, not just the handful currently on screen."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui.iptv_tab import ContentGrid, _SWEEP_BATCH

    manager = mock.Mock()
    grid = ContentGrid(manager)
    items = [Movie(id=f"m{i}", name=f"Movie {i}", url="http://s/x") for i in range(12)]
    items += [Movie(id="has", name="Has Art", url="http://s/y", logo="http://s/a.jpg")]
    grid.set_items(items)
    assert len(grid._sweep_queue) == 12   # the one with artwork isn't queued
    assert grid._sweep_timer.isActive()

    grid._sweep_step()
    assert manager.resolve_poster_async.call_count == _SWEEP_BATCH  # paced
    for _ in range(5):
        grid._sweep_step()
    assert manager.resolve_poster_async.call_count == 12  # …until all are done
    assert not grid._sweep_timer.isActive()

    # Progress is reported so the (deliberately slow) sweep is visible.
    seen = []
    grid.sweep_progress.connect(lambda d, t: seen.append((d, t)))
    grid._apply_logo(items[0], "http://tmdb/a.jpg")
    grid._apply_logo(items[1], "")  # a miss still counts as progress
    assert seen == [(1, 12), (2, 12)]

    # Switching sections abandons the sweep.
    grid.set_items([Movie(id="z", name="Z", url="http://s/z", logo="http://s/z.jpg")])
    assert not grid._sweep_queue and not grid._sweep_timer.isActive()
    assert seen[-1] == (0, 0)  # …and resets the indicator


def test_grid_prefetches_beyond_the_viewport():
    """Artwork is fetched well ahead of the viewport so scrolling doesn't
    reveal placeholders — but not for the entire 30k-entry section."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui.iptv_tab import ContentGrid, _PREFETCH_TILES

    manager = mock.Mock()
    grid = ContentGrid(manager)
    n = _PREFETCH_TILES * 4
    grid.set_items([Movie(id=f"m{i}", name=f"M{i}", url="http://s/x",
                          logo=f"http://s/{i}.jpg") for i in range(n)])
    grid.resize(600, 400)
    grid.show()
    grid._load_visible_artwork()
    fetched = manager.fetch_artwork.call_count
    assert fetched > 100, "prefetch window is too small"
    assert fetched < n, "the whole section should not be queued at once"
    grid.hide()


def test_grid_single_click_is_info_double_click_is_play():
    """Single click must not start playback — browsing a section shouldn't
    fire off streams."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui.iptv_tab import ContentGrid

    grid = ContentGrid(mock.Mock())
    mv = Movie(id="m1", name="Film", url="http://s/m.mkv")
    grid.set_items([mv])
    selected, activated = [], []
    grid.itemSelected.connect(selected.append)
    grid.itemActivated.connect(activated.append)

    grid._on_select(grid.item(0))
    assert selected == [mv] and activated == []
    grid._on_activate(grid.item(0))
    assert activated == [mv]


def test_grid_takes_artwork_found_by_the_detail_panel():
    """Clicking an entry resolves its poster — the tile must pick that up."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui.iptv_tab import ContentGrid

    manager = mock.Mock()
    grid = ContentGrid(manager)
    mv = Movie(id="m1", name="No Art", url="http://s/x")
    grid.set_items([mv])
    grid.apply_external_artwork(mv, "http://tmdb/p.jpg")
    assert mv.poster == "http://tmdb/p.jpg"
    manager.fetch_artwork.assert_called_once_with("http://tmdb/p.jpg", grid._on_artwork)
    assert id(mv) in grid._logo_tried  # the sweep won't look it up again


def test_manager_resolve_poster_uses_existing_artwork(tmp_path):
    """No lookup when the playlist already carries artwork."""
    from iptv.manager import IPTVManager
    mgr = IPTVManager(sources=[], tmdb_api_key="", data_dir=str(tmp_path))
    mv = Movie(id="m1", name="X", url="http://s/x", logo="http://s/art.jpg")
    got = []
    with mock.patch.object(mgr.metadata, "resolve_async") as res:
        mgr.resolve_poster_async(mv, lambda it, url: got.append(url))
    res.assert_not_called()
    assert got == ["http://s/art.jpg"]


# ---------------------------------------------------------------------------
# Audio-only playback: MilkDrop (Butterchurn) visualization
# ---------------------------------------------------------------------------

def test_milkdrop_assets_are_vendored():
    """The renderer, the .milk converter and the page must ship with the app —
    no CDN at runtime."""
    from gui import milkdrop as md
    assets = md.assets_dir()
    assert assets, "packaging/milkdrop not found"
    for name in ("visualizer.html", "butterchurn.min.js",
                 "milkdrop-preset-converter.min.js"):
        assert os.path.isfile(os.path.join(assets, name)), name
    html = open(os.path.join(assets, "visualizer.html"), encoding="utf-8").read()
    assert "http://" not in html and "https://" not in html


def test_shipped_presets_present():
    from gui import milkdrop as md
    presets = md.list_presets()
    assert presets, "no .milk presets shipped"
    assert all(p.lower().endswith(".milk") for p in presets)
    # Names are shown in the picker, so they must be the bare file title.
    assert md.preset_name(r"C:\x\fiShbRaiN - crazy diamond.milk") == "fiShbRaiN - crazy diamond"


@pytest.mark.parametrize("url,routed", [
    ("C:/music/song.mp3", True),
    ("C:/music/song.FLAC", True),
    ("http://host/stream.m4a?x=1", True),
    ("C:/music/song.wma", False),    # Chromium can't decode it -> mpv
    ("C:/music/song.alac", False),
    ("C:/video/movie.mkv", False),
    ("http://host/live.m3u8", False),
    ("", False),
])
def test_only_decodable_audio_is_routed_to_milkdrop(url, routed):
    from gui import milkdrop as md
    assert md.is_audio_url(url) is routed


def test_audio_file_detection_covers_formats_mpv_keeps():
    from gui import milkdrop as md
    assert md.is_audio_file("x.wma") and not md.is_audio_url("x.wma")
    assert md.is_audio_file("x.mp3")
    assert not md.is_audio_file("x.mkv")


def test_milkdrop_config_defaults():
    from config import DeeptorrentConfig
    cfg = DeeptorrentConfig()
    assert cfg.iptv.milkdrop_enabled is True
    assert cfg.iptv.milkdrop_preset == ""


def test_ffmpeg_visualizers_are_gone():
    """The lavfi visualizers were replaced by MilkDrop — no dead plumbing."""
    import iptv.player as p
    for gone in ("visualizer_graph", "AUDIO_VISUALIZERS", "DEFAULT_VISUALIZER"):
        assert not hasattr(p, gone), gone
    assert not hasattr(p.PlayerBackend, "set_audio_visualizer")


def test_mpv_audio_only_detection_kept():
    """mpv still needs to recognise audio-only media (album art is not video)."""
    from iptv.player import MpvBackend
    assert MpvBackend.is_audio_only([{"type": "audio"}]) is True
    assert MpvBackend.is_audio_only(
        [{"type": "audio"}, {"type": "video", "albumart": True}]) is True
    assert MpvBackend.is_audio_only([{"type": "audio"}, {"type": "video"}]) is False
    assert MpvBackend.is_audio_only([]) is False


def test_milkdrop_view_is_never_a_child_of_the_mpv_surface():
    """A QWebEngineView is a native window: parenting one under the surface
    makes Qt re-create the surface's HWND, mpv loses the window it embedded
    into and its core shuts down — audio kept playing, video went black.
    The view must live in the video stack, beside the surface."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QStackedWidget
    QApplication.instance() or QApplication([])
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget
    from iptv.manager import IPTVManager

    player = PlayerWidget(IPTVManager(sources=[], tmdb_api_key=""),
                          DeeptorrentConfig())
    assert isinstance(player.video_stack, QStackedWidget)
    assert player.video_stack.indexOf(player.surface) >= 0

    md = player._ensure_milkdrop()
    if md is None:
        pytest.skip("QWebEngineView unavailable in this environment")
    assert player.video_stack.indexOf(md.view) >= 0, "view not in the stack"
    # Nothing in the view's parent chain may be the mpv surface.
    node = md.view
    while node is not None:
        assert node is not player.surface, "web view is under the mpv surface"
        node = node.parent()

    # Swapping backends switches the stack page rather than stacking widgets.
    player._use_backend(md)
    assert player.video_stack.currentWidget() is md.view
    player._ensure_backend()  # mpv/VLC, as play() does
    player._use_backend(player._media_backend)
    assert player.video_stack.currentWidget() is player.surface


def test_open_file_dialog_offers_audio_by_default():
    from gui.iptv_tab import IPTVTab
    first = IPTVTab.VIDEO_FILTER.split(";;")[0]
    assert first.startswith("Media Files")
    assert "*.mp3" in first and "*.flac" in first and "*.mkv" in first


def test_vlc_track_normalization():
    """libvlc track-description tuples normalize to backend track dicts."""
    from iptv.player import LibVLCBackend
    tracks = LibVLCBackend._vlc_tracks([(-1, b"Disable"), (2, b"English"), (3, "Français")])
    assert tracks == [
        {"index": 0, "id": -1, "title": "Disable", "lang": ""},
        {"index": 1, "id": 2, "title": "English", "lang": ""},
        {"index": 2, "id": 3, "title": "Français", "lang": ""},
    ]
    assert LibVLCBackend._vlc_tracks(None) == []


def test_probe_duration_missing_file():
    """probe_duration never raises — a missing/unreadable file yields 0.0."""
    from dlmgr.ffmpeg import probe_duration
    assert probe_duration("") == 0.0
    assert probe_duration(r"C:\definitely\not\a\real\file.mkv") == 0.0


def test_opensubtitles_hash(tmp_path):
    """Zero-filled file: chunk sums are 0, so the hash is just the size."""
    from iptv.opensubtitles import opensubtitles_hash
    p = tmp_path / "v.mkv"
    p.write_bytes(b"\0" * (128 * 1024))
    assert opensubtitles_hash(str(p)) == f"{128 * 1024:016x}"
    small = tmp_path / "s.mkv"
    small.write_bytes(b"\0" * 100)
    with pytest.raises(ValueError):
        opensubtitles_hash(str(small))


def test_opensubtitles_requires_key():
    from iptv.opensubtitles import OpenSubtitlesClient, OpenSubtitlesError
    with pytest.raises(OpenSubtitlesError):
        OpenSubtitlesClient("").search(query="x")


class _OSTResp:
    def __init__(self, payload, code=200):
        self._p = payload
        self.status_code = code

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def test_opensubtitles_search_normalizes():
    from iptv.opensubtitles import OpenSubtitlesClient
    payload = {"data": [
        {"attributes": {"release": "Movie.2024.1080p", "language": "en",
                        "download_count": 42, "ratings": 7.5, "hearing_impaired": False,
                        "files": [{"file_id": 123, "file_name": "movie.srt"}],
                        "feature_details": {"title": "Movie", "year": 2024}}},
        {"attributes": {"release": "no files entry", "language": "en", "files": []}},
    ]}
    with mock.patch("iptv.opensubtitles.requests.get", return_value=_OSTResp(payload)):
        res = OpenSubtitlesClient("key").search(query="movie")
    assert len(res) == 1  # entries without files are unusable — skipped
    assert res[0]["file_id"] == 123
    assert res[0]["release"] == "Movie.2024.1080p"
    assert res[0]["year"] == 2024


def test_pick_best_prefers_hash_match():
    from iptv.opensubtitles import pick_best
    results = [
        {"file_id": 1, "language": "en", "downloads": 5000, "rating": 9.0},
        {"file_id": 2, "language": "en", "downloads": 3, "hash_match": True},
    ]
    assert pick_best(results)["file_id"] == 2


def test_pick_best_preferred_language():
    from iptv.opensubtitles import pick_best
    results = [
        {"file_id": 1, "language": "en", "downloads": 5000},
        {"file_id": 2, "language": "es", "downloads": 10},
    ]
    assert pick_best(results, "es")["file_id"] == 2
    assert pick_best(results, "en")["file_id"] == 1
    assert pick_best([], "en") is None


def test_clean_media_query():
    from iptv.opensubtitles import clean_media_query
    assert clean_media_query("Some.Movie.2024.1080p.mkv") == "Some Movie 2024 1080p"
    assert clean_media_query("") == ""


def test_player_lang_matching():
    from gui.iptv_tab import PlayerWidget
    assert PlayerWidget._lang_matches("eng", "en")
    assert PlayerWidget._lang_matches("en", "eng")   # config given as 639-2
    assert PlayerWidget._lang_matches("English", "en")
    assert not PlayerWidget._lang_matches("fra", "en")
    assert not PlayerWidget._lang_matches("", "en")
    assert not PlayerWidget._lang_matches("eng", "")


def test_parse_lang_text():
    """Settings combo values normalize to ISO codes; default yields ''."""
    from gui.iptv_settings_dialog import _parse_lang_text
    assert _parse_lang_text("English (en)") == "en"
    assert _parse_lang_text("es") == "es"
    assert _parse_lang_text("eng") == "eng"
    assert _parse_lang_text("  Ukrainian (uk) ") == "uk"
    assert _parse_lang_text("— Player default —") == ""
    assert _parse_lang_text("") == ""


# ---------------------------------------------------------------------------
# Local media folder sources (iptv/local_folder.py)
# ---------------------------------------------------------------------------

def _mkfile(root, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")


def _plex_like_tree(root) -> None:
    """Mirror the real C:\\PLEX SERVER layout: loose files, movie-per-folder,
    season-per-folder series, and sidecar junk that must be ignored."""
    _mkfile(root, "MOVIES/Dunki.2023.1080p.WEBRip.x264.AAC5.1-WORLD.mp4")
    _mkfile(root, "MOVIES/Dunki.2023.1080p.WEBRip.x264.AAC5.1-WORLD.srt")
    _mkfile(root, "MOVIES/Rumor Has It 2005 1080p NF WEB-DL DDP5 1 AV1-Saon/"
                  "Rumor Has It 2005 1080p NF WEB-DL DDP5 1 AV1-Saon.mkv")
    _mkfile(root, "MOVIES/Rumor Has It 2005 1080p NF WEB-DL DDP5 1 AV1-Saon/"
                  "Rumor Has It 2005 1080p NF WEB-DL DDP5 1 AV1-Saon.mkv.nfo")
    _mkfile(root, "MOVIES/Lanterns.2026.S01E01.1080p.WEB.H264-CAKES/"
                  "lanterns.2026.s01e01.1080p.web.h264-cakes.mkv")
    _mkfile(root, "MOVIES/Lanterns.2026.S01E01.1080p.WEB.H264-CAKES/"
                  "lanterns.2026.s01e01.1080p.web.h264-cakes.en.srt")
    _mkfile(root, "MOVIES/Lanterns.2026.S01E01.1080p.WEB.H264-CAKES/"
                  "Screens/screen0001.jpg")
    _mkfile(root, "MOVIES/Some.Sample.2020.1080p.sample.mkv")
    # Folder and file diverged after a release rename — same group suffix.
    _mkfile(root, "MOVIES/Toy Story 5 (2026) 2160p 4K WEB 5.1-LAMA/"
                  "Toy.Story.5.2026.2160p.4K.WEB.x265.10bit.AAC5.1-LAMA.mkv")
    # A legit subcategory must NOT collapse just because the file starts
    # with the folder's name.
    _mkfile(root, "MOVIES/Comedy/Comedy Central Roast 2024.mkv")
    _mkfile(root, "TV SERIES/Henry.Danger.S01.720p.HDTV.x264-W4F [NO RAR]/"
                  "henry.danger.s01e01.720p.hdtv.x264-w4f.mkv")
    _mkfile(root, "TV SERIES/Henry.Danger.S01.720p.HDTV.x264-W4F [NO RAR]/"
                  "henry.danger.s01e02.720p.hdtv.x264-w4f.mkv")
    _mkfile(root, "TV SERIES/Henry.Danger.S02.1080p.WEB-DL/"
                  "henry.danger.s02e01.1080p.web-dl.mkv")


def _scan(root, sid="local"):
    """Scan + classify + rebuild categories, mirroring _refresh_source_inner."""
    from iptv.manager import _rebuild_categories
    src = PlaylistSource(id=sid, name="Local", kind="local_folder",
                         url=str(root))
    pl = local_folder.scan_folder(src)
    classify(pl)
    _rebuild_categories(pl)
    return pl


def test_local_folder_scan_classifies_movies_and_series(tmp_path):
    _plex_like_tree(tmp_path)
    pl = _scan(tmp_path)
    assert not pl.channels  # nothing local is ever "live TV"
    assert {m.name for m in pl.movies} == {
        "Dunki.2023.1080p.WEBRip.x264.AAC5.1-WORLD",
        "Rumor Has It 2005 1080p NF WEB-DL DDP5 1 AV1-Saon",
        "Toy.Story.5.2026.2160p.4K.WEB.x265.10bit.AAC5.1-LAMA",
        "Comedy Central Roast 2024",
    }
    shows = {s.name: s for s in pl.series}
    assert set(shows) == {"lanterns 2026", "henry danger"}
    henry = shows["henry danger"]
    assert sorted((e.season, e.episode) for e in henry.episodes) == [
        (1, 1), (1, 2), (2, 1)]  # episodes grouped across season folders
    for ep in henry.episodes:
        assert os.path.isabs(ep.url)


def test_local_folder_categories_collapse_release_dirs(tmp_path):
    """Movie-own-folders and season dirs must not become one category each —
    the tree category is the library dir above them."""
    _plex_like_tree(tmp_path)
    pl = _scan(tmp_path)
    cats = {(c.section, c.name): c.count for c in pl.categories}
    assert cats[(SECTION_MOVIES, "MOVIES")] == 3     # incl. renamed Toy Story dir
    assert cats[(SECTION_MOVIES, os.path.join("MOVIES", "Comedy"))] == 1
    assert cats[(SECTION_SERIES, "MOVIES")] == 1     # Lanterns sits under MOVIES
    assert cats[(SECTION_SERIES, "TV SERIES")] == 1  # Henry Danger grouped
    assert all("[NO RAR]" not in c.name and "Toy Story" not in c.name
               and "Lanterns" not in c.name for c in pl.categories)


def test_local_folder_skips_sidecars_and_samples(tmp_path):
    _plex_like_tree(tmp_path)
    pl = _scan(tmp_path)
    names = [m.name for m in pl.movies]
    assert not any("sample" in n.lower() for n in names)
    # Only video files were picked up at all (srt/nfo/jpg never appear).
    assert pl.total == 6 + 0  # 4 movies + 2 series (not their sidecars)


def test_local_folder_nameless_episode_borrows_show_dir(tmp_path):
    """"S02E03.mkv" alone has no title — take it from the show folder so the
    episodes still collapse into one Series instead of one per file."""
    _mkfile(tmp_path, "TV SERIES/Breaking Bad/Season 02/S02E03.mkv")
    _mkfile(tmp_path, "TV SERIES/Breaking Bad/Season 02/S02E04.mkv")
    pl = _scan(tmp_path)
    assert len(pl.series) == 1
    show = pl.series[0]
    assert show.name == "Breaking Bad"
    assert show.group == os.path.join("TV SERIES", "Breaking Bad")
    assert sorted((e.season, e.episode) for e in show.episodes) == [(2, 3), (2, 4)]


def test_local_folder_ids_stable_across_rescans(tmp_path):
    _plex_like_tree(tmp_path)
    first = {i.id for i in _scan(tmp_path).movies}
    first |= {i.id for i in _scan(tmp_path).series}
    second = {i.id for i in _scan(tmp_path).movies}
    second |= {i.id for i in _scan(tmp_path).series}
    assert first == second and len(first) == 6


def test_local_folder_missing_dir_is_empty_not_an_error(tmp_path):
    pl = _scan(tmp_path / "nope")
    assert pl.total == 0


def test_local_folder_manager_refresh_and_cache(tmp_path):
    """End to end: a local_folder source refreshes like any other source and
    its scan survives the SQLite cache round-trip."""
    _plex_like_tree(tmp_path / "lib")
    src = PlaylistSource(id="loc", name="Local", kind="local_folder",
                         url=str(tmp_path / "lib"))
    mgr = IPTVManager(sources=[src], tmdb_api_key="", data_dir=str(tmp_path / "data"))
    pl = mgr._refresh_source(src, None, None, False)
    assert pl.total == 6
    assert mgr.playlist_for("loc") is pl
    assert {c.name for c in pl.categories} == {
        "MOVIES", "TV SERIES", os.path.join("MOVIES", "Comedy")}

    # Cache round-trip keeps movies, series with episodes, and the flags.
    cached = mgr.cache.load_playlist("loc")
    assert cached, "scan was not persisted"
    from iptv.manager import _playlist_from_cache
    pl2 = _playlist_from_cache("loc", cached)
    assert pl2.total == 6
    assert sum(len(s.episodes) for s in pl2.series) == 4

    # A rescan picks up newly added files (refresh = re-scan, it's cheap).
    _mkfile(tmp_path / "lib", "MOVIES/New.Movie.2024.1080p.mkv")
    pl3 = mgr._refresh_source(src, None, None, False)
    assert pl3.total == 7
    mgr.shutdown()


def test_local_folder_shows_in_the_sidebar_tree(tmp_path):
    """A local_folder source gets the same Source > Section > Category tree
    as any M3U provider."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from config import DeeptorrentConfig, IPTVSourceConfig
    from gui import iptv_tab as tab_mod

    _plex_like_tree(tmp_path / "lib")
    cfg = DeeptorrentConfig()
    cfg.iptv.cache_dir = str(tmp_path / "cache")
    cfg.iptv.sources = [IPTVSourceConfig(
        id="loc", name="Plex", kind="local_folder",
        url=str(tmp_path / "lib"))]
    with mock.patch.object(tab_mod.IPTVTab, "_refresh", lambda self: None):
        tab = tab_mod.IPTVTab(cfg)
    tab._manager._playlists["loc"] = _scan(tmp_path / "lib", sid="loc")
    tab._rebuild_sidebar()

    top = tab._tree.topLevelItem(0)
    assert top.text(0) == "Plex (6)"
    movies = _child_by_key(top, ("loc", SECTION_MOVIES, ""))
    series = _child_by_key(top, ("loc", SECTION_SERIES, ""))
    assert _child_by_key(movies, ("loc", SECTION_MOVIES, "MOVIES")).text(0) == "MOVIES (3)"
    assert _child_by_key(series, ("loc", SECTION_SERIES, "TV SERIES")).text(0) == "TV SERIES (1)"

    # Clicking a category shows its items.
    tab._show_section(SECTION_SERIES, "TV SERIES", "loc")
    assert [s.name for s in tab._current_items] == ["henry danger"]
    _close_tab(tab)


def test_browser_pane_toggle_buttons(tmp_path):
    """🗂 Tree / 🖼 Content toolbar toggles show/hide the two browser panes.
    Both start HIDDEN (player-first layout) and open at their seeded default
    widths — a pane hidden from birth never reported a width to remember."""
    tab = _iptv_tab_with_two_sources(tmp_path)
    try:
        # Default: closed, buttons unchecked.
        assert tab._sidebar.isHidden()
        assert tab._content.isHidden()
        assert not tab._sidebar_btn.isChecked()
        assert not tab._content_btn.isChecked()

        tab._sidebar_btn.setChecked(True)
        assert not tab._sidebar.isHidden()
        assert tab._splitter.sizes()[tab._splitter.indexOf(tab._sidebar)] > 0

        tab._content_btn.setChecked(True)
        assert not tab._content.isHidden()
        sizes = tab._splitter.sizes()
        assert sizes[tab._splitter.indexOf(tab._sidebar)] > 0
        assert sizes[tab._splitter.indexOf(tab._content)] > 0

        # Hide both again, then re-show: each pane comes back with a real
        # (non-zero) width. A shared saved layout used to restore the tree
        # to a 0-width sliver here, because sizes() reports the hidden pane
        # as 0.
        tab._sidebar_btn.setChecked(False)
        tab._content_btn.setChecked(False)
        assert tab._sidebar.isHidden()
        assert tab._content.isHidden()

        tab._sidebar_btn.setChecked(True)
        tab._content_btn.setChecked(True)
        assert not tab._sidebar.isHidden()
        assert not tab._content.isHidden()
        sizes = tab._splitter.sizes()
        assert sizes[tab._splitter.indexOf(tab._sidebar)] > 0
        assert sizes[tab._splitter.indexOf(tab._content)] > 0

        # Toggling again still restores widths (per-pane memory, reusable).
        tab._sidebar_btn.setChecked(False)
        tab._sidebar_btn.setChecked(True)
        assert tab._splitter.sizes()[tab._splitter.indexOf(tab._sidebar)] > 0
    finally:
        _close_tab(tab)


def _wait_for(cond, timeout=5.0):
    """Poll until cond() is true (download batches run on a worker thread)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_folder_scoped_search_filters_within_selected_folder(tmp_path):
    """The 📍 Folder toggle scopes the search box to the tree node entered
    last (category/year/section/source) instead of the whole playlist, and
    tree clicks re-filter the opened folder while a query is active."""
    tab = _iptv_tab_with_two_sources(tmp_path)
    try:
        # Provider A: one channel per category, both names match "chan".
        pla = tab._manager._playlists["a"]
        pla.channels.append(Channel(id=make_id("a", "sport"), name="Sport Chan B",
                                     url="http://a/2.ts", group="Sport",
                                     section=SECTION_LIVE))
        pla.categories.append(Category(name="Sport", section=SECTION_LIVE, count=1))
        tab._rebuild_sidebar()

        live = _child_by_key(tab._tree.topLevelItem(0), ("a", SECTION_LIVE, ""))
        news = _child_by_key(live, ("a", SECTION_LIVE, "News"))
        sport = _child_by_key(live, ("a", SECTION_LIVE, "Sport"))

        # Entering a folder records it as the search scope.
        tab._on_tree_click(news, 0)
        assert tab._scope_key == ("a", SECTION_LIVE, "News")
        assert tab._scope_label == "News"

        # Scope OFF (default): the query runs over the whole playlist.
        tab._on_search("chan")
        assert [c.name for c in tab._current_items] == ["a chan", "Sport Chan B"]

        # Scope ON: only matches inside the selected folder.
        tab._scope_btn.setChecked(True)
        tab._on_search("chan")
        assert [c.name for c in tab._current_items] == ["a chan"]
        assert "News" in tab._search.placeholderText()

        # A query with no hits in the folder finds nothing scoped…
        tab._on_search("zzz")
        assert tab._current_items == []
        # …and tree clicks re-filter the newly opened folder while scoped.
        tab._on_search("chan")
        tab._on_tree_click(sport, 0)
        assert [c.name for c in tab._current_items] == ["Sport Chan B"]

        # Scope OFF again: back to playlist-wide results.
        tab._scope_btn.setChecked(False)
        tab._on_search("chan")
        assert len(tab._current_items) == 2
    finally:
        _close_tab(tab)


def test_vod_download_queues_into_download_engine(tmp_path):
    """Play-tab downloads: a movie goes straight to the Download Manager as
    a file job; a series (confirmed) queues one job per episode into a
    per-series subfolder, with HLS episodes routed to add_stream_job."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    QApplication.instance() or QApplication([])
    from iptv.models import Episode

    tab = _iptv_tab_with_two_sources(tmp_path)
    try:
        calls = []

        class _FakeEngine:
            def add_job(self, url, filename="", save_path="", headers=None,
                        referrer="", **kw):
                calls.append(("file", url, filename, save_path))
                return mock.MagicMock(error_message="")

            def add_stream_job(self, url, filename="", save_path="",
                               headers=None, referrer="", **kw):
                calls.append(("hls", url, filename, save_path))
                return mock.MagicMock(error_message="")

        tab.set_download_engine(_FakeEngine())

        pla = tab._manager._playlists["a"]
        pla.movies.append(Movie(id=make_id("a", "m1"), name="Dune",
                                url="http://a/dune.mkv", group="VOD",
                                section=SECTION_MOVIES))
        pla.series.append(Series(
            id=make_id("a", "sr1"), name="Henry", group="TV",
            section=SECTION_SERIES,
            episodes=[
                Episode(season=1, episode=1, name="Pilot", url="http://a/s01e01.mp4"),
                Episode(season=1, episode=2, name="Solo", url="http://a/s01e02.m3u8"),
            ]))

        # Single movie: no confirmation, direct file job in the default folder.
        tab._on_download_requested(pla.movies[0])
        assert _wait_for(lambda: len(calls) >= 1)
        kind, url, fname, path = calls[0]
        assert (kind, url) == ("file", "http://a/dune.mkv")
        assert "Dune" in fname and path == ""

        # Whole series: one job per episode, per-series subfolder, m3u8
        # episodes become stream jobs.
        with mock.patch("gui.iptv_tab.QMessageBox.question",
                        return_value=QMessageBox.Yes):
            tab._on_download_requested(pla.series[0])
        assert _wait_for(lambda: len(calls) >= 3)
        assert calls[1][0] == "file" and "S01E01" in calls[1][2]
        assert calls[2][0] == "hls" and "S01E02" in calls[2][2]
        for _k, _u, _f, p in calls[1:]:
            assert p.endswith((os.sep, "/")) and "Henry" in p

        # Declining the confirmation queues nothing.
        calls.clear()
        with mock.patch("gui.iptv_tab.QMessageBox.question",
                        return_value=QMessageBox.No):
            tab._on_download_requested(pla.series[0])
        assert not _wait_for(lambda: bool(calls), timeout=0.5)

        # Season path (episode-list context menu) queues just that season.
        with mock.patch("gui.iptv_tab.QMessageBox.question",
                        return_value=QMessageBox.Yes):
            tab._download_season(pla.series[0], 1)
        assert _wait_for(lambda: len(calls) == 2)
    finally:
        _close_tab(tab)


def test_error_and_watchdog_share_one_retry_for_an_attempt():
    """The backend callback and watchdog cannot both consume attempt 1."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from unittest.mock import MagicMock, patch
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget
    from iptv.manager import IPTVManager

    QApplication.instance() or QApplication([])
    player = PlayerWidget(IPTVManager(sources=[], tmdb_api_key=""),
                          DeeptorrentConfig())
    player._current_item = MagicMock(url="http://example.com/stream.m3u8")
    player._backend = MagicMock()
    player._backend.buffer_status.return_value = {
        "state": "stopped", "percent": 0, "demuxer_cache_duration": 0,
        "paused_for_cache": False, "time_pos": 0, "core_idle": True,
    }
    player._playback_generation = 4
    player._attempt_id = 1
    callbacks = []

    with patch("gui.iptv_tab.QTimer.singleShot",
               side_effect=lambda _ms, cb: callbacks.append(cb)):
        player._on_error("connection refused", 4, 1)
        assert player._retry_count == 1
        player._loading_started = time.monotonic() - player._RETRY_INTERVAL - 1
        player._update_loading()

    assert player._retry_count == 1
    assert len(callbacks) == 1
    assert player._backend.play.call_count == 0
    callbacks[0]()
    assert player._backend.play.call_count == 1
    assert player._attempt_id == 2


def test_stale_scheduled_retry_is_ignored_after_generation_change():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from unittest.mock import MagicMock, patch
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget
    from iptv.manager import IPTVManager

    QApplication.instance() or QApplication([])
    player = PlayerWidget(IPTVManager(sources=[], tmdb_api_key=""),
                          DeeptorrentConfig())
    player._current_item = MagicMock(url="http://example.com/old.m3u8")
    player._backend = MagicMock()
    player._playback_generation = 2
    player._attempt_id = 3
    callbacks = []
    with patch("gui.iptv_tab.QTimer.singleShot",
               side_effect=lambda _ms, cb: callbacks.append(cb)):
        player._on_error("connection reset", 2, 3)
    player._playback_generation = 3
    player._attempt_id = 1
    callbacks[0]()
    assert player._backend.play.call_count == 0


def test_error_retry_skipped_when_no_current_item():
    """Errors with no current item (e.g. after stop) go straight to the
    overlay — no retry is scheduled."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from unittest.mock import MagicMock, patch
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget
    from iptv.manager import IPTVManager

    app = QApplication.instance() or QApplication([])
    player = PlayerWidget(IPTVManager(sources=[], tmdb_api_key=""),
                          DeeptorrentConfig())
    player._current_item = None
    player._backend = MagicMock()

    show_error_calls = []
    player._show_error = lambda msg: show_error_calls.append(msg)

    with patch("gui.iptv_tab.QTimer.singleShot"):
        player._on_error("connection refused")
        assert len(show_error_calls) == 1
        assert player._retry_count == 0
        assert player._error_retry_pending is False


def test_stop_cancels_pending_error_retry():
    """stop() clears _error_retry_pending so a scheduled retry can't fire
    after the user explicitly stopped playback."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from unittest.mock import MagicMock, patch
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget
    from iptv.manager import IPTVManager

    app = QApplication.instance() or QApplication([])
    player = PlayerWidget(IPTVManager(sources=[], tmdb_api_key=""),
                          DeeptorrentConfig())
    player._current_item = MagicMock(url="http://example.com/stream.m3u8")
    player._backend = MagicMock()

    with patch("gui.iptv_tab.QTimer.singleShot"):
        player._on_error("connection refused")
        assert player._error_retry_pending is True

    player.stop()
    assert player._error_retry_pending is False


@pytest.mark.parametrize(
    ("kind", "needle"),
    [
        (xtream.XtreamErrorKind.AUTH_REJECTED, "authentication was rejected"),
        (xtream.XtreamErrorKind.UNREACHABLE, "provider is unreachable"),
        (xtream.XtreamErrorKind.INVALID_RESPONSE, "invalid response"),
        (None, "genuinely empty"),
    ],
)
def test_xtream_source_error_presentations_are_distinct(kind, needle):
    from gui.iptv_tab import _source_empty_message

    source = PlaylistSource(id="x", name="Provider", kind="xtream", url="http://x")
    error = xtream.XtreamLoadError(kind, "safe") if kind is not None else None
    playlist = xtream.XtreamPlaylist(source_id="x", error=error)
    assert needle in _source_empty_message(source, playlist).lower()


def test_non_xtream_empty_source_keeps_generic_presentation():
    from gui.iptv_tab import _source_empty_message

    source = PlaylistSource(id="m", name="M3U", kind="m3u_url", url="http://x/list")
    message = _source_empty_message(source, Playlist(source_id="m"))
    assert "check the source url/credentials" in message.lower()
    assert "xtream" not in message.lower()


def test_manager_callback_preserves_typed_xtream_error(tmp_path, monkeypatch):
    source = PlaylistSource(
        id="x", name="Provider", kind="xtream", url="http://provider",
        username="u", password="p")
    expected = xtream.XtreamPlaylist(
        source_id="x",
        error=xtream.XtreamLoadError(
            xtream.XtreamErrorKind.UNREACHABLE, "Provider unreachable"),
    )
    monkeypatch.setattr(xtream, "load_playlist", lambda *a, **k: expected)
    manager = IPTVManager([source], data_dir=str(tmp_path / "cache"))
    callbacks = []
    thread = manager.load_source_async(
        source, use_cache=False, on_done=lambda ok, pl: callbacks.append((ok, pl)))
    thread.join(timeout=5)
    assert callbacks
    assert callbacks[-1][0] is False
    assert callbacks[-1][1].error.kind is xtream.XtreamErrorKind.UNREACHABLE
    manager.shutdown()


def test_opensubtitles_login_warning_continues_key_only_and_hides_secrets(
        tmp_path, monkeypatch, caplog):
    from iptv.opensubtitles import OpenSubtitlesClient, OpenSubtitlesLoginWarning

    class Response:
        def __init__(self, status, payload=None, content=b""):
            self.status_code = status
            self._payload = payload or {}
            self.content = content

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(response=self)

    posts = []

    def fake_post(url, **kwargs):
        posts.append(url)
        if url.endswith("/login"):
            return Response(401)
        return Response(200, {"link": "https://cdn.example/sub.srt"})

    monkeypatch.setattr("iptv.opensubtitles.requests.post", fake_post)
    monkeypatch.setattr(
        "iptv.opensubtitles.requests.get",
        lambda *a, **k: Response(200, content=b"subtitle"),
    )
    caplog.set_level("WARNING", logger="iptv.opensubtitles")
    client = OpenSubtitlesClient("api-key", "private-user", "private-password")
    dest = tmp_path / "subtitle.srt"
    assert client.download(12, str(dest)) == str(dest)
    assert isinstance(client.login_warning, OpenSubtitlesLoginWarning)
    assert "key-only mode" in client.login_warning.message
    assert len(posts) == 2  # rejected login did not prevent /download
    assert "private-user" not in caplog.text
    assert "private-password" not in caplog.text


def test_subtitle_dialog_displays_nonfatal_login_warning(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.iptv_tab import _SubtitleSearchDialog

    QApplication.instance() or QApplication([])
    config = DeeptorrentConfig()
    config.iptv.opensubtitles_api_key = "key"
    loaded = []
    dialog = _SubtitleSearchDialog(
        config, "", "Movie", on_loaded=loaded.append)
    path = str(tmp_path / "movie.en.srt")
    dialog._on_downloaded(path, "", "Account rejected; continuing key-only.")
    assert loaded == [path]
    assert "warning" in dialog.status.text().lower()
    assert "account rejected" in dialog.status.text().lower()
    dialog.close()


@pytest.mark.parametrize(
    ("diagnostic", "classification"),
    [
        ("Error opening input: End of file", "permanent"),
        ("TLS handshake failed: connection reset by peer", "transient"),
        ("Connection timed out", "transient"),
        ("Input/output error", "permanent"),
    ],
)
def test_framegrab_error_classification(diagnostic, classification):
    from iptv.framegrab import _classify_ffmpeg_error
    assert _classify_ffmpeg_error(diagnostic) == classification


def test_framegrab_eof_is_negative_cached(tmp_path):
    from iptv.artwork import ArtworkCache
    from iptv.framegrab import FrameGrabber

    grabber = FrameGrabber(ArtworkCache(str(tmp_path / "a")), ffmpeg_path="ffmpeg")
    eof = mock.Mock(returncode=1, stdout=b"", stderr=b"End of file")
    with mock.patch("iptv.framegrab.subprocess.run", return_value=eof) as run:
        assert grabber.grab("http://host/dead.ts") == ""
        first_calls = run.call_count
        assert grabber.grab("http://host/dead.ts") == ""
    assert first_calls == 2  # both seek fallbacks, one pass
    assert run.call_count == first_calls


@pytest.mark.parametrize(
    ("url", "valid"),
    [
        ("", True),
        ("https://provider.example/epg.xml?token=a", True),
        ("http://127.0.0.1:8080/guide.xml", True),
        ("provider.example/epg.xml", False),
        ("ftp://provider.example/epg.xml", False),
        ("https:///epg.xml", False),
        ("https://provider.example:bad/epg.xml", False),
        ("https://provider.example/my guide.xml", False),
    ],
)
def test_epg_url_validation(url, valid):
    from gui.iptv_settings_dialog import _epg_url_error
    assert (not _epg_url_error(url)) is valid


# ---------------------------------------------------------------------------
# Play capability: watch progress, recording, sleep, live buffer, compact host
# ---------------------------------------------------------------------------

def test_watch_progress_schema_migrates_partial_legacy_table(tmp_path):
    """An early two-column development table upgrades additively in place."""
    import sqlite3

    db = tmp_path / "iptv_cache.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE watch_progress (source_id TEXT, item_id TEXT, "
        "PRIMARY KEY(source_id, item_id))")
    conn.execute("INSERT INTO watch_progress(source_id, item_id) VALUES('s', 'i')")
    conn.commit()
    conn.close()

    cache = IPTVCache(str(tmp_path))
    cols = {r[1] for r in cache._conn().execute(
        "PRAGMA table_info(watch_progress)").fetchall()}
    assert {"source_id", "item_id", "position_seconds", "duration_seconds",
            "watched", "updated_at"} <= cols
    migrated = cache.watch_progress("s", "i")
    assert migrated == pytest.approx({
        "position": 0.0, "duration": 0.0, "watched": False, "updated_at": 0.0})
    cache.close()


def test_watch_progress_roundtrip_and_explicit_watched_state(tmp_path):
    cache = IPTVCache(str(tmp_path))
    cache.save_watch_progress("s", "movie", 125.5, 1000.0)
    assert cache.watch_progress("s", "movie")["position"] == 125.5
    cache.set_watched("s", "movie", True)
    cache.save_watch_progress("s", "movie", 30.0, 1000.0, watched=None)
    assert cache.watch_progress("s", "movie")["watched"] is True
    cache.set_watched("s", "movie", False)
    assert cache.watch_progress("s", "movie")["watched"] is False


@pytest.mark.parametrize(
    ("position", "duration", "watched", "resume", "complete"),
    [
        (10, 1000, False, False, False),
        (100, 1000, False, True, False),
        (900, 1000, False, False, True),
        (490, 600, False, False, True),  # <=2 min left and >half consumed
        (100, 1000, True, False, False),
        (100, 0, False, False, False),
    ],
)
def test_manager_resume_and_watched_rules(position, duration, watched, resume, complete):
    assert IPTVManager.is_meaningful_resume(position, duration, watched) is resume
    assert IPTVManager.is_near_completion(position, duration) is complete


def test_manager_watch_progress_is_per_source_and_never_live(tmp_path):
    mgr, pl = _manager_with_playlist(tmp_path)
    movie = Movie(id="s1::movie", name="Film", url="http://host/film.mkv")
    mgr.update_watch_progress(movie, 120, 1000)
    assert mgr.resume_position(movie) == 120
    mgr.update_watch_progress(pl.channels[0], 500, 600)
    assert mgr.watch_progress(pl.channels[0]) is None
    assert mgr.cache.watch_progress("s1", "c1") is None
    mgr.set_watched(movie, True)
    assert mgr.resume_position(movie) == 0
    mgr.set_watched(movie, False)
    assert mgr.resume_position(movie) == 120
    mgr.shutdown()


def test_player_checkpoints_and_applies_resume(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget

    QApplication.instance() or QApplication([])
    mgr, _pl = _manager_with_playlist(tmp_path)
    movie = Movie(id="s1::movie", name="Film", url="http://host/film.mkv")
    mgr.update_watch_progress(movie, 125, 1000)
    player = PlayerWidget(mgr, DeeptorrentConfig())
    player._current_item = movie
    player._backend = mock.MagicMock()
    player._pending_resume = mgr.resume_position(movie)
    player._on_position(0, 1000)
    player._backend.seek.assert_called_once_with(125)
    player._last_position = 300
    player._last_duration = 1000
    player._checkpoint_current()
    assert mgr.watch_progress(movie)["position"] == 300
    player.shutdown()
    mgr.shutdown()


def test_stream_recorder_command_redaction_and_lifecycle(tmp_path, caplog):
    from dlmgr.ffmpeg import StreamRecorder, redact_recording_error

    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"")
    calls = []
    finished = threading.Event()

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.done = threading.Event()

        def poll(self):
            return self.returncode

        def communicate(self):
            self.done.wait(2)
            return None, b""

        def terminate(self):
            self.returncode = -15
            self.done.set()

        def wait(self, timeout=None):
            assert self.done.wait(timeout)
            return self.returncode

        def kill(self):
            self.returncode = -9
            self.done.set()

    proc = FakeProcess()

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return proc

    statuses = []
    recorder = StreamRecorder(
        str(ffmpeg), str(tmp_path / "recordings"),
        on_status=lambda state, text: (statuses.append((state, text)),
                                       finished.set() if state == "stopped" else None),
        popen_factory=fake_popen)
    secret_url = "https://user:pass@provider.test/live.m3u8?token=private"
    ok, output = recorder.start(secret_url, 'Bad:/Name*?', {"Cookie": "secret=1"})
    assert ok and output.endswith(".mkv")
    cmd, kwargs = calls[0]
    assert cmd[0] == str(ffmpeg) and cmd[-1] == output
    assert "-c" in cmd and "copy" in cmd and "-nostdin" in cmd
    assert kwargs["creationflags"] is not None
    assert recorder.is_recording
    assert recorder.stop()
    assert finished.wait(2)
    assert statuses[0][0] == "recording" and statuses[-1][0] == "stopped"
    assert "user:pass" not in caplog.text and "token=private" not in caplog.text
    redacted = redact_recording_error(f"Failed to open {secret_url}\nCookie: secret=1")
    assert secret_url not in redacted and "secret=1" not in redacted


def test_stream_recorder_refuses_missing_ffmpeg_and_local_input(tmp_path):
    from dlmgr.ffmpeg import StreamRecorder

    missing = StreamRecorder("", str(tmp_path))
    assert missing.start("https://provider.test/live", "Live")[0] is False
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"")
    local = StreamRecorder(str(ffmpeg), str(tmp_path))
    assert local.start(str(tmp_path / "movie.mkv"), "Movie")[0] is False
    assert local.start("file:///tmp/movie.mkv", "Movie")[0] is False


def test_sleep_timer_stops_playback_and_stays_visible(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget

    QApplication.instance() or QApplication([])
    mgr, pl = _manager_with_playlist(tmp_path)
    player = PlayerWidget(mgr, DeeptorrentConfig())
    player._backend = mock.MagicMock()
    player._current_item = pl.channels[0]
    player._playback_active = True
    player._set_sleep_minutes(15)
    assert player._sleep_timer.isActive()
    player._on_sleep_timeout()
    player._backend.stop.assert_called_once()
    assert not player._playback_active
    assert player.sleep_btn.text() == "Sleep: Stopped"
    player.shutdown()
    mgr.shutdown()


def test_mpv_applies_bounded_live_pause_buffer_options():
    from iptv.player import MpvBackend

    values = {}

    class FakeMpv:
        def __setitem__(self, key, value):
            values[key] = value

    backend = MpvBackend(None)
    backend._mpv = FakeMpv()
    backend.set_live_pause_buffer(300)
    assert values["cache"] == "yes"
    assert values["cache-pause"] == "yes"
    assert values["cache-secs"] == 300
    assert values["demuxer-max-back-bytes"] == 300 * 1024 * 1024
    assert values["demuxer-max-bytes"] == 300 * 1024 * 1024


def test_player_applies_pause_buffer_only_to_live_playback(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget

    QApplication.instance() or QApplication([])
    cfg = DeeptorrentConfig()
    cfg.iptv.live_pause_buffer_seconds = 180
    mgr, pl = _manager_with_playlist(tmp_path)
    player = PlayerWidget(mgr, cfg)
    backend = mock.MagicMock()
    player._media_backend = backend
    player._backend = backend
    player._ensure_backend = lambda: True
    player.play(pl.channels[0])
    backend.set_live_pause_buffer.assert_called_with(180)

    movie = Movie(id="s1::movie", name="Film", url="http://host/film.mkv")
    player.play(movie)
    backend.set_live_pause_buffer.assert_called_with(0)
    player.shutdown()
    mgr.shutdown()


def test_live_pause_buffer_config_defaults_and_loads(tmp_path):
    import json
    from config import DeeptorrentConfig

    assert DeeptorrentConfig().iptv.live_pause_buffer_seconds == 300
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"iptv": {"live_pause_buffer_seconds": 90,
                                          "recording_dir": "R"}}), encoding="utf-8")
    cfg = DeeptorrentConfig.from_file(str(path))
    assert cfg.iptv.live_pause_buffer_seconds == 90
    assert cfg.iptv.recording_dir == "R"


def test_compact_topmost_uses_pointer_sized_win32_signature():
    import ctypes
    from ctypes import wintypes
    from gui.iptv_tab import PlayerWidget

    calls = []

    class FakeSetWindowPos:
        argtypes = None
        restype = None

        def __call__(self, *args):
            calls.append(args)
            return 1

    set_window_pos = FakeSetWindowPos()
    user32 = mock.Mock(SetWindowPos=set_window_pos)
    widget = mock.Mock()
    widget.winId.return_value = 0x12345678
    with mock.patch.object(PlayerWidget, "compact_mode_supported", return_value=True), \
            mock.patch("ctypes.WinDLL", create=True, return_value=user32) as win_dll:
        assert PlayerWidget._set_native_topmost(widget, True)
    win_dll.assert_called_once_with("user32", use_last_error=True)
    assert set_window_pos.argtypes == [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    assert set_window_pos.restype is wintypes.BOOL
    assert calls[0][0].value == 0x12345678
    assert calls[0][1].value == ctypes.c_void_p(-1).value


def test_compact_host_never_reparents_player_surface(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget

    QApplication.instance() or QApplication([])
    mgr, _pl = _manager_with_playlist(tmp_path)
    player = PlayerWidget(mgr, DeeptorrentConfig())
    original_parent = player.surface.parent()
    states = []
    player.sig_compact.connect(states.append)
    with mock.patch.object(PlayerWidget, "compact_mode_supported", return_value=True), \
            mock.patch.object(PlayerWidget, "_set_native_topmost", return_value=True):
        player._toggle_compact()
        assert player._compact and states == [True]
        assert player.surface.parent() is original_parent
        assert player.rw_btn.isHidden() and player.preset_btn.isHidden()
        player._toggle_compact()
        assert not player._compact and states == [True, False]
        assert player.surface.parent() is original_parent
        assert not player.rw_btn.isHidden() and player.preset_btn.isHidden()
    player.shutdown()
    mgr.shutdown()

