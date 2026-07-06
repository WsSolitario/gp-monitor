# Guía de despliegue de gp-monitor en servidores Windows

> **Audiencia**: administrador IT que replica el agente en N servidores
> **Tiempo por servidor**: ~10 minutos
> **Pre-requisito**: el `MONITOR_ENROLLMENT_TOKEN` configurado en `gp-it/.env`

---

## Resumen del flujo

```
Operador (tú)                     Cada servidor Windows
   │                                      │
   │  1. Pre-registrar nodo                │
   │     en gp-dash → recibe UUID          │
   │     (opcional, ver §3)                │
   │                                      │
   │  2. Compartir:                        │
   │     - URL de la API                   │
   │     - MONITOR_ENROLLMENT_TOKEN        │
   │     - Instrucciones de install        │
   │                                      │
   │                                      │  3. Clonar repo + venv
   │                                      │  4. Editar config.yaml
   │                                      │  5. .\scripts\install.bat
   │                                      │
   │                                      │  (auto-enroll con token)
   │                                      │  (instala + arranca servicio)
   │                                      │
   │                                      │  6. Cada 60s manda heartbeat
   │  7. Dashboard ve EN LÍNEA ───────────►│
```

---

## 1. Pre-requisitos en cada servidor

| Requisito | Versión mínima | Notas |
|---|---|---|
| Windows Server | 2019 / 2022 | Otros OS también funcionan (Linux/macOS) pero el script de install es para Windows |
| Python | 3.9+ | 3.12 recomendado. Agregar al PATH al instalar. |
| Acceso HTTPS saliente | `api.dev.gp.2.ssdevsolutions.com:443` | Sin proxy especial |
| Permisos | Admin local | Para registrar el servicio |
| Disk | 100 MB libres | Para venv + state + logs |

**Validar Python** (PowerShell como Admin):

```powershell
python --version
# Esperado: Python 3.9.x o superior
```

Si no está: https://www.python.org/downloads/windows/ — marcar **"Add Python to PATH"** al instalar.

---

## 2. Install por servidor (copy-paste)

Abrir **PowerShell como Administrador** en el servidor destino y ejecutar:

```powershell
# 0. Directorio de instalación (ajustar a tu estructura)
$InstallDir = "C:\Tools"
$AgentDir   = "$InstallDir\gp-monitor"

# 1. Clonar el repo
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
cd $InstallDir
git clone https://github.com/WsSolitario/gp-monitor.git
cd gp-monitor

# 2. Checkout de la rama estable
git checkout release/v1.0.0

# 3. Crear venv + instalar dependencias
python -m venv .venv
.\.venv\Scripts\pip install --upgrade pip
.\.venv\Scripts\pip install -e .

# 4. Configurar (ESTE paso es el único que requiere editar)
copy config\config.example.yaml config\config.yaml
notepad config\config.yaml
```

### `config.yaml` — qué editar

Reemplazar SOLO estos 4 campos:

```yaml
api_url: "https://api.dev.gp.2.ssdevsolutions.com"
enrollment_token: "PEGAR_AQUI_EL_TOKEN_QUE_TE_PASARON"

agency: "Matriz"           # o la agencia de este servidor
environment: "production"  # production / staging / development / testing
```

`api_url` y `enrollment_token` son los mismos para todos los servidores (es un valor compartido). `agency` y `environment` varían.

### 5. Instalar como servicio

```powershell
# 5. Instalar y arrancar el servicio Windows
.\scripts\install.bat
```

Esto:
- Registra el servicio "gp-monitor" (auto-arranca con Windows)
- Lo arranca
- Configura reinicio automático si falla

---

## 3. Pre-registro en gp-dash (opcional pero recomendado)

Aunque el agente se auto-enrolla solo, **pre-registrar el nodo en gp-dash** te permite:

- Definir `name`, `description`, `agency` desde el dashboard antes de instalar
- Ver el nodo en la lista desde el primer heartbeat (sin esperar al primero)
- Asignar la agencia/entorno desde el dashboard y no requerir editar `config.yaml`

**Desde gp-dash** (`https://panel.ssdevsolutions.com/dashboard/monitor/registrar`):

| Campo | Valor |
|---|---|
| Nombre | `SRV-WEB-01` (legible, lo verá el dashboard) |
| Hostname | `SRV-WEB-01.contoso.local` (debe coincidir con `socket.gethostname()`) |
| Nombre mostrado | "Servidor Web Producción" (opcional) |
| Agencia | "Matriz" |
| Entorno | `production` |
| Descripción | "Reverse proxy, nginx + certbot" |

