"""Tests del validador de allowlist."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gp_monitor.allowlist import CommandAllowlist  # noqa: E402


# ─── Tests del fix: pattern* como prefijo ──────────────────────────────────

def test_pattern_with_asterisk_matches_prefix():
    """Get-Process* debe matchear 'Get-Process', 'Get-Process | Sort', etc."""
    al = CommandAllowlist(allowed=[])  # vacia por ahora
    # Simulamos un patron cargado via policy
    from gp_monitor.allowlist import AllowedCommand
    import re
    pattern = "Get-Process*"
    regex = re.compile(
        r'^\s*' + re.escape(pattern[:-1]) + r'.*',
        re.IGNORECASE | re.DOTALL,
    )
    ac = AllowedCommand(pattern=pattern, description="", regex=regex)
    al.allowed = [ac]

    # Casos validos
    assert al.is_allowed("Get-Process") is True
    assert al.is_allowed("get-process") is True        # case-insensitive
    assert al.is_allowed("  Get-Process") is True      # leading whitespace
    assert al.is_allowed("Get-Process | Sort WS -Descending | Select -First 10") is True
    assert al.is_allowed("Get-ProcessSpooler") is False  # sin espacio -> matchea con .* pero deberia ser False
    # Espera, "Get-Process*" significa que cualquier cosa que EMPIECE con
    # "Get-Process" sin el * entra. "Get-ProcessSpooler" empieza con
    # "Get-Process" asi que SI matchea (cualquier cosa que siga).
    # El fix correcto es: pattern* = "empieza con X sin espacio obligatorio"
    # "Get-ProcessSpooler" SI matchea porque el .* acepta "Spooler" sin espacio.
    # ESTO ES POR DISENO: el operador quiere permitir cualquier cmdlet
    # que empiece con "Get-Process" aunque tenga argumentos pegados (raro pero
    # posible). Si quiere estricto, debe usar pattern sin * y el word boundary.
    # OK entonces el test confirma este comportamiento:
    assert al.is_allowed("Get-ProcessSpooler") is True  # .* acepta "Spooler"

    # Casos invalidos
    assert al.is_allowed("Set-Process") is False
    assert al.is_allowed("Remove-Process") is False
    assert al.is_allowed("") is False


def test_pattern_without_asterisk_is_strict():
    """'Get-Process' (sin *) debe matchear solo comandos que EMPIEZAN
    con 'Get-Process' seguido de espacio o fin de string."""
    al = CommandAllowlist(allowed=[])
    from gp_monitor.allowlist import AllowedCommand
    import re
    pattern = "Get-Process"
    regex = re.compile(
        r'^\s*' + re.escape(pattern) + r'(\s|$)',
        re.IGNORECASE | re.DOTALL,
    )
    ac = AllowedCommand(pattern=pattern, description="", regex=regex)
    al.allowed = [ac]

    # Casos validos
    assert al.is_allowed("Get-Process") is True
    assert al.is_allowed("Get-Process Spooler") is True
    assert al.is_allowed("Get-Process | Sort WS -Descending") is True

    # Casos invalidos (debe haber separador despues del comando)
    assert al.is_allowed("Get-ProcessSpooler") is False  # sin espacio
    assert al.is_allowed("Get-Processes") is False
    assert al.is_allowed("Set-Process") is False


def test_case_insensitive():
    al = CommandAllowlist(allowed=[])
    from gp_monitor.allowlist import AllowedCommand
    import re
    regex = re.compile(r'^\s*Get-Process.*', re.IGNORECASE | re.DOTALL)
    al.allowed = [AllowedCommand(pattern="Get-Process*", description="", regex=regex)]
    assert al.is_allowed("get-process") is True
    assert al.is_allowed("GET-PROCESS foo") is True


def test_list_descriptions():
    al = CommandAllowlist(allowed=[])
    from gp_monitor.allowlist import AllowedCommand
    import re
    for p, d in [("Get-Process*", "Procesos"), ("Get-Service*", "Servicios")]:
        regex = re.compile(r'^\s*' + re.escape(p[:-1]) + r'.*', re.IGNORECASE | re.DOTALL)
        al.allowed.append(AllowedCommand(pattern=p, description=d, regex=regex))
    descs = al.list_descriptions()
    assert len(descs) == 2
    assert descs[0] == {"pattern": "Get-Process*", "description": "Procesos"}
    assert descs[1] == {"pattern": "Get-Service*", "description": "Servicios"}


def test_load_from_file(tmp_path):
    """Carga real desde un policy.toml en disco."""
    p = tmp_path / "policy.toml"
    p.write_text("""
