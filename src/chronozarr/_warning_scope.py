"""Coordinate Rasterio operations that change Python warning filters."""

from __future__ import annotations

import os
import sys
import warnings
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from threading import RLock, get_ident

_filter_lock = RLock()
_raster_depths: dict[int, int] = {}
_filters = ExitStack()
_context_aware = getattr(sys.flags, "context_aware_warnings", False)


@contextmanager
def _ignore_unlocated_raster() -> Iterator[None]:
    from rasterio.errors import NotGeoreferencedWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        yield


@contextmanager
def raster_warning_scope() -> Iterator[None]:
    """Restore process-wide filters after all overlapping Rasterio operations finish."""
    if _context_aware:
        with _ignore_unlocated_raster():
            yield
        return

    thread = get_ident()
    with _filter_lock:
        if not _raster_depths:
            _filters.enter_context(_ignore_unlocated_raster())
        _raster_depths[thread] = _raster_depths.get(thread, 0) + 1
    # Rasterio can log into a callback that waits for another Rasterio operation.
    # Hold the lock only while joining or leaving the shared warning context.
    try:
        yield
    finally:
        with _filter_lock:
            depth = _raster_depths[thread] - 1
            if depth:
                _raster_depths[thread] = depth
            else:
                del _raster_depths[thread]
                if not _raster_depths:
                    _filters.close()


def _reset_after_fork() -> None:
    """Keep only the scopes on the thread that survives a fork."""
    global _filter_lock
    thread = get_ident()
    depth = _raster_depths.get(thread)
    _filter_lock = RLock()
    _raster_depths.clear()
    if depth is not None:
        _raster_depths[thread] = depth
    else:
        _filters.close()


if hasattr(os, "register_at_fork") and not _context_aware:
    # Hold the lock across the fork so the child sees a complete transition.
    os.register_at_fork(
        before=lambda: _filter_lock.acquire(),
        after_in_parent=lambda: _filter_lock.release(),
        after_in_child=_reset_after_fork,
    )
