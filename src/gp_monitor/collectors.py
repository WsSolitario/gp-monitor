"""Recolectores de métricas usando psutil.

Devuelven un dict JSON-serializable con la forma:

{
  "cpu_usage": 12.5,            # % (0-100)
  "memory_usage": 67.2,         # % (0-100)
  "disk_usage": 44.0,           # % del disco raíz (0-100)
  "load_avg_1m": 0.45,          # opcional (Linux/macOS)
  "load_avg_5m": 0.50,
  "load_avg_15m": 0.55,
  "network_rx_bps": 1234,       # bytes/seg recibidos (promedio ventana)
  "network_tx_bps": 567,        # bytes/seg enviados
  "uptime_seconds": 86400,
}

Cada colector es defensivo: si psutil falla para una métrica, devuelve
None y loguea. Nunca levanta excepciones al caller.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import time
from typing import Any, Dict, Optional

import psutil

logger = logging.getLogger(__name__)


# ─── Helpers internos ──────────────────────────────────────────────────────────

def _pct(used: float, total: float) -> Optional[float]:
    if total <= 0:
        return None
    val = (used / total) * 100.0
    # Acotar a [0, 100] por seguridad
    return max(0.0, min(100.0, round(val, 2)))


def _detect_os_info() -> Dict[str, str]:
    """Información del SO donde corre el agente (multiplataforma)."""
    try:
        uname = platform.uname()
        return {
            "name": platform.system() or "Unknown",        # Windows / Linux / Darwin
            "version": uname.version or "",
            "release": uname.release or "",
            "arch": uname.machine or platform.architecture()[0],
        }
    except Exception as exc:                                # noqa: BLE001
        logger.debug("No se pudo detectar SO: %s", exc)
        return {
            "name": "Unknown",
            "version": "",
            "release": "",
            "arch": "",
        }


# ─── Métricas individuales ────────────────────────────────────────────────────

def collect_cpu_usage(interval: float = 0.5) -> Optional[float]:
    """% de CPU global. Usa interval para tener lectura precisa (bloquea)."""
    try:
        val = psutil.cpu_percent(interval=interval)
        return round(float(val), 2)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("cpu_percent falló: %s", exc)
        return None


def collect_memory_usage() -> Optional[float]:
    try:
        mem = psutil.virtual_memory()
        return _pct(mem.used, mem.total)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("virtual_memory falló: %s", exc)
        return None


def collect_disk_usage(path: Optional[str] = None) -> Optional[float]:
    """% de uso del disco que contiene `path` (por defecto la raíz del SO)."""
    target = path or os.path.abspath(os.sep)                # "/" en Unix, "C:\\" en Windows
    try:
        usage = psutil.disk_usage(target)
        return _pct(usage.used, usage.total)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("disk_usage(%r) falló: %s", target, exc)
        return None


def collect_load_average() -> Dict[str, Optional[float]]:
    """Load average 1/5/15 minutos. Solo Linux/macOS; en Windows devuelve None."""
    out = {"load_avg_1m": None, "load_avg_5m": None, "load_avg_15m": None}
    if not hasattr(psutil, "getloadavg"):
        return out
    try:
        la1, la5, la15 = psutil.getloadavg()
        out["load_avg_1m"] = round(float(la1), 2)
        out["load_avg_5m"] = round(float(la5), 2)
        out["load_avg_15m"] = round(float(la15), 2)
    except Exception as exc:                                # noqa: BLE001
        logger.debug("getloadavg falló: %s", exc)
    return out


def collect_uptime_seconds() -> Optional[int]:
    try:
        boot = psutil.boot_time()
        return int(time.time() - boot)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("boot_time falló: %s", exc)
        return None


# ─── Network rate (ventana deslizante) ────────────────────────────────────────

class NetworkRateCollector:
    """Calcula bytes/seg de red promediados en una ventana.

    Diseño simple: en cada `sample()` se guarda (timestamp, bytes_sent, bytes_recv)
    acumulado. En `collect()` se compara con la muestra anterior y se devuelve
    el delta/seg. La primera llamada siempre devuelve (0, 0).
    """

    def __init__(self) -> None:
        self._last_ts: Optional[float] = None
        self._last_bytes_sent: Optional[int] = None
        self._last_bytes_recv: Optional[int] = None

    def sample(self) -> Dict[str, int]:
        """Toma una muestra y devuelve {network_rx_bps, network_tx_bps} desde la última."""
        now = time.time()
        try:
            counters = psutil.net_io_counters()
            sent = int(counters.bytes_sent)
            recv = int(counters.bytes_recv)
        except Exception as exc:                            # noqa: BLE001
            logger.warning("net_io_counters falló: %s", exc)
            self._last_ts = now
            return {"network_rx_bps": 0, "network_tx_bps": 0}

        if self._last_ts is None:
            self._last_ts = now
            self._last_bytes_sent = sent
            self._last_bytes_recv = recv
            return {"network_rx_bps": 0, "network_tx_bps": 0}

        elapsed = max(0.001, now - self._last_ts)
        # Protegerse contra contadores que reinician (p.ej., interface recargada)
        if sent < (self._last_bytes_sent or 0):
            self._last_bytes_sent = sent
        if recv < (self._last_bytes_recv or 0):
            self._last_bytes_recv = recv

        rx = max(0, recv - (self._last_bytes_recv or 0))
        tx = max(0, sent - (self._last_bytes_sent or 0))

        rx_bps = int(rx / elapsed)
        tx_bps = int(tx / elapsed)

        self._last_ts = now
        self._last_bytes_sent = sent
        self._last_bytes_recv = recv

        return {"network_rx_bps": rx_bps, "network_tx_bps": tx_bps}


# ─── IPs internas ─────────────────────────────────────────────────────────────

def _is_internal_ipv4(addr: str) -> bool:
    """True si `addr` es IPv4 util para un servidor (no loopback, no link-local)."""
    if not addr or not addr[0].isdigit():
        return False
    parts = addr.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if octets[0] == 127:                                   # loopback
        return False
    if octets[0] == 169 and octets[1] == 254:             # link-local
        return False
    if octets[0] == 0:                                     # "this network"
        return False
    if octets[0] >= 224:                                   # multicast / reserved
        return False
    return True


def get_primary_internal_ip() -> Optional[str]:
    """Devuelve la IP que el server usaria para trafico saliente.

    Metodo: abrir un socket UDP y hacer 'connect' a un destino publico
    (no envia paquetes, solo determina la ruta). Si no se puede, cae
    al primer psutil.net_if_addrs() IPv4 interno.

    Devuelve None si no se puede determinar.
    """
    # 1) Metodo socket.connect: detecta la IP "outbound" real
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if _is_internal_ipv4(ip):
            return ip
    except Exception:
        pass
    finally:
        s.close()

    # 2) Fallback: psutil enumera interfaces
    try:
        for _iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                fam = getattr(addr, "family", None)
                # psutil usa AF_INET (int 2) en vez de socket.AF_INET en algunas builds.
                # Aceptamos cualquier familia que represente IPv4.
                if fam in (socket.AF_INET, 2) and _is_internal_ipv4(addr.address):
                    return addr.address
    except Exception:
        pass

    return None


def collect_internal_ips() -> list[str]:
    """Devuelve la lista de IPv4 internas (no loopback / no link-local).

    La primera entrada es la IP 'principal' (la que usaria trafico saliente).
    Las siguientes son las otras NICs internas detectadas.

    Devuelve [] si no se puede determinar nada.
    """
    seen: set[str] = set()
    result: list[str] = []

    # Primero la IP principal (la mas util para el dashboard)
    primary = get_primary_internal_ip()
    if primary and primary not in seen:
        seen.add(primary)
        result.append(primary)

    # Despues las demas interfaces IPv4 internas
    try:
        for _iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                fam = getattr(addr, "family", None)
                if fam in (socket.AF_INET, 2) and _is_internal_ipv4(addr.address):
                    if addr.address not in seen:
                        seen.add(addr.address)
                        result.append(addr.address)
    except Exception as exc:
        logger.debug("No se pudo enumerar interfaces de red: %s", exc)

    return result


# ─── API principal ────────────────────────────────────────────────────────────

def collect_metrics(
    disk_path: Optional[str] = None,
    cpu_interval: float = 0.5,
    net: Optional[NetworkRateCollector] = None,
) -> Dict[str, Any]:
    """Recolecta todas las métricas. Defensivo: nunca levanta excepciones."""
    if net is None:
        net = NetworkRateCollector()

    metrics: Dict[str, Any] = {
        "cpu_usage":    collect_cpu_usage(interval=cpu_interval),
        "memory_usage": collect_memory_usage(),
        "disk_usage":   collect_disk_usage(path=disk_path),
    }

    metrics.update(collect_load_average())
    metrics["uptime_seconds"] = collect_uptime_seconds()
    metrics.update(net.sample())

    return metrics


def get_os_info() -> Dict[str, str]:
    return _detect_os_info()


def get_agent_version() -> str:
    from gp_monitor import __version__
    return __version__