"""Focused tests for advanced HLS and DASH manifest behavior."""
from __future__ import annotations

from collections import Counter
from unittest import mock

import pytest
from Crypto.Cipher import AES

from dlmgr.hls_dash import (
    HLSSegmentMetadata,
    HLSDownloader,
    StreamInfo,
    parse_manifest,
)


class _Response:
    def __init__(self, data: bytes | str, error: Exception | None = None):
        self.content = data.encode() if isinstance(data, str) else data
        self.text = data if isinstance(data, str) else data.decode(errors="replace")
        self._error = error

    def raise_for_status(self) -> None:
        if self._error:
            raise self._error


def _pkcs7(data: bytes) -> bytes:
    padding = AES.block_size - len(data) % AES.block_size
    return data + bytes([padding]) * padding


def test_hls_rendition_hook_and_per_segment_metadata():
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=320x180
low/media.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=1920x1080
high/media.m3u8
"""
    media = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:41
#EXT-X-TARGETDURATION:4
#EXT-X-KEY:METHOD=AES-128,URI="keys/one.key"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4,
one.m4s
#EXT-X-DISCONTINUITY
#EXT-X-KEY:METHOD=AES-128,URI="keys/two.key",IV=0x000000000000000000000000000000AA
#EXTINF:4,
two.m4s
#EXT-X-KEY:METHOD=NONE
#EXTINF:2,
three.m4s
#EXT-X-ENDLIST
"""

    def fake_get(url, **_kwargs):
        return _Response(media if url.endswith("low/media.m3u8") else master)

    with mock.patch("dlmgr.hls_dash.http_client.get", side_effect=fake_get):
        info = parse_manifest(
            "https://cdn.example/path/master.m3u8",
            rendition_selector=lambda renditions: min(renditions, key=lambda item: item.bandwidth),
        )

    assert info.error == ""
    assert info.selected_rendition is info.renditions[0]
    assert info.manifest_url == "https://cdn.example/path/low/media.m3u8"
    assert info.media_sequence == 41
    assert [item.media_sequence for item in info.segment_metadata] == [41, 42, 43]
    assert info.segment_key_urls == [
        "https://cdn.example/path/low/keys/one.key",
        "https://cdn.example/path/low/keys/two.key",
        "",
    ]
    assert info.segment_ivs == ["", "0x000000000000000000000000000000AA", ""]
    assert [item.encryption_method for item in info.segment_metadata] == [
        "AES-128", "AES-128", "NONE"
    ]
    assert info.discontinuity_indices == [1]
    assert info.init_segment_url == "https://cdn.example/path/low/init.mp4"
    assert info.segment_init_urls == [info.init_segment_url] * 3


def test_hls_downloader_rotates_keys_and_uses_sequence_or_explicit_iv(tmp_path):
    key_one = b"first-secret-key"
    key_two = b"other-secret-key"
    plain = [b"first payload", b"second payload", b"clear payload"]
    iv_one = (77).to_bytes(16, "big")
    iv_two = bytes.fromhex("000000000000000000000000000000aa")
    encrypted = [
        AES.new(key_one, AES.MODE_CBC, iv_one).encrypt(_pkcs7(plain[0])),
        AES.new(key_two, AES.MODE_CBC, iv_two).encrypt(_pkcs7(plain[1])),
        plain[2],
    ]
    urls = [f"https://cdn.example/{index}.ts" for index in range(3)]
    info = StreamInfo(
        segment_urls=urls,
        is_encrypted=True,
        encryption_method="AES-128",
        media_sequence=77,
        segment_metadata=[
            HLSSegmentMetadata(
                url=urls[0], media_sequence=77, encryption_method="AES-128",
                encryption_key_url="https://cdn.example/key-one",
            ),
            HLSSegmentMetadata(
                url=urls[1], media_sequence=78, encryption_method="AES-128",
                encryption_key_url="https://cdn.example/key-two",
                encryption_iv="0x000000000000000000000000000000aa",
            ),
            HLSSegmentMetadata(url=urls[2], media_sequence=79, encryption_method="NONE"),
        ],
    )
    payloads = dict(zip(urls, encrypted))
    payloads.update({
        "https://cdn.example/key-one": key_one,
        "https://cdn.example/key-two": key_two,
    })
    calls = Counter()

    def fake_get(url, **_kwargs):
        calls[url] += 1
        return _Response(payloads[url])

    progress = []
    with mock.patch("dlmgr.hls_dash.http_client.get", side_effect=fake_get):
        downloader = HLSDownloader(info, str(tmp_path), max_workers=3, on_progress=progress.append)
        paths = downloader.download()

    assert [open(path, "rb").read() for path in paths] == plain
    assert calls["https://cdn.example/key-one"] == 1
    assert calls["https://cdn.example/key-two"] == 1
    assert downloader.bytes_downloaded == sum(map(len, encrypted))
    assert downloader.progress == 1.0
    assert progress == [1, 2, 3]


