"""Capa de hardware: el ÚNICO sitio del proyecto que toca los GPIO.

Toda la lógica trabaja en términos *lógicos* (``closed=True`` ⇒ el relé está
cerrado y la antena conectada). La polaridad eléctrica (``active_high``) se
traduce aquí y solo aquí.

Hay dos implementaciones:

* ``PigpioHardware``    — Raspberry Pi real, vía el demonio pigpiod.
* ``SimulatedHardware`` — sin hardware, con registro de eventos e inyección de
  fallos, para ejecutar y probar toda la lógica en un PC.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence


class HardwareError(RuntimeError):
    """Fallo al leer o escribir un GPIO."""


class Hardware(ABC):
    def __init__(self, active_high: bool = True) -> None:
        self.active_high = active_high
        self._pins: tuple[int, ...] = ()

    # -- polaridad ---------------------------------------------------------- #
    def _level(self, closed: bool) -> int:
        """Nivel eléctrico (0/1) que corresponde a relé cerrado/abierto."""
        return int(closed == self.active_high)

    def _closed(self, level: int) -> bool:
        """Estado lógico del relé para un nivel eléctrico leído."""
        return bool(level) == self.active_high

    # -- primitivas específicas de cada backend ------------------------------ #
    @abstractmethod
    def _write_level(self, pin: int, level: int) -> None: ...

    @abstractmethod
    def _read_level(self, pin: int) -> int: ...

    @abstractmethod
    def close(self) -> None: ...

    # -- API lógica ---------------------------------------------------------- #
    @property
    def pins(self) -> tuple[int, ...]:
        return self._pins

    def setup(self, pins: Sequence[int]) -> dict[int, bool]:
        """Toma el control de los pines dejándolos con los relés ABIERTOS.

        Devuelve el estado lógico que tenían justo antes de tomar el control
        (útil para auditar glitches de arranque). Si la lectura previa falla se
        sigue adelante: lo importante es dejar todo en el estado seguro.
        """
        self._pins = tuple(pins)
        found: dict[int, bool] = {}
        for pin in self._pins:
            try:
                found[pin] = self._closed(self._read_level(pin))
            except HardwareError:
                found[pin] = False
        for pin in self._pins:
            self._write_level(pin, self._level(False))
        return found

    def set_relay(self, pin: int, closed: bool) -> None:
        self._require_known(pin)
        self._write_level(pin, self._level(closed))

    def read_relay(self, pin: int) -> bool:
        self._require_known(pin)
        return self._closed(self._read_level(pin))

    def read_all(self) -> dict[int, bool]:
        return {pin: self.read_relay(pin) for pin in self._pins}

    def _require_known(self, pin: int) -> None:
        if pin not in self._pins:
            raise HardwareError(f"GPIO{pin} no está gestionado por esta instancia")


# --------------------------------------------------------------------------- #
# Raspberry Pi real
# --------------------------------------------------------------------------- #

class PigpioHardware(Hardware):
    """GPIO mediante pigpiod (el mismo demonio que usaba v1)."""

    def __init__(
        self,
        active_high: bool = True,
        *,
        pi: Any = None,
        host: str = "localhost",
        port: int = 8888,
    ) -> None:
        super().__init__(active_high)
        self._owns_pi = pi is None
        if pi is None:
            try:
                import pigpio  # type: ignore[import-not-found]
            except ImportError as exc:
                raise HardwareError(
                    "falta el paquete 'pigpio' (pip install pigpio)"
                ) from exc
            pi = pigpio.pi(host, port)
        if not getattr(pi, "connected", False):
            raise HardwareError(
                "no se pudo conectar con pigpiod; ¿está en marcha "
                "(systemctl status pigpiod)?"
            )
        self._pi = pi

    def _write_level(self, pin: int, level: int) -> None:
        # pigpio.write() pasa el pin a modo salida con ese nivel en un solo
        # paso, sin un instante intermedio con el pin como entrada.
        try:
            self._pi.write(pin, level)
        except Exception as exc:
            raise HardwareError(f"error escribiendo GPIO{pin}: {exc}") from exc

    def _read_level(self, pin: int) -> int:
        try:
            return int(self._pi.read(pin))
        except Exception as exc:
            raise HardwareError(f"error leyendo GPIO{pin}: {exc}") from exc

    def close(self) -> None:
        # Los pines conservan su último nivel al desconectar el cliente.
        if self._owns_pi:
            try:
                self._pi.stop()
            except Exception:  # pragma: no cover - cierre best-effort
                pass


# --------------------------------------------------------------------------- #
# Simulador
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RelayEvent:
    t: float
    pin: int
    closed: bool
    source: str  # "write" (software) | "external" (glitch inyectado)


class SimulatedHardware(Hardware):
    """Backend sin hardware. Registra cada cambio y permite inyectar fallos."""

    def __init__(
        self,
        active_high: bool = True,
        *,
        clock: Callable[[], float] = time.monotonic,
        initial_closed: Iterable[int] = (),
    ) -> None:
        super().__init__(active_high)
        self._clock = clock
        self._levels: dict[int, int] = {}
        self._stuck_closed: set[int] = set()
        self._fail_writes: set[int] = set()
        self.events: list[RelayEvent] = []
        for pin in initial_closed:
            self._levels[pin] = self._level(True)

    def _write_level(self, pin: int, level: int) -> None:
        if pin in self._fail_writes:
            raise HardwareError(f"fallo de escritura simulado en GPIO{pin}")
        self._levels[pin] = level
        self.events.append(RelayEvent(self._clock(), pin, self._closed(level), "write"))

    def _read_level(self, pin: int) -> int:
        if pin in self._stuck_closed:
            return self._level(True)
        return self._levels.get(pin, self._level(False))

    def close(self) -> None:
        pass

    # -- inyección de fallos (solo para pruebas) ----------------------------- #
    def force(self, pin: int, closed: bool) -> None:
        """Cambio externo no solicitado por el software (glitch, mano en el bus)."""
        self._levels[pin] = self._level(closed)
        self.events.append(RelayEvent(self._clock(), pin, closed, "external"))

    def stick_closed(self, pin: int) -> None:
        """El relé se queda pegado: lee 'cerrado' aunque se le ordene abrir."""
        self._stuck_closed.add(pin)
        self.events.append(RelayEvent(self._clock(), pin, True, "external"))

    def unstick(self, pin: int) -> None:
        """Simula la reparación de un relé pegado."""
        self._stuck_closed.discard(pin)

    def fail_writes(self, pin: int) -> None:
        self._fail_writes.add(pin)

    def closed_pins(self) -> set[int]:
        return {p for p in self._levels if self._closed(self._read_level(p))} | {
            p for p in self._stuck_closed
        }
