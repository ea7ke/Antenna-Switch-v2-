# Antenna-Switch-v2

Control de un conmutador de antenas **6×2** (6 antenas, 2 radios) con una Raspberry Pi.
Evolución de [`antenna-switch-control`](https://github.com/ea7ke/antenna-switch-control) (v1, la
versión publicada en la revista, que se mantiene tal cual).

El diseño completo, el modelo de seguridad y el plan por fases están en
[`ARCHITECTURE.md`](ARCHITECTURE.md). Este README cubre lo que hay hoy y cómo usarlo.

## Estado: Fase 1

| Hecho en la Fase 1 | Pendiente (fases siguientes) |
|---|---|
| Servicio único dueño del GPIO (FastAPI + uvicorn, un solo worker) | Usuarios, roles, sesiones, TLS (Fase 2) |
| Exclusividad entre radios garantizada en el servidor, con lock global | Base de datos y log de auditoría persistente (Fase 2) |
| Break-before-make con tiempo muerto y verificación de los GPIO tras cada maniobra | Interlock de PTT por CAT: `ptt-agent` (Fases 3 y 4) |
| Estado seguro (todo abierto) en arranque, parada, timeout y fallo | Modo AUTO por banda, alertas, telemetría (Fase 4) |
| Polaridad de los relés configurable por software | Raíz en solo lectura, backups (Fase 5) |
| Mismo mapa de pines que v1: **el hardware no se toca** | |
| Interfaz web en tiempo real (WebSocket) y simulador sin hardware | |

### Cómo se garantiza la exclusividad

El conmutador no tiene interlock hardware, así que toda la protección es de software:

1. `assign[R1] != assign[R2]` se valida dentro de un lock global **antes** de mover ningún relé.
2. Antes de cada maniobra se comprueba que el estado real de los 12 GPIO es el esperado.
3. Un segundo control, independiente del primero, se niega a cerrar un relé si el de la misma
   antena para la otra radio está cerrado.
4. Tras la maniobra se relee todo; si algo no coincide, se abre todo y el sistema pasa a `FAULT`.
5. La maniobra corre en una tarea protegida frente a cancelación y con un timeout global.
6. Al arrancar no se restaura nunca la última asignación: se parte de «ninguna antena».

## Probarlo en un PC, sin hardware

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config/antenna.toml.example antenna.toml
python -m server --simulate --config antenna.toml
# abrir http://127.0.0.1:8000
```

`--simulate` usa el backend simulado: no se mueve ningún relé real.

### Tests

```bash
python -m unittest discover -s server/tests -t .
```

Los tests de la máquina de estados, el hardware y la configuración solo usan la biblioteca
estándar. Los de la API se omiten salvo que esté instalado `requirements-dev.txt` (necesita `httpx`).
Cubren, entre otras cosas, peticiones simultáneas a la misma antena, una prueba aleatoria de
estrés, relés pegados, glitches externos, timeouts y cancelación en mitad de una maniobra.

## Despliegue en la Raspberry Pi

> Hazlo la primera vez **con las radios desconectadas del conmutador** (o apagadas y sin RF).

1. **Estado seguro de arranque.** En `/boot/firmware/config.txt` (en sistemas antiguos,
   `/boot/config.txt`):
   ```
   gpio=2,3,4,5,6,7,8,9,10,11,12,13=op,dl
   dtparam=watchdog=on
   ```
   `dl` (nivel bajo) es correcto para módulos *activo-alto*, que es como trabaja v1. Con módulos
   *activo-bajo* sería `dh`. Reinicia y comprueba con un polímetro que los 12 pines quedan en reposo
   durante todo el arranque: BCM2 a 8 tienen pull-up por defecto y podrían activar relés.
2. **Instalar:** `sudo ./install.sh`. Instala el servicio pero **no lo activa**.
3. **Parar v1** (los dos sistemas no pueden compartir el GPIO):
   ```bash
   sudo systemctl disable --now apache2 gpio-reset.service
   ```
4. **Revisar `/etc/antenna/antenna.toml`:** pines, `active_high` y `backend = "pigpio"`.
5. **Comprobar `pigpiod`:** debe escuchar solo en local (`ss -ltn | grep 8888` → `127.0.0.1`).
   Si escucha en todas las interfaces, cualquier equipo de la red podría mover los relés.
6. **Primera prueba real:** `sudo systemctl start antenna-switchd`, y vigilar
   `journalctl -u antenna-switchd -f`. Cambia de antena y comprueba con el polímetro (o
   escuchando los relés) que se mueve **un solo** relé por radio.
7. **Activar al arrancar:** `sudo systemctl enable antenna-switchd`.

Para volver a v1: `sudo systemctl disable --now antenna-switchd` y
`sudo systemctl enable --now apache2 gpio-reset.service`.

### Acceso a la interfaz

Por defecto el servicio escucha solo en `127.0.0.1:8000`. Para usarlo desde otro equipo, lo más
seguro en la Fase 1 es un túnel SSH:

```bash
ssh -L 8000:127.0.0.1:8000 pi@<IP-de-la-Pi>   # y abrir http://127.0.0.1:8000
```

Si prefieres abrirlo a la red del club, cambia `host` en `antenna.toml` y restringe el acceso con
cortafuegos: **la Fase 1 no tiene autenticación ni TLS.** Nunca lo expongas a Internet.

## Límites conocidos de la Fase 1

- **Sin interlock de PTT.** El servidor no sabe si la radio está transmitiendo; conmutar con RF
  presente puede dañar contactos y PA. Hasta la Fase 3 es responsabilidad del operador.
- **Sin autenticación.** Cualquiera con acceso a la red puede conmutar. La API exige la cabecera
  `X-Antenna-Client` en las operaciones que cambian algo, lo que impide que otra página web las
  lance desde el navegador de un operador, pero no sustituye a un login.
- **Auditoría solo en el log.** Los eventos salen como líneas JSON:
  `journalctl -u antenna-switchd | grep antenna.audit`.
- **Al reiniciar el servicio se sueltan las antenas** (estado seguro). Es deliberado.
- **La capa FastAPI no se ha ejecutado contra FastAPI real** en el entorno donde se escribió (sin
  acceso a PyPI); sí contra un sustituto mínimo sobre Starlette. La primera vez que instales
  `requirements-dev.txt`, ejecuta los tests: el módulo `test_api.py` dejará de omitirse.

## Mapa de pines (BCM)

Idéntico al de v1 (`etc/gpio_config.sh`):

| Antena | Relé R1 | Relé R2 |
|---|---|---|
| 10m Yagi | 2 | 8 |
| 15m Yagi | 3 | 9 |
| 20m Yagi | 4 | 10 |
| 40m Dipolo | 5 | 11 |
| 80m Dipolo | 6 | 12 |
| 160m L Inv | 7 | 13 |

## Estructura

```
config/    antenna.toml.example, antenna-switchd.service
server/    config, hardware (GPIO), statemachine, audit, sdnotify, main (API), tests/
web/       interfaz (HTML + JS sin frameworks)
install.sh / uninstall.sh
ARCHITECTURE.md   diseño completo y plan por fases
```

`server/hardware.py` es el **único** sitio que toca los GPIO, y `hardware.SimulatedHardware`
permite ejecutar y probar toda la lógica en un PC.

## Créditos

Proyecto de EA7KE.
