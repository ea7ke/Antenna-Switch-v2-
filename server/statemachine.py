"""Máquina de estados del conmutador 6x2.

Garantías (el conmutador NO tiene interlock hardware, así que dependen de este
módulo):

1. Exclusividad: ``assign[R1] != assign[R2]`` salvo que ambas sean ``None``.
   Se valida dentro de un lock global ANTES de tocar ningún relé.
2. Una sola antena por radio y *break-before-make* estricto con tiempo muerto.
3. Tras cada maniobra se relee el estado real de todos los GPIO; si no coincide
   con lo esperado, se abre todo y se entra en FAULT.
4. Una maniobra no puede quedar a medias: se ejecuta en una tarea protegida
   frente a cancelación y bajo un timeout global; si expira, todo abierto + FAULT.
5. Estado seguro = NONE en ambas radios (todos los relés abiertos). Es preferible
   dejar una estación sin antena que dejar dos conectadas entre sí.
6. Nunca se restaura "a ciegas" la última asignación al arrancar.

Todo el acceso a GPIO pasa por ``Hardware``; este módulo no importa pigpio.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Iterable, Mapping, Protocol, TypeVar

from .audit import AuditSink, LogAuditSink, utcnow
from .hardware import Hardware, HardwareError

log = logging.getLogger("antenna.switch")

T = TypeVar("T")


class Radio(str, Enum):
    R1 = "R1"
    R2 = "R2"

    @property
    def other(self) -> "Radio":
        return Radio.R2 if self is Radio.R1 else Radio.R1


@dataclass(frozen=True)
class Antenna:
    id: int
    name: str
    pin_r1: int
    pin_r2: int
    bands: str = ""
    notes: str = ""

    def pin(self, radio: Radio) -> int:
        return self.pin_r1 if radio is Radio.R1 else self.pin_r2


class SwitchError(Exception):
    """Una petición rechazada o una maniobra fallida. ``status`` es el HTTP sugerido."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# Interlock de PTT (Fase 3 lo implementa de verdad)
# --------------------------------------------------------------------------- #

class PttGuard(Protocol):
    async def begin_maneuver(self, radio: Radio) -> str:
        """Comprueba RX, inhibe TX y devuelve el estado de PTT para la auditoría.

        Debe lanzar ``SwitchError`` (TX_ACTIVE / PTT_UNKNOWN) si no se puede
        conmutar con seguridad.
        """

    async def end_maneuver(self, radio: Radio) -> None:
        """Libera el bloqueo de TX. Se llama siempre que begin_maneuver tuvo éxito."""


class NoPttGuard:
    """Fase 1: sin interlock. La protección contra hot switching es del operador."""

    async def begin_maneuver(self, radio: Radio) -> str:
        return "UNCHECKED"

    async def end_maneuver(self, radio: Radio) -> None:
        return None


# --------------------------------------------------------------------------- #
# Errores internos de la fase física
# --------------------------------------------------------------------------- #

class _VerifyFailed(Exception):
    def __init__(self, pins: list[int], phase: str) -> None:
        super().__init__(f"GPIO distinto de lo esperado ({phase}): {pins}")
        self.pins = pins
        self.phase = phase


class _SiblingClosed(Exception):
    pass


# --------------------------------------------------------------------------- #
# El conmutador
# --------------------------------------------------------------------------- #

