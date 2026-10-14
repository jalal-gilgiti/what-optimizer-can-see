"""Minimal adapter contract; engines may expose additional native metrics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any


class SystemAdapter(ABC):
    @abstractmethod
    def setup(self) -> None: ...

    @abstractmethod
    def load_data(self, rows: Iterable[dict[str, int]]) -> None: ...

    @abstractmethod
    def register_udf(self, bounds: tuple[int, ...]) -> None: ...

    @abstractmethod
    def run_query(self) -> dict[str, Any]: ...

    @abstractmethod
    def explain_query(self) -> str: ...

    @abstractmethod
    def collect_metrics(self) -> dict[str, Any]: ...

    @abstractmethod
    def cleanup(self) -> None: ...
