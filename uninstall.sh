#!/usr/bin/env bash
# Desinstala antenna-switchd. Conserva /etc/antenna (configuración), /var/lib/antenna
# (estado y, en fases futuras, la base de datos y el log de auditoría) y el usuario.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Ejecuta con sudo: sudo ./uninstall.sh" >&2
  exit 1
fi

systemctl disable --now antenna-switchd 2>/dev/null || true
rm -f /etc/systemd/system/antenna-switchd.service
systemctl daemon-reload
rm -rf /opt/antenna-switch

echo "antenna-switchd desinstalado."
echo "Se conservan /etc/antenna y /var/lib/antenna; bórralos a mano si ya no los necesitas."
echo "Los relés quedan en el último estado: el servicio los abre al apagarse, pero"
echo "comprueba que ninguna antena queda conectada a las dos radios."