def test_hls_progress_counts_only_successful_segments(tmp_path):
    info = StreamInfo(segment_urls=["https://cdn.example/good.ts", "https://cdn.example/bad.ts"])

    def fake_get(url, **_kwargs):
        if url.endswith("bad.ts"):
            return _Response(b"", RuntimeError("broken segment"))
        return _Response(b"good")

    progress = []
    with mock.patch("dlmgr.hls_dash.http_client.get", side_effect=fake_get), \
            mock.patch("dlmgr.hls_dash.MAX_RETRIES", 1):
        downloader = HLSDownloader(info, str(tmp_path), max_workers=2, on_progress=progress.append)
        with pytest.raises(RuntimeError, match="1/2 segments failed"):
            downloader.download()

    assert downloader.progress == 0.5
    assert progress == [1]
    assert downloader.bytes_downloaded == 4


def test_live_hls_and_dynamic_dash_are_rejected_clearly():
    live_hls = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXTINF:4,
current.ts
"""
    dynamic_dash = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic">
  <Period><AdaptationSet contentType="video"><Representation id="v" bandwidth="1"/></AdaptationSet></Period>
</MPD>"""

    with mock.patch("dlmgr.hls_dash.http_client.get", return_value=_Response(live_hls)):
        hls_info = parse_manifest("https://cdn.example/live.m3u8")
    with mock.patch("dlmgr.hls_dash.http_client.get", return_value=_Response(dynamic_dash)):
        dash_info = parse_manifest("https://cdn.example/live.mpd")

    assert hls_info.is_live and "not supported" in hls_info.error.lower()
    assert "endlist" in hls_info.error.lower()
    assert dash_info.is_live and "not supported" in dash_info.error.lower()
    assert "static mpd" in dash_info.error.lower()


def test_dash_baseurl_timeline_time_and_separate_best_audio():
    manifest = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
  <BaseURL>root/</BaseURL>
  <Period>
    <BaseURL>period/</BaseURL>
    <AdaptationSet contentType="video">
      <BaseURL>video/</BaseURL>
      <SegmentTemplate timescale="1" startNumber="7" media="chunk-$Time$.m4s" initialization="init-$RepresentationID$.mp4">
        <SegmentTimeline>
          <S t="0" d="2" r="-1"/>
          <S t="6" d="2" r="1"/>
        </SegmentTimeline>
      </SegmentTemplate>
      <Representation id="low" bandwidth="100" width="320" height="180"><BaseURL>low/</BaseURL></Representation>
      <Representation id="high" bandwidth="1000" width="1280" height="720"><BaseURL>high/</BaseURL></Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio" codecs="mp4a.40.2">
      <BaseURL>audio/</BaseURL>
      <SegmentTemplate timescale="1" duration="4" startNumber="3" media="a-$Number%03d$.m4s" initialization="a-$RepresentationID$.mp4"/>
      <Representation id="mono" bandwidth="64"><BaseURL>mono/</BaseURL></Representation>
      <Representation id="stereo" bandwidth="128"><BaseURL>stereo/</BaseURL></Representation>
    </AdaptationSet>
  </Period>
</MPD>"""

    with mock.patch("dlmgr.hls_dash.http_client.get", return_value=_Response(manifest)):
        info = parse_manifest("https://cdn.example/base/manifest.mpd")

    assert info.error == ""
    assert info.selected_rendition.bandwidth == 1000
    assert info.init_segment_url == (
        "https://cdn.example/base/root/period/video/high/init-high.mp4"
    )
    assert info.segment_urls == [
        f"https://cdn.example/base/root/period/video/high/chunk-{value}.m4s"
        for value in (0, 2, 4, 6, 8)
    ]
    assert info.audio_rendition.bandwidth == 128
    assert info.audio_init_segment_url == (
        "https://cdn.example/base/root/period/audio/stereo/a-stereo.mp4"
    )
    assert info.audio_segment_urls == [
        f"https://cdn.example/base/root/period/audio/stereo/a-{number:03d}.m4s"
        for number in (3, 4, 5)
    ]


def test_dash_segment_list_initialization_metadata():
    manifest = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">
  <Period><AdaptationSet mimeType="video/mp4"><Representation id="v" bandwidth="1">
    <BaseURL>files/</BaseURL>
    <SegmentList><Initialization sourceURL="start.mp4"/><SegmentURL media="one.m4s"/><SegmentURL media="two.m4s"/></SegmentList>
  </Representation></AdaptationSet></Period>
</MPD>"""
    with mock.patch("dlmgr.hls_dash.http_client.get", return_value=_Response(manifest)):
        info = parse_manifest("https://cdn.example/path/video.mpd")

    assert info.error == ""
    assert info.init_segment_url == "https://cdn.example/path/files/start.mp4"
    assert info.segment_urls == [
        "https://cdn.example/path/files/one.m4s",
        "https://cdn.example/path/files/two.m4s",
    ]
