"""Punto de entrada: ``python -m server --config /etc/antenna/antenna.toml``."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys

from .config import ConfigError, load_config

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server", description=__doc__)
    parser.add_argument(
        "--config",
        default=os.environ.get("ANTENNA_CONFIG", "/etc/antenna/antenna.toml"),
        help="ruta de antenna.toml (por defecto /etc/antenna/antenna.toml)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="usar el hardware simulado aunque la configuración diga pigpio "
        "(desarrollo en un PC; no mueve ningún relé)",
    )
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2

    if args.simulate:
        config = dataclasses.replace(config, backend="simulated")

    if config.host not in LOOPBACK:
        logging.getLogger("antenna.app").warning(
            "Escuchando en %s SIN autenticación ni TLS (Fase 1). Restringe el "
            "acceso con cortafuegos a la LAN del club.",
            config.host,
        )

    import uvicorn

    from .main import create_app

    # workers=1 es obligatorio: un único proceso dueño del GPIO.
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        workers=1,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
