"""Loop principal del agente gp-monitor.

Responsabilidades:
  - Cargar config
  - Cargar o generar UUID
  - Hacer enroll (si no hay api_key guardada)
  - Loop: cada `heartbeat_interval_seconds` recolectar métricas y enviar heartbeat
  - Persistir estado tras cada cambio
  - Manejar reconexión ante errores transitorios
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from gp_monitor.api_client import MonitorApiError, MonitorClient
from gp_monitor.collectors import (
    NetworkRateCollector,
    collect_internal_ips,
    collect_metrics,
    get_agent_version,
    get_os_info,
    get_rdp_sessions,
)
from gp_monitor.config import Config, load_config
from gp_monitor.state import (
    AgentState,
    generate_node_uuid,
    load_state,
    save_state,
)

logger = logging.getLogger("gp_monitor.agent")


def setup_logging(level: str, log_file: Optional[str], state_dir: Path) -> None:
    """Configura logging para stderr + archivo.

    Si `log_file` viene None/empty, default a `<state_dir>/gp-monitor.log`
    (creando el directorio si hace falta). Asi el agente deja logs
    visibles cuando corre como servicio de Windows, donde stderr se
    descarta.

    Esta función se llama desde MonitorAgent.__init__ para que el setup
    aplique tanto en modo CLI como en modo servicio.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    target = log_file
    if not target:
        try:
            default_log = state_dir / "gp-monitor.log"
            default_log.parent.mkdir(parents=True, exist_ok=True)
            target = str(default_log)
        except OSError:
            target = None  # fallback a solo stderr

    if target:
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(target, encoding="utf-8"))
        except OSError:
            pass

    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