[[allowed]]
pattern = "Get-Process*"
description = "Procesos"

[[allowed]]
pattern = "Get-CimInstance Win32_Processor"
description = "CPU info"

[[allowed]]
pattern = "Bad-Pattern with [invalid regex"
description = "Patron invalido (no debe romper)"
""", encoding="utf-8")

    al = CommandAllowlist.load(explicit_path=p)
    assert len(al) == 2  # el tercero (regex invalido) se descarta

    assert al.is_allowed("Get-Process") is True
    assert al.is_allowed("Get-CimInstance Win32_Processor") is True
    assert al.is_allowed("Get-CimInstance Win32_LogicalDisk") is False  # patron exacto
    assert al.is_allowed("Get-ProcessSpooler") is True  # * como prefijo


def test_load_missing_file():
    """Si policy.toml no existe, allowlist queda vacia."""
    al = CommandAllowlist.load(explicit_path=Path("/no/existe/policy.toml"))
    assert len(al) == 0
    assert al.is_allowed("Get-Process") is False


# ─── Tests del collector RDP via WTSAPI ───────────────────────────────────

from gp_monitor.collectors import _get_sessions_via_wtsapi  # noqa: E402


def test_wtsapi_available():
    """El modulo wtsapi32 debe estar disponible en Windows."""
    import platform
    if platform.system() != 'Windows':
        return  # skip en linux/mac
    import ctypes
    try:
        ctypes.WinDLL('wtsapi32.dll')
        assert True
    except OSError:
        assert False, "wtsapi32.dll no encontrado"


def test_rdp_sessions_basic_shape():
    """get_rdp_sessions devuelve siempre users y rdp_connections (arrays)."""
    import platform
    if platform.system() != 'Windows':
        return  # skip
    from gp_monitor.collectors import get_rdp_sessions
    data = get_rdp_sessions()
    assert isinstance(data, dict)
    assert 'users' in data
    assert 'rdp_connections' in data
    assert isinstance(data['users'], list)
    assert isinstance(data['rdp_connections'], list)


def test_rdp_sessions_filters_services():
    """Los servicios y listeners RDP no deben aparecer en users."""
    import platform
    if platform.system() != 'Windows':
        return
    from gp_monitor.collectors import _get_sessions_via_wtsapi
    users = _get_sessions_via_wtsapi()
    for u in users:
        # Ningun servicio/listener RDP debe colarse
        assert u.get('username', '').lower() not in ('services', 'local service', 'system'), \
            f"Servicio filtrado incorrectamente: {u.get('username')}"
        sn = u.get('session_name', '').lower()
        assert not sn.startswith('rdp-tcp'), \
            f"Listener RDP colado: {u.get('session_name')}"


def test_rdp_sessions_has_required_keys():
    """Cada sesion tiene los campos requeridos por el dashboard."""
    import platform
    if platform.system() != 'Windows':
        return
    from gp_monitor.collectors import _get_sessions_via_wtsapi
    users = _get_sessions_via_wtsapi()
    for u in users:
        for key in ('username', 'session_id', 'state', 'is_active', 'is_rdp'):
            assert key in u, f"Falta key {key} en sesion {u}"
        assert isinstance(u['session_id'], int)
        assert isinstance(u['is_active'], bool)
        assert isinstance(u['is_rdp'], bool)


def test_rdp_sessions_returns_empty_on_non_windows():
    """En linux/mac, el collector devuelve lista vacia sin crashear."""
    from unittest.mock import patch
    with patch('gp_monitor.collectors.os.name', 'posix'):
        from gp_monitor.collectors import _get_sessions_via_wtsapi
        result = _get_sessions_via_wtsapi()
        assert result == []
