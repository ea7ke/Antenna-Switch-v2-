"""Utilidades comunes de los tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from server.audit import MemoryAuditSink
from server.hardware import SimulatedHardware
from server.statemachine import Antenna, AntennaSwitch

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "antenna.toml.example"

# Mapa de pines de v1 (antenna-switch-control/etc/gpio_config.sh): el hardware
# existente no se toca, así que v2 debe usar exactamente estos pares.
V1_MAP = [
    (1, "10m Yagi", 2, 8),
    (2, "15m Yagi", 3, 9),
    (3, "20m Yagi", 4, 10),
    (4, "40m Dipolo", 5, 11),
    (5, "80m Dipolo", 6, 12),
    (6, "160m L Inv", 7, 13),
]


def antennas() -> list[Antenna]:
    return [Antenna(i, name, r1, r2) for i, name, r1, r2 in V1_MAP]


async def make_switch(
    *,
    active_high: bool = True,
    dead_time: float = 0.01,
    timeout: float = 0.5,
    ptt_guard=None,
    maintenance=None,
    hw: SimulatedHardware | None = None,
):
    hw = hw or SimulatedHardware(active_high=active_high)
    audit = MemoryAuditSink()
    sw = AntennaSwitch(
        hw,
        antennas(),
        dead_time_s=dead_time,
        maneuver_timeout_s=timeout,
        ptt_guard=ptt_guard,
        audit=audit,
        maintenance=maintenance,
    )
    await sw.startup()
    return sw, hw, audit


def assert_physical_invariants(case: unittest.TestCase, hw: SimulatedHardware) -> None:
    """Repasa TODOS los eventos de relé en orden y comprueba, en cada instante:

    * ninguna antena tiene cerrados a la vez el relé de R1 y el de R2;
    * cada radio tiene como máximo un relé cerrado.
    """
    ants = antennas()
    r1_pins = {a.pin_r1 for a in ants}
    r2_pins = {a.pin_r2 for a in ants}
    closed: set[int] = set()
    for ev in hw.events:
        if ev.closed:
            closed.add(ev.pin)
        else:
            closed.discard(ev.pin)
        for a in ants:
            case.assertFalse(
                a.pin_r1 in closed and a.pin_r2 in closed,
                f"{a.name} conectada a las dos radios a la vez (t={ev.t:.6f})",
            )
        case.assertLessEqual(len(closed & r1_pins), 1, "R1 con dos antenas a la vez")
        case.assertLessEqual(len(closed & r2_pins), 1, "R2 con dos antenas a la vez")
