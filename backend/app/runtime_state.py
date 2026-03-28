from __future__ import annotations

from threading import Lock

from .config import AppConfig


class RuntimeState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._config = AppConfig()

    def get_config(self) -> AppConfig:
        with self._lock:
            return self._config.model_copy(deep=True)

    def update_config(self, config: AppConfig) -> None:
        with self._lock:
            self._config = config.model_copy(deep=True)
