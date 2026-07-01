# install.sh — instalador de gp-monitor en Linux/macOS (systemd).
# Crea un usuario de sistema, copia el código y registra un servicio systemd.

#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="gp-monitor"
SERVICE_USER="gp-monitor"
INSTALL_DIR="/opt/${SERVICE_NAME}"
STATE_DIR="/var/lib/${SERVICE_NAME}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$(id -u)" -ne 0 ]; then
    echo "[ERROR] Este script debe ejecutarse como root (sudo $0)"
    exit 1
fi

echo "=== gp-monitor: instalador Linux ==="

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 no encontrado"
    exit 1
fi

# Usuario de sistema
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

# Copiar código
mkdir -p "${INSTALL_DIR}"
cp -r . "${INSTALL_DIR}/"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

# Estado
mkdir -p "${STATE_DIR}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}"

# Dependencias
sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"

# Config
if [ ! -f "${INSTALL_DIR}/config/config.yaml" ]; then
    cp "${INSTALL_DIR}/config/config.example.yaml" "${INSTALL_DIR}/config/config.yaml"
    echo "[INFO] Edita ${INSTALL_DIR}/config/config.yaml antes de arrancar."
fi

# systemd unit
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=gp-monitor agent (métricas a gp-it)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python -m gp_monitor run
Restart=always
RestartSec=10
StateDirectory=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl start "${SERVICE_NAME}.service"

echo
echo "=== Listo ==="
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo systemctl stop ${SERVICE_NAME}"
echo "  sudo journalctl -u ${SERVICE_NAME} -f"