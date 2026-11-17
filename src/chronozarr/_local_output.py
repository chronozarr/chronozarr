"""Build a local store off to the side and publish it with one directory rename."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path


def _occupied_output(out: Path) -> FileExistsError:
    return FileExistsError(
        f"{out} already exists and is not empty; stores are immutable, write to a new path"
    )


def _changed_output(out: Path) -> FileExistsError:
    return FileExistsError(f"output destination changed while writing: {out}")


def _resolve_output(out: Path, expected: Path | None) -> Path:
    destination = out.resolve()
    if expected is not None and destination != expected:
        raise _changed_output(out)
    return destination


def _access(path: Path, mode: int) -> bool:
    if os.access in os.supports_effective_ids:
        return os.access(path, mode, effective_ids=True)
    return os.access(path, mode)


def _inspect_output(out: Path, destination: Path) -> os.stat_result | None:
    try:
        info = destination.lstat()
    except FileNotFoundError:
        if out.is_symlink():
            raise _occupied_output(out) from None
        return None
    if not stat.S_ISDIR(info.st_mode) or any(destination.iterdir()):
        raise _occupied_output(out)
    if os.name == "nt":
        raise FileExistsError(
            f"{out} already exists; atomic directory publication on Windows needs an absent path"
        )
    if not _access(destination, os.R_OK | os.W_OK | os.X_OK):
        raise PermissionError(
            errno.EACCES, "output directory must be writable and searchable", out
        )
    return info


def _prepare_destination(out: Path, *, expected: Path | None = None) -> Path:
    """Check availability without changing the destination or following a new target."""
    destination = _resolve_output(out, expected)
    _inspect_output(out, destination)
    return destination


@contextmanager
def _private_output(
    destination: Path, *, prefix: str, scratch_parent: Path | None = None
) -> Iterator[Path]:
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    if not _access(parent, os.W_OK | os.X_OK):
        raise PermissionError(
            errno.EACCES, "atomic publication needs a writable destination parent", parent
        )
    # A colocated conversion work directory can hold its private store and spill. Keep
    # other builds beside the destination, including when work is on another filesystem.
    if (
        scratch_parent is not None
        and scratch_parent.parent == parent
        and scratch_parent.stat().st_dev == parent.stat().st_dev
    ):
        parent = scratch_parent
    scratch = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield scratch
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    # After publication only the empty wrapper remains. Failure to remove it cannot fail the write.
    with suppress(OSError):
        scratch.rmdir()


def _identity(info: os.stat_result | None) -> tuple[int, int, int, int, int, int] | None:
    if info is None:
        return None
    return (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)


def _copy_darwin_acl(source: int, target: int) -> None:
    # Python's macOS filesystem API does not expose ACLs. On Darwin acl_get_fd and acl_set_fd
    # use the native extended ACL, including inheritable rules. Set it exactly on the private fd.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_get_fd.argtypes = [ctypes.c_int]
    libc.acl_get_fd.restype = ctypes.c_void_p
    libc.acl_init.argtypes = [ctypes.c_int]
    libc.acl_init.restype = ctypes.c_void_p
    libc.acl_set_fd.argtypes = [ctypes.c_int, ctypes.c_void_p]
    libc.acl_set_fd.restype = ctypes.c_int
    libc.acl_free.argtypes = [ctypes.c_void_p]
    libc.acl_free.restype = ctypes.c_int

    acl = libc.acl_get_fd(source)
    if acl is None:
        code = ctypes.get_errno()
        if code in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        if code != errno.ENOENT:
            raise OSError(code, os.strerror(code))
        acl = libc.acl_init(0)
        if acl is None:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    try:
        if libc.acl_set_fd(target, acl):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    finally:
        libc.acl_free(acl)


if sys.platform == "linux":

    def _read_xattr(fd: int, name: str) -> bytes | None:
        try:
            return os.getxattr(fd, name)
        except OSError as error:
            if error.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise
            return None

    def _copy_linux_acls(source: int, target: int) -> None:
        for name in ("system.posix_acl_access", "system.posix_acl_default"):
            acl = _read_xattr(source, name)
            if acl is not None:
                os.setxattr(target, name, acl)
            elif _read_xattr(target, name) is not None:
                os.removexattr(target, name)


def _prepare_store(store: Path, out: Path, destination: Path) -> os.stat_result | None:
    """Make a private store inherit an empty destination's current permissions before writing."""
    original = _inspect_output(out, _resolve_output(out, destination))
    if original is None:
        return None
    store.mkdir()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        source = os.open(destination, flags)
        stack.callback(os.close, source)
        if _identity(os.fstat(source)) != _identity(original):
            raise _changed_output(out)
        target = os.open(store, flags)
        stack.callback(os.close, target)
        mode = stat.S_IMODE(original.st_mode)
        owner = original.st_uid, original.st_gid
        try:
            info = os.fstat(target)
            if (info.st_uid, info.st_gid) != owner:
                os.fchown(target, *owner)
            os.fchmod(target, mode)
            if sys.platform == "darwin":
                _copy_darwin_acl(source, target)
            elif sys.platform == "linux":
                _copy_linux_acls(source, target)
            else:
                raise OSError(errno.ENOTSUP, "preserving directory ACLs is unsupported")
            info = os.fstat(target)
            if stat.S_IMODE(info.st_mode) != mode or (info.st_uid, info.st_gid) != owner:
                raise OSError(errno.EACCES, "directory ownership or mode could not be preserved")
        except OSError as error:
            raise PermissionError(
                error.errno,
                "cannot preserve the output directory permissions; choose an absent path",
                out,
            ) from error
    return original


def _publish_output(
    store: Path, out: Path, destination: Path, original: os.stat_result | None
) -> None:
    current = _inspect_output(out, _resolve_output(out, destination))
    if _identity(original) != _identity(current):
        raise _changed_output(out)
    try:
        # POSIX can replace an empty directory. A single rename rejects nonempty directories,
        # files and symlinks that race into the destination.
        os.rename(store, destination)
    except OSError as error:
        if error.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR):
            raise _occupied_output(out) from error
        raise
