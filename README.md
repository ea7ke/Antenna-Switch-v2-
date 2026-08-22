# Antenna-Switch-v2 — Arquitectura

Sistema de control de conmutador de antenas **6×2** (6 antenas, 2 estaciones) sobre Raspberry Pi,
instalado en azotea junto a las antenas y accesible por Ethernet desde la sede del club.

Estaciones: **Icom IC-7610** (puesto 1) y **Kenwood TS-950** (puesto 2).

---

## 0. Qué cambia respecto a v1 y por qué

| v1 (`antenna-switch-control`) | v2 | Motivo |
|---|---|---|
| Apache + PHP + CGI `.cgi` | Servicio único Python (FastAPI + uvicorn) | El CGI arranca un proceso por petición → **no hay forma de serializar** dos conmutaciones simultáneas. Con dos operadores esto es un fallo de seguridad, no de estilo. |
| Sin autenticación | Usuarios, roles, sesiones, TOTP opcional | Requisito. |
| Sin estado persistente | SQLite (WAL) + reconciliación al arranque | Saber qué antena tiene cada radio tras un corte. |
| Sin log | Auditoría append-only + log de sesiones | Requisito. |
| GPIO tocado desde varios sitios | **Un solo proceso propietario del hardware** | Invariante de exclusividad garantizada por diseño. |
| Polling desde el navegador | WebSocket | Cada operador ve en tiempo real lo que hace el otro. |
| HTTP plano | TLS (nginx delante) | La red de la sede no es de confianza. |
| Sin interlock de PTT | Interlock por CAT desde el PC del operador | Evitar conmutar con RF presente. |

**El hardware no se toca.** Se mantienen los dos bancos de relés y el mapa GPIO existente.

---

## 1. Modelo de seguridad (lo primero, porque condiciona todo lo demás)

Hay tres riesgos físicos reales. El software debe hacerlos **imposibles**, no "improbables".

### R1 — Colisión: las dos radios en la misma antena
La salida de PA de una radio entra directamente en el front-end de la otra. Destruye receptores.

**Mitigación:** invariante en el servidor
```
assign[RADIO_1] != assign[RADIO_2]   salvo que ambas sean NONE
```
validado dentro de un lock global antes de tocar ningún relé. No se valida en el navegador —
el navegador solo *muestra* el estado. Una petición que viole la invariante se rechaza con 409
y **se registra en el log** (intento de colisión = evento auditable).

### R2 — Hot switching: conmutar con PTT activo
Arco en los contactos, contactos pegados, ROE infinita, PA dañado.

**Mitigación:** interlock por CAT (§4). Sin confirmación RX válida no hay conmutación.

### R3 — Estado indeterminado al arranque
Si los GPIO glitchean al bootear, los relés chasquean con las radios conectadas.

**Mitigación:** fijar el estado de los pines **en el firmware**, antes de que arranque Linux.
En `/boot/firmware/config.txt`:
```
# Estado seguro de los 12 relés desde el primer instante del arranque.
# Usar 'dh' si los módulos son ACTIVO-BAJO, 'dl' si son ACTIVO-ALTO.
gpio=2,3,4,5,6,7,8,9,10,11,12,13=op,dh
dtparam=watchdog=on
```

> **Punto a verificar en el montaje real:** los GPIO **2, 3, 7 y 8** no son pines neutros.
> BCM2/BCM3 llevan pull-ups de 1,8 kΩ soldadas en la placa (bus I2C) y BCM7/BCM8 son SPI CE1/CE0,
> que el firmware deja **en alto** durante el arranque. Si los módulos de relé son *activo-alto*,
> cuatro relés se activan solos durante unos segundos en cada arranque. Comprobar la polaridad de
> los módulos antes de nada; si son activo-alto, la línea `gpio=` de arriba lo corrige, pero
> conviene documentarlo en el README porque una reinstalación limpia sin ese ajuste reintroduce el fallo.

### Estado seguro
El estado seguro es **NONE en ambas radios** (todos los relés abiertos, radios sin antena).
Es preferible dejar una estación sin antena que dejar dos conectadas entre sí.

---

## 2. Arquitectura de componentes

