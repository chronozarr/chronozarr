"""Coordinate the warning scopes used by Chronozarr's raster and Zarr writers."""

from __future__ import annotations

import os
import sys
import warnings
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from threading import Condition, get_ident

_condition = Condition()
_raster_depths: dict[int, int] = {}
_filters = ExitStack()
_exclusive_owner: int | None = None
_context_aware = getattr(sys.flags, "context_aware_warnings", False)


@contextmanager
def _ignore_unlocated_raster() -> Iterator[None]:
    from rasterio.errors import NotGeoreferencedWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        yield


def _set_raster_depth(thread: int, depth: int) -> None:
    """Join or leave the shared raster warning scope with the condition held."""
    if depth:
        if not _raster_depths:
            # With process-wide filters the last reader can exit the context started by the first.
            _filters.enter_context(_ignore_unlocated_raster())
        _raster_depths[thread] = depth
    else:
        del _raster_depths[thread]
        if not _raster_depths:
            try:
                _filters.close()
            finally:
                _condition.notify_all()


@contextmanager
def raster_open_warnings() -> Iterator[None]:
    """Use one filter for all concurrent raster opens on process-wide warning runtimes."""
    if _context_aware:
        with _ignore_unlocated_raster():
            yield
        return

    thread = get_ident()
    with _condition:
        _condition.wait_for(lambda: _exclusive_owner in (None, thread) or thread in _raster_depths)
        within_exclusive = _exclusive_owner == thread
        depth = _raster_depths.get(thread, 0)
        if not within_exclusive:
            _set_raster_depth(thread, depth + 1)
    if within_exclusive:
        with _ignore_unlocated_raster():
            yield
        return
    try:
        yield
    finally:
        with _condition:
            _set_raster_depth(thread, depth)


@contextmanager
def exclusive_warning_scope() -> Iterator[None]:
    """Keep a dependency's warning context from overlapping another such context."""
    if _context_aware:
        yield
        return

    global _exclusive_owner
    thread = get_ident()
    with _condition:
        nested = _exclusive_owner == thread
    if nested:
        yield
        return

    depth = 0
    entered = False
    try:
        with _condition:
            depth = _raster_depths.get(thread, 0)
            if depth:
                # An open can call back into a writer on this thread (for example, from logging).
                # Stop counting all its paused opens before waiting for any other writer or reader.
                _set_raster_depth(thread, 0)
            _condition.wait_for(lambda: _exclusive_owner is None)
            _exclusive_owner = thread
            # Reserve the exclusive scope before letting the active readers drain.
            _condition.wait_for(lambda: not _raster_depths)
            # Retain the caller's filters so a forked child can shed a vanished writer's scope,
            # including any warning filters installed inside the dependency.
            _filters.enter_context(warnings.catch_warnings())
            entered = True
        yield
    finally:
        with _condition:
            try:
                if entered:
                    _filters.close()
                if depth:
                    # If entry was interrupted, another writer may still own the warning scope.
                    _condition.wait_for(lambda: _exclusive_owner in (None, thread))
                    _set_raster_depth(thread, depth)
            finally:
                if _exclusive_owner == thread:
                    _exclusive_owner = None
                    _condition.notify_all()


def _reset_after_fork() -> None:
    """Keep only the scopes on the thread that survives a fork."""
    global _condition, _exclusive_owner
    thread = get_ident()
    depth = _raster_depths.get(thread)
    owns_filters = depth is not None if _raster_depths else _exclusive_owner == thread
    _condition = Condition()
    _raster_depths.clear()
    if depth is not None:
        _raster_depths[thread] = depth
    if _exclusive_owner != thread:
        _exclusive_owner = None
    if not owns_filters:
        _filters.close()


if hasattr(os, "register_at_fork") and not _context_aware:
    # Hold the condition only across the fork itself, so the child sees a complete transition.
    os.register_at_fork(
        before=lambda: _condition.acquire(),
        after_in_parent=lambda: _condition.release(),
        after_in_child=_reset_after_fork,
    )


def consolidate_zarr_metadata(path: str) -> None:
    import zarr
    from zarr.errors import ZarrUserWarning

    # Chronozarr deliberately writes v3 consolidated metadata so readers get it in one GET.
    with (
        exclusive_warning_scope(),
        warnings.catch_warnings(action="ignore", category=ZarrUserWarning),
    ):
        zarr.consolidate_metadata(path)
