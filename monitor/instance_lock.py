"""Cross-platform single-instance lock keyed by the monitor state database."""

from __future__ import annotations

import os
import pathlib
import sys
from typing import BinaryIO, Optional, Union


class InstanceAlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, state_path: Union[str, pathlib.Path]) -> None:
        state_path = pathlib.Path(state_path)
        self.path = state_path.with_name(state_path.name + ".lock")
        self._stream: Optional[BinaryIO] = None

    def acquire(self) -> None:
        if self._stream is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        stream = os.fdopen(descriptor, "r+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() < 20:
                stream.write(b" " * (20 - stream.tell()))
                stream.flush()
            _lock_stream(stream)
            stream.seek(0)
            stream.write(f"{os.getpid():<20}".encode("ascii"))
            stream.flush()
        except (OSError, BlockingIOError) as error:
            stream.close()
            raise InstanceAlreadyRunning(
                f"another monitor is already using state database {self.path.name[:-5]}"
            ) from error
        self._stream = stream

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            _unlock_stream(self._stream)
        finally:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _lock_stream(stream: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_stream(stream: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
