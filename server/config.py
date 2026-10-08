"""Carga y validación estricta de antenna.toml.

La validación es deliberadamente estricta: una errata en la polaridad o en un
pin no debe arrancar el servicio "a medias", porque el resultado puede ser un
relé cerrado que no toca.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

BACKENDS = ("pigpio", "simulated")
GPIO_MIN, GPIO_MAX = 2, 27  # BCM 0 y 1 están reservados a la EEPROM del HAT


class ConfigError(ValueError):
    """La configuración no es válida."""


@dataclass(frozen=True)
class AntennaCfg:
    id: int
    name: str
    gpio_r1: int
    gpio_r2: int
    bands: str = ""
    notes: str = ""
    maintenance: bool = False


@dataclass(frozen=True)
class Config:
    antennas: tuple[AntennaCfg, ...]
    backend: str = "pigpio"
    active_high: bool = True
    dead_time_ms: int = 150
    maneuver_timeout_ms: int = 2000
    host: str = "127.0.0.1"
    port: int = 8000
    radio_labels: dict[str, str] = field(
        default_factory=lambda: {"R1": "Radio 1", "R2": "Radio 2"}
    )

    @property
    def dead_time_s(self) -> float:
        return self.dead_time_ms / 1000

    @property
    def maneuver_timeout_s(self) -> float:
        return self.maneuver_timeout_ms / 1000

    @property
    def pins(self) -> tuple[int, ...]:
        return tuple(p for a in self.antennas for p in (a.gpio_r1, a.gpio_r2))


# --------------------------------------------------------------------------- #
# Helpers de validación
# --------------------------------------------------------------------------- #

def _int(value: Any, name: str, lo: int | None = None, hi: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name}: debe ser un número entero")
    if lo is not None and value < lo:
        raise ConfigError(f"{name}: debe ser >= {lo} (es {value})")
    if hi is not None and value > hi:
        raise ConfigError(f"{name}: debe ser <= {hi} (es {value})")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name}: debe ser true o false")
    return value


def _str(value: Any, name: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name}: debe ser texto")
    if not allow_empty and not value.strip():
        raise ConfigError(f"{name}: no puede estar vacío")
    return value


def _section(data: dict[str, Any], key: str, allowed: set[str]) -> dict[str, Any]:
    section = data.get(key, {})
    if not isinstance(section, dict):
        raise ConfigError(f"[{key}]: debe ser una tabla")
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(
            f"[{key}]: claves desconocidas {sorted(unknown)} "
            f"(válidas: {sorted(allowed)})"
        )
    return section


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #

def parse_config(data: dict[str, Any]) -> Config:
    unknown_top = set(data) - {"server", "gpio", "radios", "antenna"}
    if unknown_top:
        raise ConfigError(f"secciones desconocidas: {sorted(unknown_top)}")

    server = _section(data, "server", {"host", "port"})
    gpio = _section(
        data, "gpio", {"backend", "active_high", "dead_time_ms", "maneuver_timeout_ms"}
    )
    radios = _section(data, "radios", {"R1", "R2"})

    backend = _str(gpio.get("backend", "pigpio"), "gpio.backend")
    if backend not in BACKENDS:
        raise ConfigError(f"gpio.backend: debe ser uno de {BACKENDS} (es {backend!r})")

    active_high = _bool(gpio.get("active_high", True), "gpio.active_high")
    dead_time_ms = _int(gpio.get("dead_time_ms", 150), "gpio.dead_time_ms", 20, 1000)
    timeout_ms = _int(
        gpio.get("maneuver_timeout_ms", 2000), "gpio.maneuver_timeout_ms", 100, 10000
    )
    if timeout_ms < dead_time_ms + 200:
        raise ConfigError(
            "gpio.maneuver_timeout_ms debe superar a dead_time_ms en al menos 200 ms; "
            "si no, toda maniobra acabaría en FAULT"
        )

    host = _str(server.get("host", "127.0.0.1"), "server.host")
    port = _int(server.get("port", 8000), "server.port", 1, 65535)

    labels = {"R1": "Radio 1", "R2": "Radio 2"}
    for key, value in radios.items():
        labels[key] = _str(value, f"radios.{key}")

    raw_antennas = data.get("antenna")
    if not isinstance(raw_antennas, list) or not raw_antennas:
        raise ConfigError("hace falta al menos una sección [[antenna]]")

    allowed = {"id", "name", "gpio_r1", "gpio_r2", "bands", "notes", "maintenance"}
    antennas: list[AntennaCfg] = []
    for i, raw in enumerate(raw_antennas, start=1):
        where = f"antenna #{i}"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: debe ser una tabla")
        unknown = set(raw) - allowed
        if unknown:
            raise ConfigError(f"{where}: claves desconocidas {sorted(unknown)}")
        for required in ("id", "name", "gpio_r1", "gpio_r2"):
            if required not in raw:
                raise ConfigError(f"{where}: falta '{required}'")
        antennas.append(
            AntennaCfg(
                id=_int(raw["id"], f"{where}.id", 1),
                name=_str(raw["name"], f"{where}.name"),
                gpio_r1=_int(raw["gpio_r1"], f"{where}.gpio_r1", GPIO_MIN, GPIO_MAX),
                gpio_r2=_int(raw["gpio_r2"], f"{where}.gpio_r2", GPIO_MIN, GPIO_MAX),
                bands=_str(raw.get("bands", ""), f"{where}.bands", allow_empty=True),
                notes=_str(raw.get("notes", ""), f"{where}.notes", allow_empty=True),
                maintenance=_bool(raw.get("maintenance", False), f"{where}.maintenance"),
            )
        )

    ids = [a.id for a in antennas]
    if len(set(ids)) != len(ids):
        raise ConfigError("hay ids de antena repetidos")

    pins = [p for a in antennas for p in (a.gpio_r1, a.gpio_r2)]
    repeated = sorted({p for p in pins if pins.count(p) > 1})
    if repeated:
        raise ConfigError(
            f"GPIO repetidos en la configuración: {repeated}. "
            "Cada relé necesita su propio pin; compartirlo conectaría dos "
            "antenas (o dos radios) a la vez."
        )

    return Config(
        antennas=tuple(antennas),
        backend=backend,
        active_high=active_high,
        dead_time_ms=dead_time_ms,
        maneuver_timeout_ms=timeout_ms,
        host=host,
        port=port,
        radio_labels=labels,
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"no existe el fichero de configuración: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML inválido: {exc}") from None
    return parse_config(data)
