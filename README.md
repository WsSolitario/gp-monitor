# gp-monitor

Agente Python que monitorea servidores y envía métricas básicas
a la plataforma **gp-it** (https://api.dev.gp.ssdevsolutions.com).

```
  ┌──────────────────────┐    HTTPS (POST)    ┌─────────────────────┐
  │  Windows / Linux     │ ─────────────────► │   gp-it API         │
  │  Server (este       │  cada 60 s         │   /api/v1/monitor/  │
  │  agente)             │                    │                      │
  └──────────────────────┘                    └─────────────────────┘
                                                    │
                                                    ▼
                                            ┌─────────────────────┐
                                            │  gp-dash            │
                                            │  /dashboard/monitor │
                                            │  (vista por cluster │
                                            │   tipo Proxmox)     │
                                            └─────────────────────┘
```

- **Conexión 100% saliente.** El servidor donde corre el agente NO abre
  puertos. La API recibe heartbeats en `https://api.dev.gp.ssdevsolutions.com/api/v1/monitor/...`
- **Autenticación del nodo**: UUID + API key (rotada al re-enroll).
- **Multiplataforma** (Windows Server 2019/2022, Linux, macOS) vía `psutil`.

---

## Métricas reportadas

| Métrica        | Tipo      | Descripción                                  |
|----------------|-----------|----------------------------------------------|
| `cpuUsage`     | 0–100 %   | CPU global                                   |
| `memoryUsage`  | 0–100 %   | RAM                                          |
| `diskUsage`    | 0–100 %   | Disco donde reside el sistema                |
| `loadAvg1m/5m/15m` | float | Load average (solo Linux/macOS)               |
| `networkRxBps` | int       | Bytes/seg recibidos                          |
| `networkTxBps` | int       | Bytes/seg enviados                           |
| `uptimeSeconds`| int       | Segundos desde el último boot                |
| `osInfo`       | object    | `{name, version, release, arch}`             |

Heartbeat cada **60 s** (configurable). Cada muestra se guarda en
`monitor_node_heartbeats` con timestamp UTC.

---

## Requisitos

- Python 3.9 o superior
- Windows Server 2019/2022 (target principal) o Linux/macOS
- Salida HTTPS al backend `api.dev.gp.ssdevsolutions.com`

---

## Instalación rápida (Windows Server)

```powershell
# 1. Clonar / copiar el código
cd C:\Tools
git clone <repo> gp-monitor
cd gp-monitor

# 2. Crear entorno virtual e instalar
python -m venv .venv
.\.venv\Scripts\pip install -e .

# 3. Configurar
copy config\config.example.yaml config\config.yaml
notepad config\config.yaml
# Edita: api_url, enrollment_token, agency, etc.

# 4. Instalar como servicio de Windows (auto-arranca con el server)
.\scripts\install.bat
```

Después de instalar, el servicio:

- Arranca automáticamente al iniciar Windows
- Se reinicia solo si falla
- Loguea a `C:\ProgramData\gp-monitor\gp-monitor.log`

### Comandos útiles

```powershell
gp-monitor status       # Estado del servicio
gp-monitor stop         # Detener
gp-monitor start        # Arrancar
gp-monitor uninstall    # Desinstalar

# Probar sin esperar el siguiente ciclo (debug)
python -m gp_monitor heartbeat

# Ver logs en vivo
Get-Content C:\ProgramData\gp-monitor\gp-monitor.log -Wait
```

---

## Instalación rápida (Linux)

```bash
sudo ./scripts/install.sh
sudo systemctl status gp-monitor
sudo journalctl -u gp-monitor -f
```

---

## Configuración (`config/config.yaml`)

```yaml
api_url: "https://api.dev.gp.ssdevsolutions.com"
enrollment_token: "PEGAR_AQUI_MONITOR_ENROLLMENT_TOKEN"

name: "Servidor de Producción"
agency: "Matriz"
environment: "production"
description: ""

heartbeat_interval_seconds: 60
http_timeout_seconds: 15

log_level: "INFO"
log_file: "C:/ProgramData/gp-monitor/gp-monitor.log"
state_dir: "C:/ProgramData/gp-monitor"
```

Equivalente por variables de entorno (override):

```bash
export GP_MONITOR_API_URL=https://api.dev.gp.ssdevsolutions.com
export GP_MONITOR_ENROLLMENT_TOKEN=...
export GP_MONITOR_AGENCY=Matriz
export GP_MONITOR_LOG_LEVEL=DEBUG
```

---

## Flujo de enrollment

1. El operador registra el servidor en **gp-dash → Monitor → Registrar nodo**.
   El backend genera un **UUID** y (si quiere) devuelve la api_key.
2. En el servidor, edita `config.yaml` y pega el UUID (y opcionalmente la api_key).
3. Al arrancar, el agente:
   - Si no tiene api_key → POST `/api/v1/monitor/enroll` con `enrollmentToken` + UUID
     → recibe api_key, la guarda cifrada en `state.json`
   - Si ya tiene api_key → arranca directamente
4. Cada 60s envía POST `/api/v1/monitor/nodes/:uuid/heartbeat` con header
   `x-monitor-api-key: <apiKey>`.

**Importante**: el `enrollment_token` solo se usa una vez por nodo
(para obtener la api_key inicial). Después, la api_key se persiste localmente
en `<state_dir>/state.json` y nunca vuelve a enviarse el enrollment_token.

---

## Endpoints de la API consumidos

| Método | Path                                          | Auth                   |
|--------|-----------------------------------------------|------------------------|
| POST   | `/api/v1/monitor/enroll`                      | `enrollmentToken` body |
| POST   | `/api/v1/monitor/nodes/:uuid/heartbeat`       | `x-monitor-api-key`    |

Documentación de payloads: ver `docs/` (cuando exista).

---

## Estructura del proyecto

```
gp-monitor/
├── pyproject.toml
├── README.md
├── .env.example
├── config/
│   └── config.example.yaml
├── scripts/
│   ├── install.bat           # Windows service install
│   ├── uninstall.bat
│   └── install.sh            # Linux systemd install
├── src/gp_monitor/
│   ├── __init__.py
│   ├── __main__.py           # CLI entry point
│   ├── agent.py              # loop principal
│   ├── api_client.py         # cliente HTTP
│   ├── collectors.py         # métricas con psutil
│   ├── config.py             # carga YAML + ENV
│   ├── state.py              # UUID + api_key persistentes
│   └── windows_service.py    # wrapper SCM (pywin32)
└── tests/
    └── test_collectors.py
```

---

## Troubleshooting

### "Enrollment no habilitado en este servidor"

El operador debe poner `MONITOR_ENROLLMENT_TOKEN` en `gp-it/.env`.
Si ya no quieres auto-enrollment, deja la variable vacía y registra
el nodo manualmente desde el dashboard (con JWT).

### "Heartbeat 401"

La api_key fue rechazada (rotada o servidor con DB restaurada).
Solución: borrar `<state_dir>/state.json` y dejar que el agente
vuelva a hacer enroll con el `enrollment_token`.

### El servicio no arranca

```powershell
Get-EventLog -LogName Application -Source "gp-monitor" -Newest 20
# o
python -m gp_monitor run     # correr en foreground para ver el traceback
```

### Load average aparece como `null` en Windows

Esperado: `psutil.getloadavg()` solo existe en Linux/macOS.

---

## Licencia

ISC — Grupo Plasencia IT.