class AntennaSwitch:
    def __init__(
        self,
        hardware: Hardware,
        antennas: Iterable[Antenna],
        *,
        dead_time_s: float = 0.150,
        maneuver_timeout_s: float = 2.0,
        ptt_guard: PttGuard | None = None,
        audit: AuditSink | None = None,
        maintenance: Mapping[int, str] | None = None,
        radio_labels: Mapping[str, str] | None = None,
    ) -> None:
        antennas = list(antennas)
        if not antennas:
            raise ValueError("hace falta al menos una antena")
        ids = [a.id for a in antennas]
        if len(set(ids)) != len(ids):
            raise ValueError("ids de antena repetidos")
        pins = [p for a in antennas for p in (a.pin_r1, a.pin_r2)]
        if len(set(pins)) != len(pins):
            raise ValueError("pines GPIO repetidos")
        if maneuver_timeout_s <= dead_time_s:
            raise ValueError("el timeout de maniobra debe superar al tiempo muerto")

        self._hw = hardware
        self._antennas: dict[int, Antenna] = {a.id: a for a in antennas}
        self._dead_time = dead_time_s
        self._timeout = maneuver_timeout_s
        self._ptt: PttGuard = ptt_guard or NoPttGuard()
        self._audit: AuditSink = audit or LogAuditSink()
        self._maintenance: dict[int, str] = dict(maintenance or {})
        self._labels = {"R1": "Radio 1", "R2": "Radio 2", **(radio_labels or {})}

        self._assign: dict[Radio, int | None] = {Radio.R1: None, Radio.R2: None}
        self._fault: str | None = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Future[Any]] = set()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._started = False
        self._boot_id = uuid.uuid4().hex[:8]  # distingue reinicios del servicio
        self._seq = 0
        self._updated_at = utcnow()

    # ------------------------------------------------------------------ #
    # Ciclo de vida
    # ------------------------------------------------------------------ #

    async def startup(self) -> None:
        """Toma el control del hardware y lo deja en estado seguro (todo abierto)."""
        async with self._lock:
            pins = [p for a in self._antennas.values() for p in (a.pin_r1, a.pin_r2)]
            try:
                found = self._hw.setup(pins)
            except HardwareError as exc:
                self._audit.record(
                    "FAULT", code="HARDWARE_ERROR", detail=str(exc), result="ERROR"
                )
                raise
            found_closed = sorted(p for p, closed in found.items() if closed)
            bad = self._verify()
            self._started = True
            self._audit.record("BOOT", found_closed=found_closed, result="OK")
            if bad:
                self._fault = "RELAY_MISMATCH"
                self._audit.record(
                    "RELAY_MISMATCH", mismatched=bad, phase="boot", result="ERROR"
                )
            self._touch()
            self._publish()

    async def shutdown(self) -> None:
        """Parada ordenada: estado seguro y liberación del hardware."""
        try:
            await self._shielded(self._release_all_locked("system", "shutdown"))
        except SwitchError as exc:
            log.error("al apagar no se pudo llegar al estado seguro: %s", exc.message)
        finally:
            self._hw.close()

    # ------------------------------------------------------------------ #
    # Operaciones públicas
    # ------------------------------------------------------------------ #

    async def switch(
        self, radio: Radio | str, antenna_id: int | None, *, actor: str = "local"
    ) -> dict[str, Any]:
        """Conecta ``antenna_id`` a ``radio`` (o ninguna si es ``None``)."""
        radio = Radio(radio)
        return await self._shielded(self._switch_locked(radio, antenna_id, actor))

    async def release_all(
        self, *, actor: str = "local", reason: str = "emergency"
    ) -> dict[str, Any]:
        """Parada de emergencia: abre todos los relés. Siempre permitida."""
        return await self._shielded(self._release_all_locked(actor, reason))

    async def set_maintenance(
        self, antenna_id: int, reason: str | None, *, actor: str = "local"
    ) -> dict[str, Any]:
        """Marca (reason) o desmarca (None) una antena como fuera de servicio.

        No desconecta una antena que esté en uso: solo impide seleccionarla.
        """
        async with self._lock:
            if antenna_id not in self._antennas:
                raise SwitchError("UNKNOWN_ANTENNA", f"La antena {antenna_id} no existe", 404)
            if reason:
                self._maintenance[antenna_id] = reason
            else:
                self._maintenance.pop(antenna_id, None)
            self._audit.record(
                "CONFIG_CHANGED",
                actor=actor,
                antenna=antenna_id,
                maintenance=reason or None,
                result="OK",
            )
            self._touch()
            self._publish()
            return self.snapshot()

    # ------------------------------------------------------------------ #
    # Estado y suscripciones
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        holder = {a: r for r, a in self._assign.items() if a is not None}
        expected = self._expected_closed()
        relays: dict[str, bool | None] = {}
        for pin in self._hw.pins:
            try:
                relays[str(pin)] = self._hw.read_relay(pin)
            except HardwareError:
                relays[str(pin)] = None
        relays_ok = bool(relays) and all(
            closed is not None and closed == (int(pin) in expected)
            for pin, closed in relays.items()
        )
        return {
            "boot": self._boot_id,
            "seq": self._seq,
            "updated_at": self._updated_at,
            "fault": self._fault,
            "relays_ok": relays_ok,
            "radios": {
                r.value: {
                    "label": self._labels.get(r.value, r.value),
                    "antenna_id": self._assign[r],
                    "antenna": (
                        self._antennas[self._assign[r]].name
                        if self._assign[r] is not None
                        else None
                    ),
                }
                for r in Radio
            },
            "antennas": [
                {
                    "id": a.id,
                    "name": a.name,
                    "bands": a.bands,
                    "notes": a.notes,
                    "maintenance": self._maintenance.get(a.id),
                    "in_use_by": holder[a.id].value if a.id in holder else None,
                }
                for a in self._antennas.values()
            ],
            "relays": relays,
        }

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=16)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def fault(self) -> str | None:
        return self._fault

    # ------------------------------------------------------------------ #
    # Implementación
    # ------------------------------------------------------------------ #

    def _spawn(self, coro: Awaitable[T]) -> "asyncio.Future[T]":
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: "asyncio.Future[Any]") -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # marca la excepción como recuperada

    async def _shielded(self, coro: Awaitable[T]) -> T:
        """Ejecuta la sección crítica aunque quien llama sea cancelado."""
        return await asyncio.shield(self._spawn(coro))

    def _touch(self) -> None:
        self._updated_at = utcnow()

    def _publish(self) -> None:
        self._seq += 1
        state = self.snapshot()
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass
            queue.put_nowait(state)

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("AntennaSwitch.startup() no se ha ejecutado")

    def _expected_closed(self) -> set[int]:
        return {
            self._antennas[ant].pin(radio)
            for radio, ant in self._assign.items()
            if ant is not None
        }

    def _verify(self) -> list[int]:
        """Pines cuyo estado real difiere del esperado (o que no se pueden leer)."""
        expected = self._expected_closed()
        bad: list[int] = []
        for pin in self._hw.pins:
            try:
                closed = self._hw.read_relay(pin)
            except HardwareError:
                bad.append(pin)
                continue
            if closed != (pin in expected):
                bad.append(pin)
        return bad

    def _open_all(self) -> list[int]:
        """Abre todos los relés. Devuelve los pines que no se pudieron escribir."""
        failed: list[int] = []
        for pin in self._hw.pins:
            try:
                self._hw.set_relay(pin, False)
            except HardwareError as exc:
                log.error("no se pudo abrir GPIO%s: %s", pin, exc)
                failed.append(pin)
        return failed

    # -- validación (sin tocar hardware) ---------------------------------- #

    def _validate(self, radio: Radio, dest: int | None, prev: int | None) -> None:
        if self._fault:
            raise SwitchError(
                "FAULT",
                f"El sistema está en FAULT ({self._fault}); un administrador debe "
                "revisarlo y usar la parada de emergencia para rearmarlo",
                503,
            )
        if dest is None or dest == prev:
            return
        antenna = self._antennas.get(dest)
        if antenna is None:
            raise SwitchError("UNKNOWN_ANTENNA", f"La antena {dest} no existe", 404)
        if dest in self._maintenance:
            raise SwitchError(
                "MAINTENANCE",
                f"{antenna.name} está fuera de servicio: {self._maintenance[dest]}",
            )
        holder = self._assign[radio.other]
        if holder == dest:
            raise SwitchError(
                "ANTENNA_IN_USE",
                f"{antenna.name} está conectada a {radio.other.value}",
            )

    # -- maniobra ----------------------------------------------------------- #

    async def _switch_locked(
        self, radio: Radio, dest: int | None, actor: str
    ) -> dict[str, Any]:
        async with self._lock:
            self._require_started()
            prev = self._assign[radio]
            t0 = time.monotonic()

            try:
                self._validate(radio, dest, prev)
            except SwitchError as exc:
                self._audit_denied(exc, radio, prev, dest, actor)
                raise

            if dest == prev:
                return self.snapshot()  # ya está así: no se mueve ningún relé

            ptt_state = await self._ptt_begin(radio, prev, dest, actor)
            try:
                await self._execute(radio, prev, dest, actor, ptt_state)
            finally:
                await self._ptt_end(radio)

            latency_ms = int((time.monotonic() - t0) * 1000)
            self._audit.record(
                "SWITCH_OK",
                actor=actor,
                radio=radio.value,
                ant_from=prev,
                ant_to=dest,
                ptt_state=ptt_state,
                result="OK",
                latency_ms=latency_ms,
            )
            self._touch()
            self._publish()
            return self.snapshot()

    async def _ptt_begin(
        self, radio: Radio, prev: int | None, dest: int | None, actor: str
    ) -> str:
        try:
            return await asyncio.wait_for(
                self._ptt.begin_maneuver(radio), self._timeout
            )
        except SwitchError as exc:
            self._audit_denied(exc, radio, prev, dest, actor)
            raise
        except Exception as exc:  # incluye el timeout: sin respuesta = desconocido
            err = SwitchError(
                "PTT_UNKNOWN",
                f"No se pudo confirmar que {radio.value} esté en recepción: "
                f"{str(exc) or type(exc).__name__}",
                428,
            )
            self._audit_denied(err, radio, prev, dest, actor)
            raise err from exc

    async def _ptt_end(self, radio: Radio) -> None:
        try:
            await self._ptt.end_maneuver(radio)
        except Exception:  # best-effort: no debe tapar el resultado de la maniobra
            log.exception("fallo liberando el bloqueo de PTT de %s", radio.value)

    async def _execute(
        self,
        radio: Radio,
        prev: int | None,
        dest: int | None,
        actor: str,
        ptt_state: str,
    ) -> None:
        """Fase física. Cualquier fallo ⇒ todo abierto + FAULT."""
        try:
            await asyncio.wait_for(self._physical(radio, prev, dest), self._timeout)
            return
        except asyncio.TimeoutError:
            code, status = "MANEUVER_TIMEOUT", 504
            message = "La maniobra superó el tiempo máximo; se han abierto todos los relés"
        except (_VerifyFailed, _SiblingClosed) as exc:
            code, status = "RELAY_MISMATCH", 503
            message = f"El estado real de los relés no es el esperado: {exc}"
        except Exception as exc:
            code, status = "HARDWARE_ERROR", 503
            message = f"Fallo de hardware durante la maniobra: {exc}"
        self._enter_fault(
            code, message, actor=actor, radio=radio, prev=prev, dest=dest, ptt_state=ptt_state
        )
        raise SwitchError(code, message, status)

    async def _physical(
        self, radio: Radio, prev: int | None, dest: int | None
    ) -> None:
        # 0. El estado real debe ser el que creemos antes de mover nada.
        bad = self._verify()
        if bad:
            raise _VerifyFailed(bad, "antes de la maniobra")

        old_pin = self._antennas[prev].pin(radio) if prev is not None else None
        new_pin = self._antennas[dest].pin(radio) if dest is not None else None

        # 1. Break: abrir el relé actual.
        if old_pin is not None:
            self._hw.set_relay(old_pin, False)
            self._assign[radio] = None

        # 2. Dead time (solo si hay break Y make).
        if old_pin is not None and new_pin is not None:
            await asyncio.sleep(self._dead_time)

        # 3. Make: cerrar el relé de la antena destino.
        if new_pin is not None and dest is not None:
            # Último cinturón, independiente de _validate: no cerrar nunca un
            # relé si el de la misma antena para la otra radio está cerrado.
            sibling = self._antennas[dest].pin(radio.other)
            if self._hw.read_relay(sibling):
                raise _SiblingClosed(
                    f"el relé GPIO{sibling} de {radio.other.value} está cerrado"
                )
            self._hw.set_relay(new_pin, True)
            self._assign[radio] = dest

        # 4. Verificar el estado real de los 12 GPIO.
        bad = self._verify()
        if bad:
            raise _VerifyFailed(bad, "después de la maniobra")

    def _enter_fault(
        self,
        code: str,
        detail: str,
        *,
        actor: str,
        radio: Radio | None = None,
        prev: int | None = None,
        dest: int | None = None,
        ptt_state: str | None = None,
    ) -> None:
        failed = self._open_all()
        self._assign = {Radio.R1: None, Radio.R2: None}
        self._fault = code
        fields: dict[str, Any] = dict(
            actor=actor,
            radio=radio.value if radio else None,
            ant_from=prev,
            ant_to=dest,
            ptt_state=ptt_state,
            result="ERROR",
            detail=detail,
            write_failed=failed,
        )
        if code == "RELAY_MISMATCH":
            self._audit.record("RELAY_MISMATCH", **fields)
        self._audit.record("FAULT", code=code, **fields)
        self._touch()
        self._publish()

    def _audit_denied(
        self,
        exc: SwitchError,
        radio: Radio,
        prev: int | None,
        dest: int | None,
        actor: str,
    ) -> None:
        event = {
            "ANTENNA_IN_USE": "COLLISION_BLOCKED",
            "TX_ACTIVE": "TX_ACTIVE_BLOCKED",
        }.get(exc.code, "SWITCH_DENIED")
        self._audit.record(
            event,
            actor=actor,
            radio=radio.value,
            ant_from=prev,
            ant_to=dest,
            result="DENIED",
            code=exc.code,
            reason=exc.message,
        )

    async def _release_all_locked(self, actor: str, reason: str) -> dict[str, Any]:
        async with self._lock:
            self._require_started()
            failed = self._open_all()
            self._assign = {Radio.R1: None, Radio.R2: None}
            bad = self._verify()
            ok = not failed and not bad
            if ok:
                if self._fault:
                    self._audit.record(
                        "FAULT_CLEARED", actor=actor, previous=self._fault, result="OK"
                    )
                self._fault = None
            else:
                self._fault = "RELAY_MISMATCH"
                self._audit.record(
                    "FAULT",
                    code="RELAY_MISMATCH",
                    actor=actor,
                    detail="no se pudo llegar al estado seguro",
                    write_failed=failed,
                    mismatched=bad,
                    result="ERROR",
                )
            self._audit.record(
                "EMERGENCY_RELEASE",
                actor=actor,
                reason=reason,
                result="OK" if ok else "ERROR",
            )
            self._touch()
            self._publish()
            if not ok:
                raise SwitchError(
                    "RELEASE_FAILED",
                    "No se pudo abrir todos los relés (relé pegado o fallo de GPIO): "
                    f"pines {sorted(set(failed) | set(bad))}. Requiere revisión física.",
                    503,
                )
            return self.snapshot()
