"""Roomy top-level window sizing.

Menu-launched dialogs must open with MORE than enough space to show all
their content (user decision 2026-09-10): after the +50% font restyle the
old fixed sizes clipped content on screens that had plenty of room. The
rule ``roomy()`` implements:

* open at the content's sizeHint grown by ~15% headroom, so nothing is
  cramped and nothing clips;
* only shrink below that on small screens, and never below 90% of the
  available desktop — a capped dialog relies on its scroll areas (every
  settings page is a QScrollArea) rather than hiding content;
* place the window TOP-CENTER of the available desktop, inset by the same
  margin the 90% cap leaves, so dialogs land predictably instead of
  wherever the parent happened to be (user request 3.5.5).

``roomy()`` fits immediately (best effort with whatever is laid out at the
call site) and again via a zero-timeout single shot, which fires once the
complete layout — including anything a subclass added after
``super().__init__()`` — exists. No event filters: a filter object
parented to the dialog outlives its Python wrapper across teardown and
crashes Qt with an access violation (verified 2026-09-10).

``avail`` (a QRect) can be injected by tests to simulate screen sizes.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRect, QSize, QTimer
from PySide6.QtWidgets import QApplication, QWidget


def _apply(
    widget: QWidget, headroom: float, screen_fraction: float, avail: Optional[QRect],
    floor_fraction: float,
) -> None:
    """Size AND place: fit content with headroom, floored at
    ``floor_fraction`` and capped at ``screen_fraction`` of the available
    desktop, then move to the top-center."""
    if avail is None:
        screen = widget.screen() if hasattr(widget, "screen") else None
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
    hint = widget.sizeHint().expandedTo(widget.minimumSizeHint())
    target = QSize(max(1, round(hint.width() * headroom)),
                   max(1, round(hint.height() * headroom)))
    if avail is not None:
        cap = QSize(max(1, round(avail.width() * screen_fraction)),
                    max(1, round(avail.height() * screen_fraction)))
        # Content hints can be deceptively small (a bare QListWidget hints
        # ~280px no matter how many rows it holds — the History dialog opened
        # at 320x289 because of it). A floor keeps dialogs substantial on
        # big screens: "only small windows on small screens".
        floor = QSize(max(1, round(avail.width() * floor_fraction)),
                      max(1, round(avail.height() * floor_fraction)))
        target = target.expandedTo(floor).boundedTo(cap)
    widget.resize(target)
    if avail is not None:
        # Top-center placement, inset by the same margin the 90% cap leaves
        # (e.g. 5% each side at fraction=0.9), so the window sits inside the
        # usable region instead of wherever the parent happened to be.
        margin = round(avail.height() * (1.0 - screen_fraction) / 2)
        x = avail.x() + max(0, (avail.width() - target.width()) // 2)
        y = avail.y() + max(0, margin)
        widget.move(x, y)


def roomy(
    widget: QWidget,
    headroom: float = 1.15,
    screen_fraction: float = 0.9,
    avail: Optional[QRect] = None,
    floor_fraction: float = 0.5,
) -> None:
    """Size and place ``widget`` — content sizeHint with headroom, at least
    ``floor_fraction`` of the available screen and never more than
    ``screen_fraction`` of it, positioned top-center — now and again on the
    next event-loop turn, when subclass content is laid out."""
    _apply(widget, headroom, screen_fraction, avail, floor_fraction)

    def _refit() -> None:
        # The closure keeps the widget's wrapper alive until the shot
        # fires, so the second pass always sees a live Python object.
        _apply(widget, headroom, screen_fraction, avail, floor_fraction)

    QTimer.singleShot(0, _refit)
