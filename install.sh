#!/usr/bin/env bash
# Instala antenna-switchd (Fase 1) en una Raspberry Pi.
#
# NO arranca ni activa el servicio: con relés reales conectados, la primera puesta
# en marcha se hace a mano y con los pasos previos del README (parar Apache y
# gpio-reset de v1, fijar el estado seguro en config.txt, comprobar polaridad).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Ejecuta con sudo: sudo ./install.sh" >&2
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/antenna-switch
CONF=/etc/antenna
SVC_USER=antenna

echo "[1/6] Dependencias del sistema"
apt-get update -qq
apt-get install -y python3 python3-venv pigpio

echo "[2/6] Usuario de servicio '$SVC_USER'"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
fi

echo "[3/6] Código en $APP"
install -d -m 0755 "$APP"
rm -rf "$APP/server" "$APP/web"
cp -r "$SRC/server" "$SRC/web" "$APP/"
rm -rf "$APP/server/tests"
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
install -m 0644 "$SRC/requirements.txt" "$APP/requirements.txt"
chown -R root:root "$APP"

echo "[4/6] Entorno virtual de Python"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"

echo "[5/6] Configuración en $CONF"
install -d -m 0755 "$CONF"
if [[ ! -f "$CONF/antenna.toml" ]]; then
  install -m 0644 "$SRC/config/antenna.toml.example" "$CONF/antenna.toml"
  echo "      creado $CONF/antenna.toml: revisa pines y polaridad antes de arrancar"
else
  echo "      $CONF/antenna.toml ya existe: no se toca"
fi

echo "[6/6] Servicio systemd (sin activar)"
install -m 0644 "$SRC/config/antenna-switchd.service" /etc/systemd/system/antenna-switchd.service
systemctl daemon-reload
systemctl enable --now pigpiod
sleep 1
# pigpiod da control total de los GPIO a quien conecte con él: debe escuchar solo en local.
if ss -ltn 2>/dev/null | awk '$4 ~ /:8888$/ && $4 !~ /^(127\.0\.0\.1|\[::1\]):8888$/ { f = 1 } END { exit !f }'; then
  echo "AVISO: pigpiod escucha en una interfaz que no es local. Arráncalo con la opción -l" >&2
  echo "       (ExecStart=/usr/bin/pigpiod -l) o cualquier equipo de la LAN podrá mover los relés." >&2
fi

cat <<EOF

Instalación terminada. El servicio NO está activado todavía.

Antes de la primera prueba con relés reales:
  1. Para la v1:   sudo systemctl disable --now apache2 gpio-reset.service
  2. Fija el estado seguro de arranque en /boot/firmware/config.txt (ver README).
  3. Revisa $CONF/antenna.toml (pines, polaridad active_high, backend = "pigpio").
  4. Prueba primero sin hardware:  $APP/venv/bin/python -m server --simulate \\
         --config $CONF/antenna.toml
  5. Cuando todo esté verificado:  sudo systemctl enable --now antenna-switchd
EOF
