"""Protocolo sd_notify mínimo (sin dependencias) para systemd Type=notify.

READY=1 avisa de que el servicio está listo y WATCHDOG=1 es el latido que exige
``WatchdogSec=`` en la unidad. Fuera de systemd (sin NOTIFY_SOCKET) no hace nada.
"""

from __future__ import annotations

import os
import socket


def _send(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):  # socket abstracto de Linux
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
        return True
    except OSError:
        return False


def ready() -> bool:
    return _send("READY=1")


def watchdog() -> bool:
    return _send("WATCHDOG=1")


def stopping() -> bool:
    return _send("STOPPING=1")


def watchdog_interval() -> float | None:
    """Segundos entre latidos (la mitad del plazo de systemd), o None si no aplica."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    pid = os.environ.get("WATCHDOG_PID")
    if pid and pid.isdigit() and int(pid) != os.getpid():
        return None
    try:
        return max(float(usec) / 1_000_000 / 2, 1.0)
    except ValueError:
        return None
