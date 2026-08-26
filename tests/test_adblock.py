"""Tests for the ad-block interceptor: blocklist matching, main-frame
exemption, and blocklist contents sanity."""
from __future__ import annotations

import os
import sys

import pytest

# QWebEngineUrlRequestInterceptor is a QObject subclass — needs a QApplication
# on some platforms.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWebEngineCore import QWebEngineUrlRequestInfo  # noqa: E402

from dlmgr.adblock import AdBlockInterceptor, _BLOCKED_DOMAINS  # noqa: E402


@pytest.fixture(scope="module")
def app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    yield app


def test_bing_not_blocklisted():
    assert "bing.com" not in _BLOCKED_DOMAINS
    assert "bat.bing.com" in _BLOCKED_DOMAINS


def test_host_matching_suffix(app):
    ab = AdBlockInterceptor()
    assert ab._host_matches_blocklist("ads.doubleclick.net")
    assert ab._host_matches_blocklist("doubleclick.net")
    assert not ab._host_matches_blocklist("example.com")
    assert not ab._host_matches_blocklist("notdoubleclick.net")


def test_main_frame_never_blocked(app):
    """Main-frame navigations to blocklisted domains must pass through."""
    ab = AdBlockInterceptor()
    ab.set_enabled(True)

    class FakeUrl:
        def host(self):
            return "doubleclick.net"

        def path(self):
            return "/ad.js"

    class FakeInfo:
        def __init__(self, rtype):
            self._rtype = rtype
            self.blocked = False

        def resourceType(self):
            return self._rtype

        def requestUrl(self):
            return FakeUrl()

        def block(self, b):
            self.blocked = b

    main = FakeInfo(QWebEngineUrlRequestInfo.ResourceType.ResourceTypeMainFrame)
    ab.interceptRequest(main)
    assert not main.blocked

    sub = FakeInfo(QWebEngineUrlRequestInfo.ResourceType.ResourceTypeScript)
    ab.interceptRequest(sub)
    assert sub.blocked


def test_disabled_blocks_nothing(app):
    ab = AdBlockInterceptor()
    ab.set_enabled(False)

    class FakeInfo:
        blocked = False

        def resourceType(self):
            return QWebEngineUrlRequestInfo.ResourceType.ResourceTypeScript

        def requestUrl(self):
            class U:
                def host(self):
                    return "doubleclick.net"

                def path(self):
                    return "/x.js"

            return U()

        def block(self, b):
            self.blocked = b

    info = FakeInfo()
    ab.interceptRequest(info)
    assert not info.blocked
