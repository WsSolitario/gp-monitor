"""Windows Service wrapper para gp-monitor.

Permite instalar el agente como servicio de Windows (similar a NSSM/SC):
    gp-monitor install
    gp-monitor uninstall
    gp-monitor start
    gp-monitor stop
    gp-monitor status

Usa pywin32 (servicemanager + win32serviceutil).
Si el módulo no está disponible (no Windows o pywin32 no instalado),
los comandos install/uninstall/start/stop devuelven un mensaje claro.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gp_monitor.windows_service")

SERVICE_NAME = "gp-monitor"
SERVICE_DISPLAY_NAME = "gp-monitor Agent"
SERVICE_DESCRIPTION = (
    "Agente Python que reporta métricas del servidor a gp-it "
    "(https://api.dev.gp.ssdevsolutions.com)."
)


def _is_windows() -> bool:
    return os.name == "nt"


def _pywin32_available() -> bool:
    if not _is_windows():
        return False
    try:
        import win32serviceutil  # noqa: F401
        import win32service      # noqa: F401
        import servicemanager    # noqa: F401
        return True
    except ImportError:
        return False


# ─── Implementación del servicio (subclase de Win32Service) ──────────────────

class GpMonitorWindowsService:
    """Implementación concreta del servicio. Se registra con win32serviceutil."""

    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self) -> None:
        if not _pywin32_available():
            raise RuntimeError("pywin32 no disponible")

    # pywin32 llama estos nombres exactos
    def SvcDoRun(self) -> None:                             # noqa: N802
        import servicemanager
        from gp_monitor.agent import MonitorAgent
        from gp_monitor.config import load_config

        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )

        try:
            config = load_config()
            self._agent = MonitorAgent(config)
            self._agent.run()
        except Exception as exc:                            # noqa: BLE001
            logger.exception("Error en SvcDoRun: %s", exc)
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_ERROR_TYPE,
                0xF000,  # generic error
                (f"gp-monitor crashed: {exc}", ""),
            )

    def SvcStop(self) -> None:                              # noqa: N802
        import servicemanager
        from win32service import ServiceStop  # type: ignore

        self.ReportServiceStatus(ServiceStop)
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, ""),
        )
        if getattr(self, "_agent", None) is not None:
            self._agent.stop()


# ─── Comandos CLI ─────────────────────────────────────────────────────────────

def install_service(config_path: Optional[Path] = None) -> int:
    """Registra gp-monitor como servicio de Windows."""
    if not _is_windows():
        print("✗ Esta función solo funciona en Windows.")
        return 2
    if not _pywin32_available():
        print("✗ pywin32 no está instalado. Ejecuta: pip install pywin32")
        return 2

    import win32serviceutil
    import win32api
    import pywintypes

    # Determinar python.exe y el path de config para pasarlos al servicio
    python_exe = sys.executable
    # argv[0] es el módulo entry-point; si está frozen, usar el exe directamente
    service_module = "gp_monitor.windows_service"
    config_arg = f'--config "{config_path}"' if config_path else ""

    cmd = f'"{python_exe}" -m {service_module} {config_arg}'.strip()

    try:
        win32serviceutil.InstallService(
            pythonClassString=f"{service_module}.GpMonitorWindowsService",
            serviceName=SERVICE_NAME,
            displayName=SERVICE_DISPLAY_NAME,
            description=SERVICE_DESCRIPTION,
            exeName=cmd,
            startType=win32serviceutil.SERVICE_AUTO_START,
        )
        print(f"✓ Servicio '{SERVICE_NAME}' instalado.")
        print(f"  Para arrancarlo:    gp-monitor start")
        print(f"  Auto-arranca con Windows.")
        return 0
    except pywintypes.error as exc:
        print(f"✗ Error instalando servicio: {exc}")
        return 1


def uninstall_service() -> int:
    if not _is_windows() or not _pywin32_available():
        print("✗ Solo en Windows con pywin32.")
        return 2

    import win32serviceutil
    import pywintypes

    # Parar primero si está corriendo
    try:
        win32serviceutil.StopService(SERVICE_NAME)
        print("• Servicio detenido.")
    except pywintypes.error:
        pass

    try:
        win32serviceutil.RemoveService(SERVICE_NAME)
        print(f"✓ Servicio '{SERVICE_NAME}' desinstalado.")
        return 0
    except pywintypes.error as exc:
        print(f"✗ Error desinstalando servicio: {exc}")
        return 1


def start_service() -> int:
    if not _is_windows() or not _pywin32_available():
        return 2
    import win32serviceutil
    import pywintypes
    try:
        win32serviceutil.StartService(SERVICE_NAME)
        print(f"✓ Servicio '{SERVICE_NAME}' arrancado.")
        return 0
    except pywintypes.error as exc:
        print(f"✗ Error arrancando servicio: {exc}")
        return 1


def stop_service() -> int:
    if not _is_windows() or not _pywin32_available():
        return 2
    import win32serviceutil
    import pywintypes
    try:
        win32serviceutil.StopService(SERVICE_NAME)
        print(f"✓ Servicio '{SERVICE_NAME}' detenido.")
        return 0
    except pywintypes.error as exc:
        print(f"✗ Error deteniendo servicio: {exc}")
        return 1


def service_status() -> int:
    if not _is_windows() or not _pywin32_available():
        return 2
    import win32serviceutil
    import pywintypes
    try:
        status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)
        running = status[1] == win32serviceutil.SERVICE_RUNNING
        state_code = status[1]
        STATE_NAMES = {
            1: "STOPPED",
            2: "START_PENDING",
            3: "STOP_PENDING",
            4: "RUNNING",
            5: "CONTINUE_PENDING",
            6: "PAUSE_PENDING",
            7: "PAUSED",
        }
        print(f"Servicio '{SERVICE_NAME}': {STATE_NAMES.get(state_code, state_code)}")
        return 0 if running else 1
    except pywintypes.error as exc:
        print(f"✗ Error consultando servicio: {exc}")
        return 1


# ─── Entry point cuando el SCM arranca el servicio ────────────────────────────

def handle_service_command_line() -> Optional[int]:
    """Si pywin32serviceutil.HandleCommandLine existe, despacha."""
    if not _pywin32_available():
        return None
    try:
        import win32serviceutil
    except ImportError:
        return None
    # Devuelve None si no fue invocado con argumentos de servicio.
    try:
        win32serviceutil.HandleCommandLine(GpMonitorWindowsService)
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)