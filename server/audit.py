"""Registro de auditoría.

Fase 1: los eventos van al log (una línea JSON por evento). La Fase 2 añade un
sumidero SQLite append-only con la misma interfaz, sin tocar la máquina de
estados.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuditSink(Protocol):
    def record(self, event: str, **fields: Any) -> None: ...


class LogAuditSink:
    """Escribe cada evento como una línea JSON en el logger 'antenna.audit'."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger("antenna.audit")

    def record(self, event: str, **fields: Any) -> None:
        entry = {"ts_utc": utcnow(), "event": event, **fields}
        self._log.info(json.dumps(entry, ensure_ascii=False, default=str))


class MemoryAuditSink:
    """Guarda los eventos en memoria (pruebas)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.events.append({"ts_utc": utcnow(), "event": event, **fields})

    def of(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]
