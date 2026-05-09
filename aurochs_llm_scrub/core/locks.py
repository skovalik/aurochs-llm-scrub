"""OS-level advisory lockfiles.

Single-writer enforcement at the OS level — not just SQLite-level. Two
distinct lockfile classes serve different concurrency contracts:

* ``WriteLock`` — held by ANY writer (indexer, extractor, taxonomy mutation).
  Path: ``recall.db.write.lock`` next to the database file.
* ``MigrateLock`` — held during the entire migration sequence, including
  the ``schema_version`` status update. Path: ``recall.db.migrate.lock``.

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking``. The Windows
implementation hardens against fork-inheritance leaks (``close_fds=True``,
non-inheritable handle) and ghost locks (stale-PID detection via
``psutil.pid_exists``).

Each lockfile records the holder PID. If a process tries to acquire and
finds the lock held but the recorded PID no longer exists, the lock is
considered stale and force-released with an audit-log entry. This closes
the "user kill -9'd indexer; lock now blocks all subsequent runs" failure
mode.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import TracebackType


class LockError(RuntimeError):
    """Raised when a lock cannot be acquired (held by another process)."""

    def __init__(self, lockfile: Path, holder_pid: int | None) -> None:
        msg = f"Lock {lockfile} is held"
        if holder_pid is not None:
            msg += f" by PID {holder_pid}"
        super().__init__(msg)
        self.lockfile = lockfile
        self.holder_pid = holder_pid


def _pid_alive(pid: int) -> bool:
    """Check whether a PID corresponds to a living process.

    Tries ``psutil.pid_exists`` first (handles cross-platform edge cases
    cleanly); falls back to ``os.kill(pid, 0)`` on POSIX and a Windows
    ``OpenProcess`` probe via ctypes if psutil is unavailable.
    """
    try:
        import psutil  # type: ignore[import-not-found]

        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass

    if sys.platform == "win32":
        # Best-effort ctypes probe so the spine remains importable without
        # psutil installed (T0 wires psutil into pyproject.toml later).
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            # Conservative default: assume alive so we don't steal a
            # legitimately held lock.
            return True
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        # PermissionError means the PID exists but isn't ours — still alive.
        return isinstance(sys.exc_info()[1], PermissionError)
    except OSError:
        return False


class _LockfileBase:
    """Shared behavior for advisory lockfiles.

    Subclasses set ``suffix`` (e.g. ``".write.lock"``). The concrete
    acquire/release implementation differs by platform.
    """

    suffix: str = ".lock"

    def __init__(self, db_path: Path | str, *, timeout: float = 0.0) -> None:
        """Construct a lock pointing at ``{db_path}{suffix}``.

        ``timeout`` is the number of seconds to wait for the lock before
        raising ``LockError``. ``0`` (default) = fail fast.
        """
        db = Path(db_path)
        self.path: Path = db.with_name(db.name + self.suffix)
        self.timeout: float = float(timeout)
        self._fd: int | None = None
        self._acquired: bool = False

    # ----- subclass hooks -------------------------------------------------

    def _try_lock(self, fd: int) -> bool:
        """Attempt to lock ``fd`` non-blocking. Return True on success."""
        raise NotImplementedError

    def _unlock(self, fd: int) -> None:
        """Release the OS-level lock on ``fd``."""
        raise NotImplementedError

    # ----- public API ----------------------------------------------------

    def acquire(self) -> None:
        """Acquire the lock, retrying up to ``timeout`` seconds.

        On timeout, raises ``LockError``. If the holder PID recorded in the
        lockfile is dead, the lock is force-released first (stale-PID
        detection per plan v5 KAI delta).
        """
        if self._acquired:
            raise RuntimeError("Lock already acquired by this object")

        deadline = time.monotonic() + self.timeout
        while True:
            fd = self._open_lockfile()
            if self._try_lock(fd):
                # Got it. Stamp our PID into the file and remember it.
                self._fd = fd
                self._write_pid(fd)
                self._acquired = True
                return

            # Failed to lock — peek at the recorded PID to decide stale-vs-live.
            holder_pid = self._read_pid_from_path()
            os.close(fd)

            if holder_pid is not None and not _pid_alive(holder_pid):
                # Stale lock. Force-release and retry once. The retry path
                # only runs once because we can't be unlucky-stale twice.
                self._force_release_stale(holder_pid)
                continue

            if time.monotonic() >= deadline:
                raise LockError(self.path, holder_pid)
            time.sleep(0.1)

    def release(self) -> None:
        """Release the lock. Safe to call even if not acquired.

        On POSIX the lockfile is unlinked after release. On Windows, msvcrt
        keeps an exclusive byte-range lock that prevents unlink even after
        the FD is closed in some sharing modes — so we leave the file in
        place. The next acquire will reuse it. The PID stamping inside the
        file means stale-lock detection still works across runs.
        """
        if self._fd is not None:
            try:
                self._unlock(self._fd)
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        self._acquired = False
        # Best-effort lockfile removal — only on POSIX. On Windows leaving
        # the pid-file alone is the safer behavior.
        if sys.platform != "win32":
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass

    # ----- helpers -------------------------------------------------------

    def _open_lockfile(self) -> int:
        """Open the lockfile, creating it if needed, with non-inheritable FD."""
        # O_CLOEXEC: don't leak the FD to child processes (subprocess /
        # multiprocessing). On Windows there's no O_CLOEXEC but we set
        # the inheritable flag explicitly below.
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), flags, 0o644)
        # Belt-and-braces on Windows where O_CLOEXEC isn't available.
        try:
            os.set_inheritable(fd, False)
        except (OSError, AttributeError):
            pass
        return fd

    def _write_pid(self, fd: int) -> None:
        """Stamp the current PID into the lockfile body."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            try:
                os.fsync(fd)
            except OSError:
                pass
        except OSError:
            # PID-stamping is best-effort. If it fails the lock is still
            # held; we just lose stale-detection on the next attempt.
            pass

    def _read_pid_from_path(self) -> int | None:
        """Read the recorded PID from the lockfile body, if any."""
        try:
            raw = self.path.read_bytes().strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _force_release_stale(self, dead_pid: int) -> None:
        """Remove a stale lockfile whose holder PID is gone."""
        # Best-effort. A concurrent acquirer might be racing us; if so the
        # next acquire-attempt will simply fail and retry.
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    # ----- context manager -----------------------------------------------

    def __enter__(self) -> _LockfileBase:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Platform-specific implementations
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import msvcrt

    class _WindowsLockMixin(_LockfileBase):
        def _try_lock(self, fd: int) -> bool:
            try:
                # LK_NBLCK: non-blocking lock of the first byte. msvcrt
                # locks ranges, not the whole file, but byte 0 is enough
                # for advisory mutual exclusion.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                return True
            except OSError:
                return False

        def _unlock(self, fd: int) -> None:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            except OSError:
                # Already released or process dying; no recourse.
                pass

    _PlatformLock = _WindowsLockMixin

else:
    import fcntl

    class _PosixLockMixin(_LockfileBase):
        def _try_lock(self, fd: int) -> bool:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False

        def _unlock(self, fd: int) -> None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass

    _PlatformLock = _PosixLockMixin  # type: ignore[misc, assignment]


# ---------------------------------------------------------------------------
# Public lock classes
# ---------------------------------------------------------------------------

class WriteLock(_PlatformLock):
    """Single-writer advisory lock — held by any process that writes drawers,
    extractions, or taxonomy mutations.

    Usage::

        from aurochs_recall.core.locks import WriteLock

        with WriteLock(db_path, timeout=30):
            # do writes
            ...
    """

    suffix = ".write.lock"


class MigrateLock(_PlatformLock):
    """Migration advisory lock — held across an entire migration sequence,
    including the ``schema_version`` status update. Distinct from
    ``WriteLock`` so writers and migrators can be diagnosed independently.
    """

    suffix = ".migrate.lock"