```
  AZOTEA                                    SEDE
  ┌──────────────────────────────┐          ┌────────────────────────────┐
  │ Raspberry Pi                 │          │ PC puesto 1 (IC-7610)      │
  │                              │          │  ├ navegador → UI web      │
  │  nginx :443 (TLS)            │◄── LAN ──┤  └ ptt-agent (CI-V/USB)    │
  │    ├ estáticos (SPA)         │          └────────────────────────────┘
  │    └ proxy → :8000           │          ┌────────────────────────────┐
  │                              │          │ PC puesto 2 (TS-950)       │
  │  antenna-switchd (systemd)   │◄── LAN ──┤  ├ navegador → UI web      │
  │    ├ API REST + WebSocket    │          │  └ ptt-agent (IF-232C)     │
  │    ├ AuthN/AuthZ + sesiones  │          └────────────────────────────┘
  │    ├ máquina de estados      │
  │    │   (lock global, único   │
  │    │    dueño del GPIO)      │
  │    ├ SQLite WAL (/var/lib)   │
  │    └ pigpio → 12 relés       │
  │                              │
  │  hardware watchdog           │
  └──────────────────────────────┘
```

**Regla dura:** `antenna-switchd` es el **único** proceso que abre los GPIO. Ni scripts, ni CGI,
ni cron. Todo lo demás pasa por su API.

### Stack
- **Python 3.11 + FastAPI + uvicorn**, un solo worker (crítico: varios workers = varios dueños del GPIO).
- **pigpio** (ya instalado en v1) o **lgpio** si se pasa a Bookworm. Se mantiene pigpio para no romper nada.
- **SQLite** con WAL, `synchronous=FULL` en las escrituras de auditoría.
- **nginx** solo como terminador TLS y servidor de estáticos.
- **Frontend**: SPA sin framework pesado (HTML + JS vanilla o Preact). Debe funcionar en un PC viejo
  del club y en el móvil desde la LAN.
- Servicio corriendo como usuario **`antenna`** (no root), en el grupo `gpio`, con
  `ProtectSystem=strict`, `PrivateTmp=yes`, `NoNewPrivileges=yes`, `ReadWritePaths=/var/lib/antenna`.

---

## 3. Máquina de estados y secuencia de conmutación

Estado: `assign[radio] ∈ {NONE, ANT1..ANT6}` + `mode[radio] ∈ {MANUAL, AUTO}`.

Secuencia de `switch(radio, antena_destino)`, íntegramente bajo `asyncio.Lock` global:

1. **AuthZ** — sesión válida, rol con permiso, radio permitida para ese usuario.
2. **Antena disponible** — no deshabilitada por mantenimiento, no asignada a la otra radio.
3. **Interlock PTT** — heartbeat del agente < 3 s **y** estado `RX`. Si no → rechazo (`428 PTT_UNKNOWN` / `409 TX_ACTIVE`).
4. **Inhibir TX** — se pide al agente que fuerce RX y bloquee el PTT durante la maniobra.
5. **Break** — abrir el relé actual de esa radio.
6. **Dead time** — 150 ms (ajustable). Break-before-make estricto.
7. **Make** — cerrar el relé de la antena destino.
8. **Verificar** — releer el estado de los 12 GPIO y comprobar que coincide con el esperado.
   Si no coincide → **abrir todo**, marcar `FAULT`, alertar.
9. **Persistir** — estado + entrada de auditoría en la misma transacción.
10. **Liberar PTT** y **difundir** el nuevo estado por WebSocket a todos los clientes.

Timeout global de la maniobra: 2 s. Si expira, estado seguro (todo abierto) y `FAULT`.

**Robo de antena:** prohibido por defecto. Un `admin` puede forzar la liberación de una antena
ocupada por la otra radio; queda registrado como `FORCED_RELEASE` con motivo obligatorio.

---

## 4. Interlock de PTT (`ptt-agent`)

Agente ligero en el PC de cada operador (Python, empaquetado como .exe con PyInstaller para Windows).
Se conecta por WebSocket a la Pi, se autentica con un token de dispositivo y publica estado.

- **IC-7610** — CI-V. Poll de estado TX con `1C 00`; forzar RX con `1C 00 00`. Alternativa mejor si
  el USB de la radio está libre: leer el bit de TX del subcomando de transceive.