Tras crear, el backend genera un UUID y deja `api_key_hash = NULL`. El agente, al primer arranque, hace enroll con su UUID y hostname, y el backend matchea por hostname → reutiliza el registro existente y completa la api_key.

**Si no pre-registrás**: el agente crea el nodo automáticamente al primer enroll con los valores de `config.yaml`.

---

## 4. Verificación post-install

Ejecutar en el server (PowerShell como Admin):

```powershell
# 1. Estado del servicio
.\.venv\Scripts\python.exe -m gp_monitor status
# Esperado: Servicio 'gp-monitor': RUNNING

# 2. Si el status dice STOPPED, ver el último error
Get-WinEvent -LogName Application -MaxEvents 100 |
  Where-Object { $_.ProviderName -eq "Python Service" -or $_.ProviderName -eq "gp-monitor" } |
  Select-Object -First 3 |
  Format-List TimeCreated, ProviderName, Id, Message

# 3. Log del agente en vivo (Ctrl+C para salir)
Get-Content "C:\ProgramData\gp-monitor\gp-monitor.log" -Wait

# 4. Forzar un heartbeat manual para test inmediato
.\.venv\Scripts\python.exe -m gp_monitor heartbeat
# Esperado: ✓ Heartbeat enviado correctamente.
```

En el **dashboard** (`https://panel.ssdevsolutions.com/dashboard/monitor`):
- La card del nuevo servidor aparece en menos de 60 segundos
- Badge: **EN LÍNEA** (punto pulsando)
- Métricas actualizándose cada 30 segundos

---

## 5. Operación día a día

Todos los comandos asumen PowerShell como Admin en el server.

```powershell
# Estado
.\.venv\Scripts\python.exe -m gp_monitor status

# Arrancar / detener
.\.venv\Scripts\python.exe -m gp_monitor start
.\.venv\Scripts\python.exe -m gp_monitor stop

# Heartbeat manual (debug)
.\.venv\Scripts\python.exe -m gp_monitor heartbeat

# Log en vivo
Get-Content "C:\ProgramData\gp-monitor\gp-monitor.log" -Wait

# Cambiar configuración (sin reinstalar)
notepad config\config.yaml
.\.venv\Scripts\python.exe -m gp_monitor stop
.\.venv\Scripts\python.exe -m gp_monitor start

# Desinstalar el servicio (sin borrar archivos)
.\.venv\Scripts\python.exe -m gp_monitor uninstall
```

---

## 6. Actualizar un servidor existente

```powershell
cd C:\Tools\gp-monitor
git pull origin release/v1.0.0
.\.venv\Scripts\pip install -e .
.\.venv\Scripts\python.exe -m gp_monitor stop
.\.venv\Scripts\python.exe -m gp_monitor start
```

Si el upgrade cambia la firma del servicio (raro), desinstalar antes:

```powershell
.\.venv\Scripts\python.exe -m gp_monitor uninstall
.\.venv\Scripts\python.exe -m gp_monitor install
.\.venv\Scripts\python.exe -m gp_monitor start
```

---

## 7. Troubleshooting (los 5 bugs que ya documentamos)

Si el servicio no queda RUNNING, en orden de probabilidad:

### Bug 1: Service stopped, Event Viewer dice `__init__() takes 1 positional argument but 2`

pywin32 pasa un argumento al constructor. Causado por versión vieja del paquete.

**Fix**: `git pull && .\install_path_unused\Scripts\pip install -e .`

### Bug 2: AttributeError `module 'win32serviceutil' has no attribute 'SERVICE_AUTO_START'`

La constante vive en `win32service`, no `win32serviceutil`.

**Fix**: actualizar el paquete (mismo comando que bug 1).

### Bug 3: AttributeError `'SvcRun'`

La clase no hereda de `ServiceFramework`.

**Fix**: actualizar el paquete.

### Bug 4: NameError `name 'win32serviceutil' is not defined`

Herencia mal armada con import local.

**Fix**: actualizar el paquete.

### Bug 5: AttributeError `'SvcDoRun'` (en `win32serviceutil.py` línea 1062)

Métodos `SvcDoRun`/`SvcStop` quedaron en la clase stub en lugar de la real.

**Fix**: actualizar el paquete.

**Si ves un error NUEVO** (no en la lista):

```powershell
Get-WinEvent -LogName Application -MaxEvents 100 |
  Where-Object { $_.ProviderName -eq "Python Service" } |
  Select-Object -First 3 |
  Format-List TimeCreated, Id, Message
```

