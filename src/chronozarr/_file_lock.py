"""An exclusive nonblocking lock shared by local writers."""

from __future__ import annotations

import errno
import hashlib
import os
import sys
import unicodedata
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def exclusive_file_lock(path: Path, *, conflict_message: str) -> Iterator[None]:
    """Keep the file in place so every writer opens the same lock. The OS releases it on exit."""
    with path.open("a+b") as lock:
        fd = lock.fileno()
        contention = (
            (errno.EACCES,) if sys.platform == "win32" else (errno.EAGAIN, errno.EWOULDBLOCK)
        )
        try:
            if sys.platform == "win32":
                lock.seek(0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in contention:
                raise
            raise BlockingIOError(conflict_message) from exc
        try:
            yield
        finally:
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def local_store_write_lock(
    store: Path, *, invalid_message: str, conflict_message: str
) -> Iterator[Path]:
    """Coordinate append and band-role changes at a store's canonical local path."""
    original = store.resolve()
    if not original.is_dir():
        raise ValueError(invalid_message)
    # Keep the original append lock name so already installed append clients still share it.
    lock_path = original.with_name(f".{original.name}.append.lock")
    with ExitStack() as stack:
        try:
            stack.enter_context(exclusive_file_lock(lock_path, conflict_message=conflict_message))
        except OSError as error:
            if error.errno != errno.ENAMETOOLONG and getattr(error, "winerror", None) != 206:
                raise
            # An old client cannot have this overlong lock. Use the same bounded name for
            # both writers, including on filesystems that coalesce case or Unicode variants.
            name = unicodedata.normalize("NFC", original.name.casefold())
            digest = hashlib.sha256(os.fsencode(name)).hexdigest()[:32]
            fallback = original.with_name(f".chronozarr-write-{digest}.lock")
            stack.enter_context(exclusive_file_lock(fallback, conflict_message=conflict_message))
        yield original
