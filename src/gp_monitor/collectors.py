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

import ctypes
import logging
import os
import platform
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
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


# ─── Sesiones RDP / usuarios conectados ──────────────────────────────────────

# Constantes de la API WTSAPI32 (Windows Terminal Services).
# Documentacion: https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/
_WTS_CURRENT_SERVER_HANDLE = ctypes.c_void_p(0).value  # NULL = current server

# WTS_CONNECTSTATE_CLASS
_WTSActive           = 0
_WTSConnected        = 1
_WTSConnectQuery     = 2
_WTSShadow           = 3
_WTSDisconnected     = 4
_WTSIdle             = 5
_WTSListen           = 6
_WTSReset            = 7
_WTSDown             = 8
_WTSInit             = 9

# WTS_INFO_CLASS (algunos valores)
_WTSUserName         = 5
_WTSDomainName       = 7
_WTSLogonTime        = 8
_WTSClientName       = 10
_WTSClientAddress    = 14


# Estructura WTS_SESSION_INFOW (32-bit en sistemas 32, 64-bit en 64).
# La hacemos portable: usamos c_uint32 y c_wchar_p que se adaptan.
class _WTS_SESSION_INFOW(ctypes.Structure):
    _fields_ = [
        ('SessionId',        ctypes.c_uint32),
        ('pWinStationName',  ctypes.c_wchar_p),
        ('State',            ctypes.c_uint32),
    ]


# Carga wtsapi32.dll una sola vez al importar el modulo
_wtsapi32 = None
_WTS_SESSION_INFO_p = ctypes.POINTER(_WTS_SESSION_INFOW)
_ppSessionInfo = ctypes.POINTER(_WTS_SESSION_INFO_p)
_pUInt32 = ctypes.POINTER(ctypes.c_uint32)
_pWchar = ctypes.POINTER(ctypes.c_wchar_p)


def _init_wtsapi():
    """Carga wtsapi32.dll y define las signatures de las funciones."""
    global _wtsapi32
    if _wtsapi32 is not None:
        return _wtsapi32
    if os.name != 'nt':
        return None
    try:
        lib = ctypes.WinDLL('wtsapi32.dll', use_last_error=True)

        # WTSEnumerateSessionsW(HANDLE, DWORD, DWORD, PWTS_SESSION_INFOW*, DWORD*)
        #                                                       ^^^^^^ Reserved (value, no pointer!)
        WTSEnumerateSessionsW = lib.WTSEnumerateSessionsW
        WTSEnumerateSessionsW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, _ppSessionInfo, _pUInt32,
        ]
        WTSEnumerateSessionsW.restype = ctypes.c_int

        # WTSQuerySessionInformationW(HANDLE, DWORD, WTS_INFO_CLASS, LPWSTR*, DWORD*)
        WTSQuerySessionInformationW = lib.WTSQuerySessionInformationW
        WTSQuerySessionInformationW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, _pWchar, _pUInt32,
        ]
        WTSQuerySessionInformationW.restype = ctypes.c_int

        # WTSFreeMemory(PVOID)
        WTSFreeMemory = lib.WTSFreeMemory
        WTSFreeMemory.argtypes = [ctypes.c_void_p]
        WTSFreeMemory.restype = ctypes.c_int

        # Stash en el lib para que las funciones queden accesibles
        lib.WTSEnumerateSessionsW = WTSEnumerateSessionsW
        lib.WTSQuerySessionInformationW = WTSQuerySessionInformationW
        lib.WTSFreeMemory = WTSFreeMemory

        _wtsapi32 = lib
        return lib
    except (OSError, AttributeError) as exc:
        logger.debug("No se pudo cargar wtsapi32.dll: %s", exc)
        return None


def _wts_query_string(session_id: int, info_class: int) -> str:
    """Llama WTSQuerySessionInformationW y devuelve el string, o '' si falla."""
    lib = _wtsapi32
    if lib is None:
        return ''
    pp_buffer = ctypes.c_wchar_p()
    p_bytes = ctypes.c_uint32(0)
    ok = lib.WTSQuerySessionInformationW(
        _WTS_CURRENT_SERVER_HANDLE,
        session_id,
        info_class,
        ctypes.byref(pp_buffer),
        ctypes.byref(p_bytes),
    )
    if not ok or not pp_buffer.value:
        return ''
    try:
        return pp_buffer.value or ''
    finally:
        lib.WTSFreeMemory(pp_buffer)


