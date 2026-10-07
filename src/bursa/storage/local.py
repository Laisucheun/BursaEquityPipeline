from __future__ import annotations

import shutil
from pathlib import Path


class LocalBlobStore:
    """Filesystem-backed store. The development default."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        root = self.root.resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"key escapes storage root: {key!r}")
        return path

    def put(self, key: str, source: Path) -> str:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(source, dest)
        return str(dest)

    def put_bytes(self, key: str, data: bytes) -> str:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return str(dest)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def open_local(self, key: str) -> Path:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(path)
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).exists()
