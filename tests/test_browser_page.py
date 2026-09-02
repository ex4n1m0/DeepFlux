from __future__ import annotations

import os

import pytest
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEnginePermission, QWebEngineProfile

from gui.main_window import MainWindow, _BrowserPage


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def page(app):
    profile = QWebEngineProfile()
    browser_page = _BrowserPage(profile)
    yield browser_page
    browser_page.deleteLater()
    profile.deleteLater()


def _navigate(page, url):
    return page.acceptNavigationRequest(
        QUrl(url), QWebEnginePage.NavigationType.NavigationTypeLinkClicked, True)


def test_navigation_blocks_active_local_and_data_schemes(page):
    assert _navigate(page, "javascript:alert(1)") is False
    assert _navigate(page, "file:///secret.txt") is False
    assert _navigate(page, "data:text/html,test") is False
    assert _navigate(page, "blob:https://example.com/id") is False
    assert _navigate(page, "https://example.com") is True


def test_magnet_navigation_is_intercepted(page):
    received = []
    page.magnetRequested.connect(received.append)
    assert _navigate(page, "magnet:?xt=urn:btih:" + "a" * 40) is False
    assert received and received[0].startswith("magnet:")


def test_capture_permissions_are_denied_without_prompt(page):
    result = page._decide_permission(
        QWebEnginePage.Feature.MediaAudioCapture,
        prompt=True,
        origin="https://example.com",
    )
    assert result == QWebEnginePage.PermissionPolicy.PermissionDeniedByUser


def test_permission_signal_handler_denies_capture_without_dialog():
    permission = MagicMock()
    permission.permissionType.return_value = QWebEnginePermission.PermissionType.MediaVideoCapture

    MainWindow._browser_permission_requested(MagicMock(), permission)

    permission.deny.assert_called_once()
    permission.grant.assert_not_called()