def _wts_query_logon_time(session_id: int) -> str | None:
    """Lee el logon time como ISO 8601."""
    lib = _wtsapi32
    if lib is None:
        return None
    pp_buffer = ctypes.c_wchar_p()
    p_bytes = ctypes.c_uint32(0)
    ok = lib.WTSQuerySessionInformationW(
        _WTS_CURRENT_SERVER_HANDLE,
        session_id,
        _WTSLogonTime,
        ctypes.byref(pp_buffer),
        ctypes.byref(p_bytes),
    )
    if not ok or not pp_buffer.value or p_bytes.value < 8:
        return None
    try:
        # WTSLogonTime devuelve un LARGE_INTEGER (8 bytes) con los 100ns
        # ticks desde 1601-01-01 (FILETIME). Convertimos a datetime.
        import struct
        ticks = struct.unpack('<Q', pp_buffer.value[:8])[0]
        epoch_start = datetime(1601, 1, 1, tzinfo=timezone.utc)
        # FILETIME ticks: 10_000_000 por segundo
        seconds = ticks / 10_000_000
        dt = epoch_start.fromtimestamp(epoch_start.timestamp() + seconds, tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None
    finally:
        lib.WTSFreeMemory(pp_buffer)


def _wts_query_client_address(session_id: int) -> str:
    """Lee WTSClientAddress. Devuelve IP como string o '' si no es RDP.

    WTSClientAddress devuelve un WTS_CLIENT_ADDRESS struct (4 bytes family +
    4 bytes padding + 16 bytes address). Solo es relevante para sesiones
    RDP; en consola o desconectado devuelve family=0 (AF_UNSPEC).
    """
    lib = _wtsapi32
    if lib is None:
        return ''
    pp_buffer = ctypes.c_wchar_p()
    p_bytes = ctypes.c_uint32(0)
    ok = lib.WTSQuerySessionInformationW(
        _WTS_CURRENT_SERVER_HANDLE,
        session_id,
        _WTSClientAddress,
        ctypes.byref(pp_buffer),
        ctypes.byref(p_bytes),
    )
    if not ok or not pp_buffer.value or p_bytes.value < 4:
        return ''
    try:
        import struct
        # WTS_CLIENT_ADDRESS: 4 bytes family + 18 bytes address (IPv4: 4 bytes)
        family = struct.unpack('<I', pp_buffer.value[:4])[0]
        if family == 2:  # AF_INET = IPv4
            ip_bytes = pp_buffer.value[4:8]
            return '.'.join(str(b) for b in ip_bytes)
        return ''
    except Exception:
        return ''
    finally:
        lib.WTSFreeMemory(pp_buffer)


_WTS_STATE_NAMES = {
    _WTSActive:       'Active',
    _WTSConnected:    'Connected',
    _WTSConnectQuery: 'ConnectQuery',
    _WTSShadow:       'Shadow',
    _WTSDisconnected: 'Disconnected',
    _WTSIdle:         'Idle',
    _WTSListen:       'Listen',
    _WTSReset:        'Reset',
    _WTSDown:         'Down',
    _WTSInit:         'Init',
}


def _get_sessions_via_wtsapi() -> list[dict]:
    """Enumera sesiones via WTSEnumerateSessionsW (API nativa de Windows).

    Es la unica forma confiable de listar sesiones cuando el agente
    corre como servicio. 'query session' y 'quser' (ejecutables) pueden
    fallar o devolver 0 sesiones porque requieren que la consola de la
    sesion 0 tenga window station accesible, lo cual no siempre es asi
    para servicios que corren como LocalSystem o NetworkService.

    Devuelve lista de {username, session_name, session_id, state, is_active,
    is_rdp, session_type, logon_time, host}.
    """
    lib = _init_wtsapi()
    if lib is None:
        return []

    pp_session_info = ctypes.POINTER(_WTS_SESSION_INFOW)()
    p_count = ctypes.c_uint32(0)

    if not lib.WTSEnumerateSessionsW(
        _WTS_CURRENT_SERVER_HANDLE,
        0,  # Reserved
        1,  # Version 1
        ctypes.byref(pp_session_info),
        ctypes.byref(p_count),
    ):
        return []

    sessions: list[dict] = []
    raw_session_count = 0
    try:
        count = p_count.value
        raw_session_count = count
        for i in range(count):
            try:
                si = pp_session_info[i]
            except IndexError:
                break

            session_id = si.SessionId
            win_station = si.pWinStationName or ''
            state_code = si.State
            state_str = _WTS_STATE_NAMES.get(state_code, f'Unknown({state_code})')
            sn_low = win_station.lower()

            # Filtrar EXCLUSIVAMENTE sesiones de servicio / listener RDP.
            # Importante: NO filtrar por username vacio aqui, porque desde
            # Session 0 (servicio) WTSQuerySessionInformationW a veces
            # devuelve '' para sesiones activas (sesion en uso concurrente).
            # Filtrar por username vacio ocultaba los usuarios activos.
            # Filtramos el listener RDP (state=WTSListen=6) y servicios
            # (state=Active pero win_station='Services').
            if state_code == _WTSListen:
                continue
            if sn_low == 'services':
                continue

            # Info adicional (username, hostname, logon time)
            username = _wts_query_string(session_id, _WTSUserName)
            domain = _wts_query_string(session_id, _WTSDomainName)
            client_name = _wts_query_string(session_id, _WTSClientName)
            client_addr = _wts_query_client_address(session_id)
            logon_time = _wts_query_logon_time(session_id)

            # Filtrar usuarios-sistema conocidos (despues de consultar username)
            if username and username.lower() in ('services', 'local service', 'system'):
                continue

            # Si el username esta vacio (tipico para sesiones activas desde
            # Session 0), intentar fallback via psutil (enumera procesos con
            # su SessionId). Tambien funciona: usar el nombre de la win_station
            # como label legible.
            if not username:
                fallback = _resolve_username_from_psutil(session_id)
                if fallback:
                    username = fallback
                else:
                    # Como ultimo recurso, mostrar el session_id legible
                    username = f'user@{session_id}'

            is_active = state_code in (_WTSActive, _WTSConnected)
            if sn_low == 'console':
                session_type = 'console'
                is_rdp = False
            elif 'rdp' in sn_low:
                session_type = 'rdp'
                is_rdp = True
            else:
                session_type = win_station
                is_rdp = bool(client_addr)  # heuristic: tiene IP de cliente = RDP

            full_user = f"{domain}\\{username}" if domain else username

            sessions.append({
                'username':     full_user,
                'session_name': win_station,
                'session_id':   session_id,
                'state':        state_str,
                'is_active':    is_active,
                'is_rdp':       is_rdp,
                'session_type': session_type,
                'logon_time':   logon_time,
                'host':         client_addr or client_name or '',
            })
    finally:
        lib.WTSFreeMemory(pp_session_info)

    # Log resumen para que el operador verifique desde el log del agente
    # (una linea por heartbeat, no es ruidosa).
    logger.info(
        "WTSAPI: %d sesiones crudas -> %d sesiones reportadas (filtradas: %d)",
        raw_session_count, len(sessions), raw_session_count - len(sessions),
    )

    return sessions


def _resolve_username_from_psutil(session_id: int) -> str:
    """Fallback: buscar el username del dueno de la sesion via psutil + kernel32.

    WTSQuerySessionInformationW a veces devuelve vacio para sesiones activas
    cuando el agente corre como servicio en Session 0. Como fallback, iteramos
    los procesos visibles y, para cada uno, preguntamos a Windows
    (ProcessIdToSessionId de kernel32) a que sesion pertenece. Si alguno
    cae en `session_id` y tiene username legible (via psutil), lo devolvemos.

    psutil >= 5.x expone 'username' en process_info(). 'session_id' no
    esta siempre disponible como atributo, asi que usamos kernel32.
    """
    if os.name != 'nt':
        return ''
    try:
        kernel32 = ctypes.WinDLL('kernel32.dll', use_last_error=True)
        kernel32.ProcessIdToSessionId.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.ProcessIdToSessionId.restype = ctypes.c_int
    except (OSError, AttributeError):
        return ''

    found_user = ''
    try:
        for proc in psutil.process_iter(['pid', 'username']):
            try:
                info = proc.info
                pid = info.get('pid')
                user = info.get('username') or ''
                if not pid or not user or user.lower().endswith('\\system'):
                    continue
                sess = ctypes.c_uint32(0)
                ok = kernel32.ProcessIdToSessionId(ctypes.c_uint32(pid), ctypes.byref(sess))
                if ok and sess.value == session_id:
                    if '\\' in user:
                        found_user = user.split('\\', 1)[1]
                    else:
                        found_user = user
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    except Exception as exc:                                # noqa: BLE001
        logger.debug("psutil fallback para session %d fallo: %s", session_id, exc)
    return found_user


def get_rdp_sessions() -> dict:
    """Devuelve las sesiones de usuario activas y las conexiones RDP entrantes.

    Output shape:
    {
      'users': [
        {username, session_name, session_id, state, is_active, is_rdp,
         session_type, logon_time, host},
        ...
      ],
      'rdp_connections': [
        {local_addr, local_port, remote_addr, remote_port, pid},
        ...
      ],
    }

    Implementacion: usa la API nativa de Windows (WTSEnumerateSessionsW)
    via ctypes. Esta API funciona correctamente cuando el agente corre
    como servicio (Session 0), a diferencia de 'query session' que
    puede fallar o devolver 0 sesiones.

    Las conexiones RDP (puerto 3389) vienen de psutil.net_connections,
    complementarias a la info de sesiones (dan la IP:puerto remoto).
    """
    users = _get_sessions_via_wtsapi()

    rdp_conns: list[dict] = []
    try:
        for c in psutil.net_connections(kind='inet'):
            if c.laddr and c.laddr.port == 3389 and c.status == psutil.CONN_ESTABLISHED:
                rdp_conns.append({
                    'local_addr':  c.laddr.ip,
                    'local_port':  c.laddr.port,
                    'remote_addr': c.raddr.ip if c.raddr else '',
                    'remote_port': c.raddr.port if c.raddr else 0,
                    'pid':         c.pid or 0,
                })
    except (psutil.AccessDenied, OSError, Exception) as exc:
        logger.debug("get_rdp_sessions: net_connections fallo: %s", exc)

    return {
        'users': users,
        'rdp_connections': rdp_conns,
    }


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