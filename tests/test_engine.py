"""Unit tests for the TorrentEngine."""

import os
import tempfile
import time

import libtorrent as lt
import pytest

from engine import TorrentEngine


@pytest.fixture
def engine():
    with TorrentEngine() as eng:
        yield eng


def test_session_created(engine):
    assert engine.list_torrents() == []


def test_add_magnet_and_list(engine):
    # Big Buck Bunny magnet link (public domain)
    magnet = (
        "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0"
        "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example.com%3A80"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        info_hash = engine.add_magnet(magnet, tmpdir, "Movies")
        assert isinstance(info_hash, str)
        assert len(info_hash) == 40

        torrents = engine.list_torrents()
        assert len(torrents) == 1
        assert torrents[0]["info_hash"] == info_hash
        assert torrents[0]["category"] == "Movies"
        assert torrents[0]["save_path"] == tmpdir


def test_pause_resume(engine):
    magnet = (
        "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0"
        "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example.com%3A80"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        info_hash = engine.add_magnet(magnet, tmpdir, "Movies")
        assert engine.pause(info_hash) is True
        time.sleep(0.2)
        status = engine.get_torrent_status(info_hash)
        assert status["paused"] is True

        assert engine.resume(info_hash) is True
        time.sleep(0.2)
        status = engine.get_torrent_status(info_hash)
        assert status["paused"] is False


def test_remove(engine):
    magnet = (
        "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0"
        "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example.com%3A80"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        info_hash = engine.add_magnet(magnet, tmpdir, "Movies")
        assert engine.remove(info_hash) is True
        assert engine.list_torrents() == []


def test_set_file_priority_needs_metadata(engine):
    magnet = (
        "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0"
        "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example.com%3A80"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        info_hash = engine.add_magnet(magnet, tmpdir, "Movies")
        # Metadata not available yet, so this should fail
        with pytest.raises(Exception):
            engine.set_file_priority(info_hash, 0, 0)


def test_add_tracker_and_swarm_stats(engine):
    magnet = (
        "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0"
        "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example.com%3A80"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        info_hash = engine.add_magnet(magnet, tmpdir, "Movies")
        assert engine.add_tracker(info_hash, "http://tracker.openbittorrent.com:80/announce") is True
        time.sleep(0.2)
        stats = engine.get_swarm_stats(info_hash)
        assert stats["info_hash"] == info_hash
        assert "trackers" in stats
        assert "peers" in stats
        assert "dht_nodes" in stats
        assert "piece_availability_histogram" in stats


def test_duplicate_magnet_rejected(engine):
    magnet = (
        "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0"
        "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example.com%3A80"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        engine.add_magnet(magnet, tmpdir, "Movies")
        with pytest.raises(Exception, match="already in the list"):
            engine.add_magnet(magnet, tmpdir, "TV")
        # The existing record is untouched (category not overwritten).
        assert engine.list_torrents()[0]["category"] == "Movies"


def test_add_magnet_paused(engine):
    magnet = (
        "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0"
        "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example.com%3A80"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        info_hash = engine.add_magnet(magnet, tmpdir, "Movies", paused=True)
        time.sleep(0.2)
        assert engine.get_torrent_status(info_hash)["paused"] is True


def test_status_includes_eta(engine):
    magnet = (
        "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0"
        "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example.com%3A80"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        info_hash = engine.add_magnet(magnet, tmpdir, "Movies")
        status = engine.get_torrent_status(info_hash)
        assert "eta" in status  # None until downloading


def test_stream_helpers_need_metadata(engine):
    magnet = (
        "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0"
        "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example.com%3A80"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        info_hash = engine.add_magnet(magnet, tmpdir, "Movies")
        # Metadata not available yet: prefix is empty, window is a no-op.
        assert engine.get_file_prefix(info_hash, 0) == (0, 0)
        assert engine.set_stream_window(info_hash, 0, 1 << 20) is False


def test_resume_data_roundtrip():
    magnet = (
        "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0"
        "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.example.com%3A80"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        with TorrentEngine() as eng:
            info_hash = eng.add_magnet(magnet, tmpdir, "Movies")
            blobs = eng.export_resume_data(timeout=5)
        assert info_hash in blobs
        with TorrentEngine() as eng2:
            restored = eng2.add_magnet(magnet, tmpdir, "Movies", resume=blobs[info_hash])
            assert restored == info_hash
            # Corrupt resume data falls back to a normal add.
            other = magnet.replace("dd8255", "dd8256")[:-1] + "a"
            assert eng2.add_magnet(other, tmpdir, "TV", resume=b"junk")


def test_state_manager_resume_files(tmp_path):
    from engine.state import TorrentStateManager
    sm = TorrentStateManager(str(tmp_path))
    sm.save_resume_data({"abc123": b"\x01\x02", "def456": b"\x03"})
    assert sm.load_resume_data("abc123") == b"\x01\x02"
    assert sm.load_resume_data("missing") is None
    # Stale entries are pruned on next save.
    sm.save_resume_data({"abc123": b"\x09"})
    assert sm.load_resume_data("def456") is None
    sm.remove_resume_data("abc123")
    assert sm.load_resume_data("abc123") is None
