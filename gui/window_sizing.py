"""Roomy top-level window sizing.

Menu-launched dialogs must open with MORE than enough space to show all
their content (user decision 2026-09-10): after the +50% font restyle the
old fixed sizes clipped content on screens that had plenty of room. The
rule ``roomy()`` implements:

* open at the content's sizeHint grown by ~15% headroom, so nothing is
  cramped and nothing clips;
* only shrink below that on small screens, and never below 90% of the
  available desktop — a capped dialog relies on its scroll areas (every
  settings page is a QScrollArea) rather than hiding content.

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


def _target_size(widget: QWidget, headroom: float, screen_fraction: float,
                 avail: Optional[QRect]) -> Optional[QSize]:
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
        target = target.boundedTo(cap)
    return target


def roomy(
    widget: QWidget,
    headroom: float = 1.15,
    screen_fraction: float = 0.9,
    avail: Optional[QRect] = None,
) -> None:
    """Resize ``widget`` to fit its content with headroom, capped at
    ``screen_fraction`` of the available screen — now and again on the
    next event-loop turn, when subclass content is laid out."""
    size = _target_size(widget, headroom, screen_fraction, avail)
    if size is not None:
        widget.resize(size)

    def _refit() -> None:
        # The closure keeps the widget's wrapper alive until the shot
        # fires, so the second pass always sees a live Python object.
        delayed = _target_size(widget, headroom, screen_fraction, avail)
        if delayed is not None:
            widget.resize(delayed)

    QTimer.singleShot(0, _refit)
