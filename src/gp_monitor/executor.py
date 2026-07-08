"""Ejecutor de comandos para el agente gp-monitor.

Maneja la ejecucion segura de comandos PowerShell en Windows, con:
- Truncado de output (1MB max stdout/stderr cada uno)
- Timeout configurable (5-600s, default 60s)
- Parsing de output estructurado para tasks especificos
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("gp_monitor.executor")

# Tama\u00f1o maximo de stdout/stderr persistido (1MB)
MAX_OUTPUT_BYTES = 1 * 1024 * 1024

# Tama\u00f1o maximo recomendado para stderr combinado (mas permisivo)
MAX_STDERR_BYTES = 1 * 1024 * 1024


@dataclass
class CommandResult:
    status: str                # 'succeeded' | 'failed'
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool
    error_message: Optional[str] = None


def _truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    """Trunca `text` a `max_bytes` bytes. Devuelve (texto, truncado)."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False
    truncated_text = encoded[:max_bytes].decode("utf-8", errors="replace")
    truncated_text += f"\n\n[... output truncado a {max_bytes // 1024}KB ...]"
    return truncated_text, True


def _build_powershell_command(command: str) -> list[str]:
    """Construye el argv para ejecutar `command` con PowerShell -NoProfile -Command.

    Devuelve una lista (no string) para evitar shell injection.
    """
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command", command,
    ]


def run_powershell(command: str, timeout_seconds: int = 60) -> CommandResult:
    """Ejecuta `command` en PowerShell con timeout y captura output.

    Devuelve CommandResult con stdout/stderr truncados a 1MB cada uno
    y duration_ms calculado.
    """
    if not command or not command.strip():
        return CommandResult(
            status="failed", exit_code=-1, stdout="", stderr="",
            duration_ms=0, truncated=False,
            error_message="Comando vacio",
        )

    argv = _build_powershell_command(command)
    start = time.time()
    try:
        proc = subprocess.run(
            argv,
            shell=False,                # argv es lista, no string
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            # No cwd= para que corra en el CWD del servicio
            # (que es donde esta el contexto)
        )
        duration_ms = int((time.time() - start) * 1000)
        stdout, stdout_trunc = _truncate(proc.stdout or "", MAX_OUTPUT_BYTES)
        stderr, stderr_trunc = _truncate(proc.stderr or "", MAX_STDERR_BYTES)
        status = "succeeded" if proc.returncode == 0 else "failed"
        return CommandResult(
            status=status,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            truncated=stdout_trunc or stderr_trunc,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.time() - start) * 1000)
        return CommandResult(
            status="failed",
            exit_code=-1,
            stdout=(exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""),
            stderr=(exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""),
            duration_ms=duration_ms,
            truncated=False,
            error_message=f"Timeout: el comando supero los {timeout_seconds}s",
        )
    except FileNotFoundError:
        # powershell.exe no esta en PATH
        return CommandResult(
            status="failed", exit_code=-1, stdout="", stderr="",
            duration_ms=0, truncated=False,
            error_message="powershell.exe no encontrado en PATH",
        )
    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        return CommandResult(
            status="failed", exit_code=-1, stdout="", stderr="",
            duration_ms=duration_ms, truncated=False,
            error_message=f"Excepcion: {exc}",
        )


# ─── Tasks pre-armados (Fase 3) ─────────────────────────────────────────────

def run_top_processes(payload: dict, timeout: int) -> CommandResult:
    """Top N procesos por CPU o memoria. payload: {top_n: int, sort_by: 'cpu'|'memory'}"""
    top_n = int(payload.get("topN", 10) or 10)
    top_n = max(1, min(50, top_n))
    sort_by = (payload.get("sortBy") or "cpu").lower()
    if sort_by == "memory":
        sort_property = "WS"; header = "RAM (MB)"
        # Ordenar por WorkingSet descendente
        ps_cmd = (
            f"Get-Process | Sort-Object WS -Descending | Select-Object -First {top_n} "
            f"Id, ProcessName, @{{n='{header}';e={{[int]($_.WS/1MB)}}}}, CPU | "
            f"Format-Table -AutoSize | Out-String -Width 4096"
        )
    else:
        sort_property = "CPU"; header = "CPU (s)"
        ps_cmd = (
            f"Get-Process | Sort-Object CPU -Descending | Select-Object -First {top_n} "
            f"Id, ProcessName, CPU, @{{n='{header}';e={{[int]$_.CPU}}}}, WS | "
            f"Format-Table -AutoSize | Out-String -Width 4096"
        )
    return run_powershell(ps_cmd, timeout_seconds=timeout)


