import errno
import fcntl
import os
import stat
from pathlib import Path
from types import TracebackType
from uuid import UUID, uuid4

_LOCK_NAME = ".cairn-instance.lock"
_LOCK_OPEN_FLAGS = os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


class LeaseError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"data directory lease error: {code}")


class DataDirectoryLease:
    def __init__(self, data_path: Path, instance_id: UUID) -> None:
        self._data_path = data_path
        self._instance_id = instance_id
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return

        directory_fd: int | None = None
        fd: int | None = None
        failure_code: str | None = None
        try:
            directory_fd = os.open(
                self._data_path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except OSError:
            failure_code = "data_unavailable"

        if directory_fd is not None:
            try:
                writable = os.access(
                    ".",
                    os.W_OK,
                    dir_fd=directory_fd,
                    effective_ids=True,
                )
            except OSError:
                writable = False
            if not writable:
                failure_code = "data_unavailable"

        if directory_fd is not None and failure_code is None:
            try:
                fd = self._open_lock_file(directory_fd)
            except OSError:
                failure_code = "data_unavailable"

        if directory_fd is not None:
            self._close(directory_fd)

        if fd is not None:
            try:
                file_status = os.fstat(fd)
                valid_target = (
                    stat.S_ISREG(file_status.st_mode)
                    and stat.S_IMODE(file_status.st_mode) == 0o660
                )
            except OSError:
                valid_target = False
            if not valid_target:
                failure_code = "data_unavailable"

        if fd is not None and failure_code is None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    failure_code = "already_locked"
                else:
                    failure_code = "data_unavailable"

        if fd is not None and failure_code is None:
            payload = f"{self._instance_id}\n".encode("ascii")
            try:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                if os.write(fd, payload) != len(payload):
                    failure_code = "data_unavailable"
                else:
                    os.fsync(fd)
            except OSError:
                failure_code = "data_unavailable"

        if failure_code is not None:
            if fd is not None:
                self._close(fd)
            raise LeaseError(failure_code)

        if fd is None:
            raise LeaseError("data_unavailable")
        self._fd = fd

    @classmethod
    def _open_lock_file(cls, directory_fd: int) -> int:
        try:
            return os.open(
                _LOCK_NAME,
                _LOCK_OPEN_FLAGS,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return cls._prepare_and_publish_lock(directory_fd)

    @classmethod
    def _prepare_and_publish_lock(cls, directory_fd: int) -> int:
        temporary_name = f".cairn-instance-lock-{uuid4().hex}.tmp"
        fd: int | None = None
        try:
            fd = os.open(
                temporary_name,
                _LOCK_OPEN_FLAGS | os.O_CREAT | os.O_EXCL,
                0o660,
                dir_fd=directory_fd,
            )
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "invalid lock target")
            os.fchmod(fd, 0o660)
            os.fsync(fd)
            try:
                os.link(
                    temporary_name,
                    _LOCK_NAME,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                cls._close(fd)
                fd = None
                cls._unlink(temporary_name, directory_fd)
                return os.open(
                    _LOCK_NAME,
                    _LOCK_OPEN_FLAGS,
                    dir_fd=directory_fd,
                )
            cls._unlink(temporary_name, directory_fd)
            return fd
        except OSError:
            if fd is not None:
                cls._close(fd)
            cls._unlink(temporary_name, directory_fd)
            raise

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return

        failed = False
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            failed = True
        try:
            os.close(fd)
        except OSError:
            failed = True
        if failed:
            raise LeaseError("data_unavailable")

    async def __aenter__(self) -> "DataDirectoryLease":
        self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    @staticmethod
    def _close(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    @staticmethod
    def _unlink(name: str, directory_fd: int) -> None:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
