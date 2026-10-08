"""API HTTP + WebSocket de antenna-switchd.

IMPORTANTE: este proceso debe ejecutarse con UN SOLO worker. Es el único dueño
del hardware; varios workers serían varios dueños del GPIO y romperían la
exclusividad que garantiza la máquina de estados.

Fase 1: sin usuarios ni TLS (llegan en la Fase 2). Como defensa mínima contra
peticiones cruzadas desde otras páginas web, las operaciones que cambian algo
exigen la cabecera ``X-Antenna-Client``: un navegador no puede añadirla en una
petición entre orígenes sin permiso previo (CORS), y este servicio no lo concede.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import sdnotify
from .audit import AuditSink
from .config import Config
from .hardware import Hardware, PigpioHardware, SimulatedHardware
from .statemachine import Antenna, AntennaSwitch, PttGuard, SwitchError

log = logging.getLogger("antenna.app")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
CLIENT_HEADER = "x-antenna-client"


class SwitchRequest(BaseModel):
    radio: Literal["R1", "R2"]
    antenna_id: int | None = None


def build_hardware(config: Config) -> Hardware:
    if config.backend == "simulated":
        log.warning("Backend SIMULADO: no se mueve ningún relé real")
        return SimulatedHardware(active_high=config.active_high)
    return PigpioHardware(active_high=config.active_high)


def build_switch(
    config: Config,
    hardware: Hardware,
    *,
    audit: AuditSink | None = None,
    ptt_guard: PttGuard | None = None,
) -> AntennaSwitch:
    antennas = [
        Antenna(a.id, a.name, a.gpio_r1, a.gpio_r2, a.bands, a.notes)
        for a in config.antennas
    ]
    maintenance = {
        a.id: "fuera de servicio (configuración)" for a in config.antennas if a.maintenance
    }
    return AntennaSwitch(
        hardware,
        antennas,
        dead_time_s=config.dead_time_s,
        maneuver_timeout_s=config.maneuver_timeout_s,
        audit=audit,
        ptt_guard=ptt_guard,
        maintenance=maintenance,
        radio_labels=config.radio_labels,
    )


def _cpu_temp_c() -> float | None:
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text()
        return round(int(raw.strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


async def require_client_header(request: Request) -> None:
    if request.headers.get(CLIENT_HEADER) is None:
        raise SwitchError(
            "FORBIDDEN",
            f"Falta la cabecera {CLIENT_HEADER}; la petición no procede de la interfaz",
            403,
        )


def _actor(request: Request) -> str:
    host = request.client.host if request.client else "desconocido"
    return f"ip:{host}"


def create_app(
    config: Config,
    *,
    hardware: Hardware | None = None,
    audit: AuditSink | None = None,
    ptt_guard: PttGuard | None = None,
) -> FastAPI:
    started = time.monotonic()

    async def systemd_heartbeat(switch: AntennaSwitch) -> None:
        interval = sdnotify.watchdog_interval()
        if interval is None:
            return
        while True:
            # Si no se puede leer NINGÚN relé (pigpiod caído) no se late:
            # systemd reiniciará el servicio en lugar de dejarlo "vivo" y ciego.
            relays = switch.snapshot()["relays"]
            if any(v is not None for v in relays.values()):
                sdnotify.watchdog()
            await asyncio.sleep(interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hw = hardware or build_hardware(config)
        switch = build_switch(config, hw, audit=audit, ptt_guard=ptt_guard)
        await switch.startup()
        app.state.switch = switch
        heartbeat = asyncio.create_task(systemd_heartbeat(switch))
        sdnotify.ready()
        log.info("antenna-switchd listo en %s:%s", config.host, config.port)
        try:
            yield
        finally:
            sdnotify.stopping()
            heartbeat.cancel()
            await switch.shutdown()

    app = FastAPI(
        title="antenna-switchd",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(SwitchError)
    async def switch_error_handler(request: Request, exc: SwitchError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    def switch_of(request: Request) -> AntennaSwitch:
        return request.app.state.switch

    # ---- lectura ------------------------------------------------------- #

    @app.get("/api/state")
    async def get_state(request: Request) -> dict[str, Any]:
        return switch_of(request).snapshot()

    @app.get("/api/antennas")
    async def get_antennas(request: Request) -> list[dict[str, Any]]:
        return switch_of(request).snapshot()["antennas"]

    @app.get("/api/health")
    async def get_health(request: Request) -> dict[str, Any]:
        snap = switch_of(request).snapshot()
        return {
            "ok": snap["fault"] is None and snap["relays_ok"],
            "fault": snap["fault"],
            "relays_ok": snap["relays_ok"],
            "backend": config.backend,
            "uptime_s": int(time.monotonic() - started),
            "cpu_temp_c": _cpu_temp_c(),
        }

    # ---- mutaciones ---------------------------------------------------- #

    @app.post("/api/switch", dependencies=[Depends(require_client_header)])
    async def post_switch(body: SwitchRequest, request: Request) -> dict[str, Any]:
        return await switch_of(request).switch(
            body.radio, body.antenna_id, actor=_actor(request)
        )

    @app.post(
        "/api/emergency/release-all", dependencies=[Depends(require_client_header)]
    )
    async def post_release_all(request: Request) -> dict[str, Any]:
        return await switch_of(request).release_all(
            actor=_actor(request), reason="emergency"
        )

    # ---- tiempo real --------------------------------------------------- #

    @app.websocket("/ws")
    async def ws_state(ws: WebSocket) -> None:
        await ws.accept()
        switch: AntennaSwitch = ws.app.state.switch
        queue = switch.subscribe()
        incoming = asyncio.ensure_future(ws.receive_text())  # detecta el cierre
        try:
            await ws.send_json({"type": "state", "state": switch.snapshot()})
            while True:
                pending = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait(
                    {pending, incoming}, return_when=asyncio.FIRST_COMPLETED
                )
                if pending in done:
                    await ws.send_json({"type": "state", "state": pending.result()})
                else:
                    pending.cancel()
                if incoming in done:
                    incoming.result()  # lanza WebSocketDisconnect si el cliente cerró
                    incoming = asyncio.ensure_future(ws.receive_text())  # se ignora
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            incoming.cancel()
            switch.unsubscribe(queue)

    # ---- interfaz web (último: no debe tapar /api ni /ws) --------------- #

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

    return app
