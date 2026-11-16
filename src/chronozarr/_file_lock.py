"""An exclusive nonblocking lock shared by local writers."""

from __future__ import annotations

import errno
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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