- **TS-950** — protocolo Kenwood por el IF-232C. `IF;` devuelve una cadena de estado cuyo campo de
  TX/RX indica si está transmitiendo; `RX;` fuerza recepción. **Verificar que ese TS-950 tiene la
  interfaz IF-232C instalada** — muchos TS-950SD salieron sin ella.

**Fail-safe si no hay agente** (radio sin CAT, PC apagado, agente caído). Configurable por radio:
- `STRICT` (por defecto): sin heartbeat no hay conmutación.
- `CONFIRM`: se permite, pero la UI exige marcar "confirmo que no estoy transmitiendo" y el evento
  se registra con `ptt_state = UNKNOWN`. Es el modo realista si el TS-950 se queda sin CAT.

El agente también gestiona el bloqueo del PTT durante la maniobra: mientras dura, el operador no
puede transmitir aunque pise el pedal.

---

## 5. Usuarios, roles y sesiones

### Roles
| Rol | Puede |
|---|---|
| `viewer` | Ver estado y log propio. Nada más. |
| `operator` | Conmutar la radio que tenga asignada o cualquiera libre. Ver log completo. |
| `admin` | Todo + gestión de usuarios, mantenimiento de antenas, forzar liberación, exportar log. |

Además, `operator` puede tener `allowed_radios` restringido (útil si un socio solo usa un puesto).

### Autenticación
- Contraseñas con **Argon2id** (`argon2-cffi`, parámetros: 64 MiB, t=3, p=2 — cómodos para una Pi 3).
- Sesión en cookie **`HttpOnly` + `Secure` + `SameSite=Strict`**, token opaco de 256 bits en BD,
  expiración por inactividad (8 h) y absoluta (24 h).
- **CSRF**: token por sesión, obligatorio en todo `POST`/`DELETE` (doble comprobación con SameSite).
- **Rate limiting**: 5 intentos por usuario/IP en 15 min → bloqueo temporal exponencial. Todo intento
  fallido se registra.
- **TOTP obligatorio para `admin`** (pyotp). Opcional para el resto.
- Primer arranque: se genera un `admin` con contraseña aleatoria impresa en el log de instalación,
  y se **fuerza el cambio** en el primer login.

### TLS
Certificado de una CA propia del club (script `make-ca.sh` incluido) o Let's Encrypt vía DNS-01 si
hay dominio. HSTS activo. **Nunca** exponer el puerto a Internet: si hace falta acceso remoto,
WireGuard o Tailscale hasta la LAN.

---

## 6. Esquema de base de datos

```sql
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  callsign      TEXT NOT NULL UNIQUE,      -- indicativo como nombre de usuario
  display_name  TEXT NOT NULL,
  pwd_hash      TEXT NOT NULL,             -- argon2id
  role          TEXT NOT NULL CHECK (role IN ('viewer','operator','admin')),
  allowed_radios TEXT NOT NULL DEFAULT '*',
  totp_secret   TEXT,
  must_change_pwd INTEGER NOT NULL DEFAULT 0,
  disabled      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  last_login_at TEXT
);

CREATE TABLE sessions (
  token_hash  TEXT PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  ip          TEXT NOT NULL,
  user_agent  TEXT,
  created_at  TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  revoked_at  TEXT
);

CREATE TABLE antennas (
  id          INTEGER PRIMARY KEY,          -- 1..6
  name        TEXT NOT NULL,                -- "10m Yagi"
  bands       TEXT NOT NULL,                -- "28000-29700" (rangos kHz, coma-separados)
  gpio_r1     INTEGER NOT NULL,
  gpio_r2     INTEGER NOT NULL,
  maintenance INTEGER NOT NULL DEFAULT 0,
  notes       TEXT,
  cycles_r1   INTEGER NOT NULL DEFAULT 0,   -- contador de vida de contactos
  cycles_r2   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE state (                        -- una fila por radio, siempre presente
  radio       TEXT PRIMARY KEY,             -- 'R1' | 'R2'
  antenna_id  INTEGER REFERENCES antennas(id),  -- NULL = NONE
  mode        TEXT NOT NULL DEFAULT 'MANUAL',
  updated_at  TEXT NOT NULL,
  updated_by  INTEGER REFERENCES users(id)
);

CREATE TABLE audit_log (                    -- append-only, nunca UPDATE ni DELETE
  id          INTEGER PRIMARY KEY,
  ts_utc      TEXT NOT NULL,
  event       TEXT NOT NULL,   -- LOGIN_OK, LOGIN_FAIL, LOGOUT, SWITCH_OK, SWITCH_DENIED,
                               -- COLLISION_BLOCKED, TX_ACTIVE_BLOCKED, FORCED_RELEASE,
                               -- FAULT, RELAY_MISMATCH, USER_CREATED, CONFIG_CHANGED,
                               -- BOOT, WATCHDOG_RESET
  user_id     INTEGER REFERENCES users(id),
  callsign    TEXT,            -- desnormalizado: el log sobrevive al borrado del usuario
  ip          TEXT,
  radio       TEXT,
  ant_from    INTEGER,
  ant_to      INTEGER,
  ptt_state   TEXT,            -- RX | TX | UNKNOWN
  result      TEXT NOT NULL,   -- OK | DENIED | ERROR
  reason      TEXT,
  latency_ms  INTEGER,
  detail_json TEXT
);
CREATE INDEX idx_audit_ts ON audit_log(ts_utc);
CREATE INDEX idx_audit_user ON audit_log(user_id, ts_utc);
```