Pegame el output y te ayudo.

---

## 8. Desinstalar completamente (cleanup total)

```powershell
cd C:\Tools\gp-monitor
.\.venv\Scripts\python.exe -m gp_monitor uninstall

# Borrar archivos (opcional)
cd ..
Remove-Item -Recurse -Force gp-monitor

# Borrar estado del agente (UUID + api_key persistidos)
Remove-Item -Recurse -Force "C:\ProgramData\gp-monitor"
```

---

## 9. Despliegue automatizado en N servidores

Para deployar en muchos servers a la vez, dos opciones:

### Opción A — PowerShell Remoting (built-in, hasta ~50 servers)

```powershell
$servers = @("SRV-WEB-01","SRV-WEB-02","SRV-DB-01","SRV-DB-02","SRV-APP-01")

Invoke-Command -ComputerName $servers -ScriptBlock {
    param($repoUrl, $token, $agency, $env)
    
    # Install
    if (-not (Test-Path "C:\Tools\gp-monitor")) {
        New-Item -ItemType Directory -Path "C:\Tools" -Force | Out-Null
        cd "C:\Tools"
        git clone $repoUrl
    }
    cd "C:\Tools\gp-monitor"
    git pull origin release/v1.0.0
    
    if (-not (Test-Path ".venv")) {
        python -m venv .venv
    }
    .\.venv\Scripts\pip install -e .
    
    # Configure
    @"
api_url: "https://api.dev.gp.2.ssdevsolutions.com"
enrollment_token: "$token"
agency: "$agency"
environment: "$env"
"@ | Set-Content -Path "config\config.yaml" -Encoding UTF8
    
    # Install service
    .\scripts\install.bat
} -ArgumentList $repoUrl, $token, $agency, $env
```

### Opción B — Ansible (más limpio, recomendado para 10+ servers)

```yaml
# deploy-gp-monitor.yml
- hosts: windows_servers
  vars:
    repo_url: https://github.com/WsSolitario/gp-monitor.git
    api_url: "https://api.dev.gp.2.ssdevsolutions.com"
    enrollment_token: "{{ vault_gp_monitor_enrollment_token }}"
  tasks:
    - name: Clone repo
      git:
        repo: "{{ repo_url }}"
        dest: 'C:\Tools\gp-monitor'
        version: release/v1.0.0
    
    - name: Create venv
      win_command: python -m venv .venv
      args:
        chdir: 'C:\Tools\gp-monitor'
    
    - name: Install dependencies
      win_command: '.\.venv\Scripts\pip install -e .'
      args:
        chdir: 'C:\Tools\gp-monitor'
    
    - name: Configure
      win_copy:
        dest: 'C:\Tools\gp-monitor\config\config.yaml'
        content: |
          api_url: "{{ api_url }}"
          enrollment_token: "{{ enrollment_token }}"
          agency: "{{ agency | default('Matriz') }}"
          environment: "{{ environment | default('production') }}"
    
    - name: Install service
      win_command: .\scripts\install.bat
      args:
        chdir: 'C:\Tools\gp-monitor'
    
    - name: Verify service
      win_service:
        name: gp-monitor
        state: started
```

---

## 10. Checks de auditoría mensual

Desde el dashboard, podés ver todos los servidores en:
- `https://panel.ssdevsolutions.com/dashboard/monitor` — vista general
- Click en cada servidor para ver histórico

Señales de alerta:
- **Sin conexión** > 5 min → agente caído, requiere atención
- **Disco > 90%** → necesita limpieza
- **Memoria > 90%** sostenida → leak o sizing
- **CPU > 80%** sostenido → revisar qué corre

Si un server aparece **Sin conexión**:
1. RDP al server → `gp-monitor status`
2. Si STOPPED: `gp-monitor start`
3. Si falla: ver Event Viewer
4. Si sigue RUNNING pero no se ve en el dashboard: revisar logs del agente

---

## TL;DR — el comando único

Para un admin con prisa, esto es TODO lo que hay que correr en cada server nuevo:

```powershell
# Como Admin
git clone https://github.com/WsSolitario/gp-monitor.git C:\Tools\gp-monitor
cd C:\Tools\gp-monitor
git checkout release/v1.0.0
python -m venv .venv
.\.venv\Scripts\pip install -e .
copy config\config.example.yaml config\config.yaml
# Editar config.yaml: api_url, enrollment_token, agency, environment
.\scripts\install.bat
```

Y verificar en `https://panel.ssdevsolutions.com/dashboard/monitor` en 60 segundos.