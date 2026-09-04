from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

_COPY_BUFFER_BYTES = 1024 * 1024


class VaultIntegrityError(RuntimeError):
    """Raised when bytes at a content-addressed path do not match its digest."""


@dataclass(frozen=True, slots=True)
class StoredFile:
    sha256: str
    path: Path
    size_bytes: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_COPY_BUFFER_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


class FileVault:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def store(self, source: Path) -> StoredFile:
        source = Path(source)
        digest = sha256_file(source)
        target_directory = self.root / digest[:2]
        target = target_directory / digest

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        target_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_directory.chmod(0o700)

        if self._target_exists(target):
            self._verify(target, digest)
            target.chmod(0o600)
            return self._stored_file(target, digest)

        temporary_fd, temporary_name = tempfile.mkstemp(
            dir=target_directory,
            prefix=f".{digest}.",
            suffix=".partial",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(temporary_fd, "wb") as destination, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, destination, length=_COPY_BUFFER_BYTES)
                destination.flush()
                os.fsync(destination.fileno())
            temporary.chmod(0o600)
            self._verify(temporary, digest)

            try:
                os.link(temporary, target)
            except FileExistsError:
                self._verify(target, digest)
        finally:
            temporary.unlink(missing_ok=True)

        target.chmod(0o600)
        self._verify(target, digest)
        return self._stored_file(target, digest)

    @staticmethod
    def _verify(path: Path, expected_sha256: str) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise VaultIntegrityError(f"Vault object {path} is not a regular file") from error

        digest = hashlib.sha256()
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise VaultIntegrityError(f"Vault object {path} is not a regular file")
            while chunk := os.read(descriptor, _COPY_BUFFER_BYTES):
                digest.update(chunk)
        finally:
            os.close(descriptor)

        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise VaultIntegrityError(
                f"Vault object {path} has SHA-256 {actual_sha256}, expected {expected_sha256}"
            )

    @staticmethod
    def _target_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _stored_file(path: Path, digest: str) -> StoredFile:
        return StoredFile(sha256=digest, path=path, size_bytes=path.stat().st_size)