Timestamps siempre en **UTC ISO-8601**; la UI los convierte a hora local. La Pi debe tener NTP
funcionando: un log con hora incorrecta no sirve para nada.

**Retención:** sin borrado automático. El log completo de un club cabe de sobra en la SD durante
años (≈300 bytes/evento). Exportación CSV desde el panel de admin, y volcado semanal opcional a
un recurso de red.

---

## 7. API

```
POST   /api/auth/login              {callsign, password, totp?}  → cookie de sesión
POST   /api/auth/logout
GET    /api/auth/me

GET    /api/state                   → estado completo (asignaciones, modos, relés, salud)
POST   /api/switch                  {radio, antenna_id|null, confirm_no_tx?}
POST   /api/emergency/release-all   (admin) → estado seguro

GET    /api/antennas
PATCH  /api/antennas/{id}           (admin) nombre, bandas, mantenimiento

GET    /api/users                   (admin)
POST   /api/users                   (admin)
PATCH  /api/users/{id}              (admin)
POST   /api/users/me/password

GET    /api/log?from&to&user&event&limit   → auditoría filtrable
GET    /api/log.csv                        (admin) exportación

GET    /api/health                  → uptime, temp CPU, estado GPIO, agentes conectados
WS     /ws                          → push de estado, eventos y avisos
WS     /ws/agent                    → canal del ptt-agent (token de dispositivo)
```

Todas las mutaciones son **idempotentes por `Idempotency-Key`** para que un doble clic o una
reconexión no genere dos maniobras.

---

## 8. Funciones nuevas

**Seguimiento de banda por CAT (`AUTO`).**
El `ptt-agent` ya está leyendo la radio: publica también la frecuencia. En modo `AUTO` el servidor
selecciona la antena adecuada al cambiar de banda, respetando exclusividad y PTT. En modo `MANUAL`
solo **avisa** ("estás en 40 m con la Yagi de 10 m") con un banner rojo en la UI. Esta es
probablemente la mejora de mayor valor práctico del proyecto.

**Mantenimiento de antenas.** Marcar una antena como fuera de servicio con un motivo; desaparece de
la selección para todos y aparece en gris con el motivo visible.

**Panel de estado compartido.** Los dos operadores ven la misma pantalla en tiempo real: quién tiene
qué antena, quién está en TX, qué antenas quedan libres.

**Salud y telemetría.** Temperatura de CPU, uptime, tensión (`vcgencmd get_throttled` — la
subtensión es la causa número uno de rarezas en una Pi de azotea), estado real de los 12 GPIO,
contador de ciclos por relé (los módulos chinos rondan las 100 000 maniobras) y última conmutación.

**Alertas.** Telegram o correo ante `FAULT`, `RELAY_MISMATCH`, `COLLISION_BLOCKED`, ráfaga de
`LOGIN_FAIL` o reinicio inesperado. Estando el equipo en la azotea, enterarse a tiempo importa.

**Parada de emergencia.** Botón que lleva todo a estado seguro. Registrado.

---

## 9. Fiabilidad de un equipo al que no se puede subir