class MonitorAgent:
    """Agente de monitoreo. Un único loop por proceso."""

    def __init__(self, config: Config, *, client: Optional[MonitorClient] = None) -> None:
        # Setup de logging ANTES de cualquier otra cosa — asi aun si falla
        # algo abajo, queda registro en el archivo.
        setup_logging(config.log_level, config.log_file, config.state_dir_path())

        self.config = config
        # Pasamos la api_key al cliente para que pueda usarla en los
        # endpoints de tasks (que requieren x-monitor-api-key).
        from gp_monitor.api_client import MonitorClient as _MC
        if client is None:
            self.client = _MC(
                api_url=config.api_url,
                timeout=config.http_timeout_seconds,
            )
        else:
            self.client = client
        self.state_dir = config.state_dir_path()
        self.state: Optional[AgentState] = None
        self.net_collector = NetworkRateCollector()

        # Allowlist de comandos (Fase 1)
        from gp_monitor.allowlist import CommandAllowlist
        self.allowlist = CommandAllowlist.load()

        self._stop_event = threading.Event()
        self._stop_event.set()  # inicializado = no corriendo

    # ─── Lifecycle ───────────────────────────────────────────────────────────
    def _setup_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def _handler(signum, frame):                    # noqa: ARG001
            logger.info("Señal %s recibida; parando agente…", signum)
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # No estamos en el main thread o plataforma sin SIGTERM
                pass

    def load_or_create_state(self) -> AgentState:
        """Carga el state desde disco o crea uno nuevo con UUID fresco."""
        ensure_dir = self.state_dir
        ensure_dir.mkdir(parents=True, exist_ok=True)

        state = load_state(self.state_dir)
        if state is None:
            logger.info("Generando nuevo UUID para este nodo")
            state = AgentState(
                uuid=generate_node_uuid(),
                api_key=None,
                agent_version=get_agent_version(),
                enrolled_at=None,
                last_heartbeat_at=None,
            )
            save_state(self.state_dir, state)
            logger.info("UUID asignado: %s", state.uuid)
        else:
            logger.info("Estado cargado: uuid=%s enrolled=%s",
                        state.uuid, "sí" if state.api_key else "no")

        # Propagar api_key al cliente HTTP (necesario para endpoints de tasks)
        if state.api_key:
            self.client.api_key = state.api_key

        return state

    def ensure_enrolled(self) -> bool:
        """Si no tenemos api_key, hacemos enroll. Devuelve True si quedó listo."""
        if self.state is None:
            self.state = self.load_or_create_state()

        if self.state.api_key:
            return True

        if not self.config.enrollment_token:
            logger.error(
                "Falta api_key en estado local y enrollment_token no está configurado. "
                "Edita config.yaml o GP_MONITOR_ENROLLMENT_TOKEN."
            )
            return False

        os_info = get_os_info()
        payload = {
            "enrollmentToken":  self.config.enrollment_token,
            "uuid":             self.state.uuid,
            "name":             self.config.resolved_name,
            "hostname":         self.config.resolved_hostname,
            "displayName":      self.config.name or None,
            "description":      self.config.description or None,
            "agency":           self.config.agency or None,
            "environment":      self.config.environment,
            "agentVersion":     self.state.agent_version,
            "osInfo":           os_info,
        }

        try:
            result = self.client.enroll(payload)
        except MonitorApiError as exc:
            logger.error("Enrollment falló: %s (status=%s)", exc, exc.status_code)
            return False

        self.state.api_key = result["apiKey"]
        self.state.enrolled_at = datetime.now(timezone.utc).isoformat()
        save_state(self.state_dir, self.state)

        logger.info("Enroll exitoso. api_key recibida y persistida.")
        return True

    # ─── Loop ────────────────────────────────────────────────────────────────
    def send_one_heartbeat(self) -> bool:
        """Recolecta métricas y envía un heartbeat. Devuelve True si la API aceptó."""
        if self.state is None or not self.state.api_key:
            logger.warning("Sin api_key; no se puede enviar heartbeat")
            return False

        os_info = get_os_info()
        metrics = collect_metrics(net=self.net_collector)
        internal_ips = collect_internal_ips()
        rdp = get_rdp_sessions()

        # El backend espera camelCase. Mapear:
        payload = {
            "uuid":           self.state.uuid,
            "hostname":       self.config.resolved_hostname,
            "agentVersion":   self.state.agent_version,
            "osInfo":         os_info,
            "cpuUsage":       metrics.get("cpu_usage"),
            "memoryUsage":    metrics.get("memory_usage"),
            "diskUsage":      metrics.get("disk_usage"),
            "loadAvg1m":      metrics.get("load_avg_1m"),
            "loadAvg5m":      metrics.get("load_avg_5m"),
            "loadAvg15m":     metrics.get("load_avg_15m"),
            "networkRxBps":   metrics.get("network_rx_bps"),
            "networkTxBps":   metrics.get("network_tx_bps"),
            "uptimeSeconds":  metrics.get("uptime_seconds"),
            "internalIps":    internal_ips if internal_ips else None,
            # Allowlist de comandos (leida de policy.toml). Sirve para que el
            # dashboard muestre un dropdown contextual cuando el operador elige
            # 'RunCommand'. Lista de {pattern, description}. Tipicamente <100
            # entradas x ~50 bytes = ~5KB, despreciable para heartbeat.
            "allowlistPatterns": self.allowlist.list_descriptions() if len(self.allowlist) > 0 else None,
            # Sesiones RDP / usuarios conectados. El dashboard cachea esto y
            # el detail page lo refresca cada 2s para vista "live".
            "rdpUsers":       rdp.get("users") or None,
            "rdpConnections": rdp.get("rdp_connections") or None,
            "collectedAt":    datetime.now(timezone.utc).isoformat(),
        }

        try:
            self.client.send_heartbeat(self.state.uuid, self.state.api_key, payload)
        except MonitorApiError as exc:
            status = exc.status_code
            if status == 401:
                # API key inválida: forzar re-enrollment en el próximo ciclo
                logger.warning("Heartbeat 401: api_key rechazada. Limpiando para re-enroll.")
                self.state.api_key = None
                self.state.enrolled_at = None
                save_state(self.state_dir, self.state)
            else:
                logger.warning("Heartbeat falló (status=%s): %s", status, exc)
            return False

        self.state.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
        save_state(self.state_dir, self.state)
        logger.info(
            "Heartbeat OK: cpu=%s%% mem=%s%% disk=%s%% up=%ss",
            payload["cpuUsage"], payload["memoryUsage"], payload["diskUsage"],
            payload["uptimeSeconds"],
        )
        return True

    def poll_and_execute_tasks(self) -> None:
        """Consulta tasks pendientes y los ejecuta uno por uno (serial).

        Cada task se ejecuta en su propio thread NO: en el main thread
        (serial, simple, predecible). Si un task tarda 5 min, bloquea
        el heartbeat hasta 5 min. Eso es OK para v1; podemos cambiar
        a pool despues.
        """
        if self.state is None or not self.state.api_key:
            return

        try:
            tasks = self.client.get_pending_tasks(self.state.uuid)
        except MonitorApiError as exc:
            logger.warning("No se pudo obtener tasks pendientes: %s", exc)
            return
        except Exception as exc:                                # noqa: BLE001
            logger.exception("Error inesperado consultando tasks: %s", exc)
            return

        if not tasks:
            return

        logger.info("Recibidas %d tasks pendientes", len(tasks))

        # Import local para evitar circular import
        from gp_monitor.executor import run_task

        for task in tasks:
            task_uuid = task.get("taskUuid")
            task_type = task.get("type")
            if not task_uuid or not task_type:
                logger.warning("Task invalida (sin taskUuid/type): %r", task)
                continue

            logger.info("Ejecutando task %s (%s)...", task_uuid, task_type)
            try:
                result = run_task(
                    task_type=task_type,
                    payload=task.get("payload") or {},
                    timeout=int(task.get("timeoutSeconds") or 60),
                    command=task.get("command") or "",
                    allow_arbitrary=bool(task.get("allowArbitrary", False)),
                    allowlist=self.allowlist,
                )
            except Exception as exc:                            # noqa: BLE001
                logger.exception("Excepcion ejecutando task %s: %s", task_uuid, exc)
                # Postear como failed para que el dashboard lo sepa
                try:
                    self.client.post_task_result(self.state.uuid, task_uuid, {
                        "status": "failed",
                        "exitCode": -1,
                        "durationMs": 0,
                        "truncated": False,
                        "stdout": "",
                        "stderr": "",
                        "errorMessage": f"agent exception: {exc}",
                    })
                except Exception:                                # noqa: BLE001
                    logger.exception("No se pudo postear el fallo")
                continue

            # Postear resultado
            try:
                self.client.post_task_result(self.state.uuid, task_uuid, {
                    "status": result.status,
                    "exitCode": result.exit_code,
                    "durationMs": result.duration_ms,
                    "truncated": result.truncated,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "errorMessage": result.error_message,
                })
                logger.info(
                    "Task %s completada: status=%s exit=%s dur=%sms trunc=%s",
                    task_uuid, result.status, result.exit_code,
                    result.duration_ms, result.truncated,
                )
            except MonitorApiError as exc:
                logger.warning("No se pudo postear resultado de task %s: %s", task_uuid, exc)

    def run(self) -> int:
        """Loop principal. Devuelve exit code."""
        self._setup_signals()
        self._stop_event.clear()

        logger.info("=" * 60)
        logger.info("gp-monitor agent v%s", get_agent_version())
        logger.info("API: %s", self.config.api_url)
        logger.info("Nodo: %s (uuid=%s)", self.config.resolved_hostname, "(cargando)")
        logger.info("Heartbeat cada %ss", self.config.heartbeat_interval_seconds)
        logger.info("Allowlist: %d comandos", len(self.allowlist))
        logger.info("=" * 60)

        self.state = self.load_or_create_state()
        logger.info("Nodo uuid=%s", self.state.uuid)

        # Health check inicial (no bloqueante, solo info)
        try:
            self.client.health()
            logger.info("API reachable (%s/health OK)", self.config.api_url)
        except MonitorApiError as exc:
            logger.warning("API no responde a /health (%s); seguimos igual", exc)

        # Enroll si hace falta
        if not self.ensure_enrolled():
            logger.error("No se pudo completar el enrollment. Reintentando en cada ciclo.")
            # No abortamos: si llega a haber red, lo hará solo.

        # Loop
        while not self._stop_event.is_set():
            if not self.state.api_key:
                # Reintentar enroll
                if not self.ensure_enrolled():
                    self._sleep_or_stop(self.config.heartbeat_interval_seconds)
                    continue

            try:
                self.send_one_heartbeat()
            except Exception as exc:                        # noqa: BLE001
                logger.exception("Excepción inesperada en send_one_heartbeat: %s", exc)

            # Poll de tasks remotos. Si hay pendientes, ejecuta serial.
            # El heartbeat ya incremento la cuenta de ciclos, asi que el
            # task poll no debe interferir con el intervalo.
            try:
                self.poll_and_execute_tasks()
            except Exception as exc:                        # noqa: BLE001
                logger.exception("Excepción inesperada en poll_and_execute_tasks: %s", exc)

            self._sleep_or_stop(self.config.heartbeat_interval_seconds)

        logger.info("gp-monitor detenido limpiamente.")
        return 0

    def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep interrumpible por stop()."""
        stopped = self._stop_event.wait(timeout=seconds)
        if stopped:
            logger.debug("_sleep_or_stop: detenido durante sleep")

    def stop(self) -> None:
        self._stop_event.set()


# ─── Helpers públicos para CLI ────────────────────────────────────────────────

def run_foreground(config: Config) -> int:
    """Helper para correr el agente en foreground (CLI run)."""
    agent = MonitorAgent(config)
    return agent.run()


def run_once(config: Config) -> int:
    """Envía un único heartbeat y termina (CLI heartbeat). Útil para debug."""
    agent = MonitorAgent(config)
    agent.state = agent.load_or_create_state()
    if not agent.ensure_enrolled():
        return 2
    ok = agent.send_one_heartbeat()
    return 0 if ok else 1