def run_disk_info(payload: dict, timeout: int) -> CommandResult:
    """Informacion de todas las unidades (tama\u00f1o, libre, % uso)."""
    ps_cmd = (
        "Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used -ne $null } | "
        "ForEach-Object { "
        "[PSCustomObject]@{ "
        "Drive=$_.Name; "
        "TotalGB=[math]::Round(($_.Used + $_.Free)/1GB, 2); "
        "UsedGB=[math]::Round($_.Used/1GB, 2); "
        "FreeGB=[math]::Round($_.Free/1GB, 2); "
        "PercentUsed=[math]::Round(($_.Used/($_.Used+$_.Free))*100, 1) "
        "} "
        "} | Format-Table -AutoSize | Out-String -Width 4096"
    )
    return run_powershell(ps_cmd, timeout_seconds=timeout)


def run_stopped_services(payload: dict, timeout: int) -> CommandResult:
    """Lista servicios en estado 'Stopped'."""
    ps_cmd = (
        "Get-Service | Where-Object { $_.Status -eq 'Stopped' } | "
        "Select-Object Name, DisplayName, StartType | "
        "Format-Table -AutoSize | Out-String -Width 4096"
    )
    return run_powershell(ps_cmd, timeout_seconds=timeout)


def run_recent_errors(payload: dict, timeout: int) -> CommandResult:
    """Eventos de error recientes del system log. payload: {hours: int, logName: str}"""
    hours = int(payload.get("hours", 24) or 24)
    hours = max(1, min(168, hours))
    log_name = payload.get("logName", "System")
    if log_name not in ("System", "Application"):
        log_name = "System"
    ps_cmd = (
        f"Get-EventLog -LogName {log_name} -EntryType Error -Newest 50 "
        f"-ErrorAction SilentlyContinue | Where-Object {{ $_.TimeGenerated -gt (Get-Date).AddHours(-{hours}) }} | "
        f"Select-Object TimeGenerated, Source, EventID, @{{n='Message';e={{$_.Message -replace '\\s+',' ' | Select-Object -First 200}}}} | "
        f"Format-Table -AutoSize -Wrap | Out-String -Width 4096"
    )
    return run_powershell(ps_cmd, timeout_seconds=timeout)


def run_restart_service(payload: dict, timeout: int) -> CommandResult:
    """Reinicia un servicio. payload: {serviceName: str}"""
    name = (payload.get("serviceName") or "").strip()
    # Sanitizacion basica: solo letras, numeros, guion bajo, guion, punto
    if not name or not re.match(r"^[A-Za-z0-9_.\-]+$", name):
        return CommandResult(
            status="failed", exit_code=-1, stdout="", stderr="",
            duration_ms=0, truncated=False,
            error_message=f"serviceName invalido: {name!r}",
        )
    ps_cmd = (
        f"Restart-Service -Name '{name}' -Force -ErrorAction Stop; "
        f"Start-Sleep -Seconds 1; "
        f"Get-Service -Name '{name}' | Select-Object Name, Status | "
        f"Format-List | Out-String -Width 4096"
    )
    # Restart-Service puede tardar; permitimos 60s o lo que pidio el usuario
    return run_powershell(ps_cmd, timeout_seconds=max(timeout, 30))


# Dispatcher centralizado
TASK_RUNNERS = {
    "RunCommand": lambda payload, timeout, **kw: run_powershell(
        kw.get("command", payload.get("command", "")),
        timeout_seconds=timeout,
    ),
    "GetTopProcesses": run_top_processes,
    "GetDiskInfo": run_disk_info,
    "GetStoppedServices": run_stopped_services,
    "GetRecentErrors": run_recent_errors,
    "RestartService": run_restart_service,
}


def run_task(task_type: str, payload: dict, timeout: int, *,
             command: str = "", allow_arbitrary: bool = False,
             allowlist=None) -> CommandResult:
    """Punto de entrada. Despacha segun el task_type.

    Para RunCommand:
    - Si allow_arbitrary=True: ejecuta cualquier cosa (permiso lo valida el backend)
    - Si no: valida contra la allowlist. Si no matchea, devuelve error.
    """
    runner = TASK_RUNNERS.get(task_type)
    if runner is None:
        return CommandResult(
            status="failed", exit_code=-1, stdout="", stderr="",
            duration_ms=0, truncated=False,
            error_message=f"Tipo de task desconocido: {task_type}",
        )

    if task_type == "RunCommand":
        cmd = command or (payload or {}).get("command", "")
        if not allow_arbitrary:
            if allowlist is None or len(allowlist) == 0:
                return CommandResult(
                    status="failed", exit_code=-1, stdout="", stderr="",
                    duration_ms=0, truncated=False,
                    error_message="allow_arbitrary=False pero el agente no tiene allowlist cargada",
                )
            if not allowlist.is_allowed(cmd):
                return CommandResult(
                    status="failed", exit_code=-1, stdout="", stderr="",
                    duration_ms=0, truncated=False,
                    error_message="Comando no esta en la allowlist. allow_arbitrary=False.",
                )
        return run_powershell(cmd, timeout_seconds=timeout)

    return runner(payload or {}, timeout)