- **Hardware watchdog** de la Pi activado + `WatchdogSec=30` en la unidad systemd.
- `Restart=always`, `RestartSec=5`.
- **Raíz en solo lectura** (overlayfs) con `/var/lib/antenna` en una partición aparte montada
  `rw,sync`. Es lo que evita que un corte de luz corrompa la SD. Alternativa mínima si no quieres
  overlay: `commit=1` y `noatime`.
- Journal de systemd limitado (`SystemMaxUse=50M`) para no llenar la tarjeta.
- **SSH accesible siempre**, con clave pública y `PasswordAuthentication no`. Es la única vía de
  rescate sin escalera.
- Reconciliación al arranque: leer `state`, comparar con los GPIO, y si hay discrepancia **ir a
  estado seguro** y registrar `BOOT` + `RELAY_MISMATCH`. Nunca restaurar a ciegas la última
  asignación: al arrancar no sabes si alguien tiene el pedal pisado.
- Copia de la BD a la LAN cada noche (rsync a un NAS o al PC del club).

---

## 10. Estructura del repositorio

```
Antenna-Switch-v2/
├── ARCHITECTURE.md
├── README.md
├── install.sh / uninstall.sh
├── config/
│   ├── antenna.toml.example        # pines, polaridad, dead time, modos fail-safe
│   ├── nginx-antenna.conf
│   └── antenna-switchd.service
├── server/
│   ├── main.py                     # FastAPI, rutas, WebSocket
│   ├── hardware.py                 # capa GPIO (única), abstracta y simulable
│   ├── statemachine.py             # invariantes, lock, secuencia de conmutación
│   ├── auth.py                     # argon2, sesiones, CSRF, TOTP, rate limit
│   ├── audit.py                    # escritura del log
│   ├── db.py / schema.sql
│   └── tests/                      # con hardware.py simulado
├── agent/
│   ├── ptt_agent.py                # CI-V (IC-7610) + Kenwood (TS-950)
│   └── build-windows.ps1
└── web/
    ├── index.html / app.js / style.css
    └── admin.html
```

`hardware.py` con backend simulado permite ejecutar **toda** la lógica y los tests en un PC, sin
tocar la Pi. Con el equipo en la azotea, esto no es un lujo: es la diferencia entre depurar en tu
mesa o depurar en el tejado.

---

## 11. Plan por fases

| Fase | Contenido | Entregable |
|---|---|---|
| 1 | Servicio + máquina de estados + exclusividad + GPIO + estado seguro + UI mínima | Sustituye a v1 con las garantías físicas resueltas |
| 2 | Usuarios, roles, sesiones, TLS, auditoría completa, panel de log | Cumple el requisito planteado |
| 3 | `ptt-agent` para el IC-7610 e interlock real | Elimina el hot switching en el puesto 1 |
| 4 | Agente del TS-950 (si tiene IF-232C), modo AUTO por banda, alertas, telemetría | Funcionalidad ampliada |
| 5 | Endurecimiento: raíz RO, watchdog, backups, detección de relé pegado por realimentación | Operación desatendida |

Las fases 1 y 2 se pueden desplegar en paralelo a v1 (puerto distinto, mismo GPIO **no** —
hay que parar Apache antes de la primera prueba real con relés).

---

## 12. Supuestos a confirmar antes de escribir código

1. **Polaridad de los módulos de relé**: ¿activo-alto o activo-bajo? Determina la línea `gpio=` de
   `config.txt` y el `active_high` de la capa GPIO. Un error aquí activa relés al arrancar.
2. **El conmutador 6×2 en sí**: ¿tiene interlock propio (imposibilidad mecánica/eléctrica de que dos
   radios compartan antena) o la exclusividad depende **solo** del software? Si es lo segundo, el §1
   pasa de importante a crítico.
3. **Modelo exacto de Raspberry Pi** y versión de Raspberry Pi OS (pigpio vs lgpio en Bookworm).
4. **¿Sigue habiendo botones físicos en GPIO?** En la azotea no tienen uso; si están, sus pines
   quedan libres y sirven como realimentación de estado de relés (fase 5).
5. **¿Tiene el TS-950 la interfaz IF-232C instalada?** Determina si el puesto 2 puede tener
   interlock real o se queda en modo `CONFIRM`.
6. **Alimentación de la Pi**: fuente y longitud de cable. La subtensión en instalaciones de azotea
   es la causa habitual de corrupciones de SD y cuelgues.

