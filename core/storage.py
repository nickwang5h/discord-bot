import json
import logging
import os
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class JsonStore:
    """Small synchronous JSON store with process-local locking and atomic writes."""

    def __init__(self, path: Path, default_factory: Callable[[], Any]):
        self.path = path
        self.default_factory = default_factory
        self._lock = threading.RLock()

    def read(self, *, strict: bool = False) -> Any:
        with self._lock:
            if not self.path.exists():
                return self.default_factory()
            try:
                with self.path.open("r", encoding="utf-8") as file:
                    return json.load(file)
            except (OSError, json.JSONDecodeError, TypeError) as error:
                if strict:
                    raise RuntimeError(f"无法读取 JSON 存储 {self.path}: {error}") from error
                logger.error("无法读取 JSON 存储 %s: %s", self.path, error)
                return self.default_factory()

    def write(self, data: Any) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as file:
                    json.dump(data, file, indent=4, ensure_ascii=False)
                    file.flush()
                    os.fsync(file.fileno())
                    temp_path = Path(file.name)
                os.replace(temp_path, self.path)
            finally:
                if temp_path is not None and temp_path.exists():
                    temp_path.unlink()

    def update(self, mutator: Callable[[Any], Any | None]) -> Any:
        """Atomically read, mutate and persist data inside this process."""
        with self._lock:
            data = self.read(strict=True)
            replacement = mutator(deepcopy(data))
            updated = data if replacement is None else replacement
            self.write(updated)
            return updated
