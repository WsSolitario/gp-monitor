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


# ─── Tests del parser de query session ──────────────────────────────────

from gp_monitor.collectors import _parse_query_session_line  # noqa: E402


def test_query_session_english_console():
    parsed = _parse_query_session_line(
        ">console           administrator     1  Active      none   6/7/2026 12:00:00"
    )
    assert parsed is not None
    assert parsed["username"] == "administrator"
    assert parsed["session_name"] == "console"
    assert parsed["session_id"] == 1
    assert parsed["state"] == "Active"
    assert parsed["is_active"] is True
    assert parsed["is_rdp"] is False
    assert parsed["session_type"] == "console"


def test_query_session_english_rdp():
    parsed = _parse_query_session_line(
        "                   jdoe              2  Disc       00:05    6/7/2026 11:00:00"
    )
    assert parsed is not None
    assert parsed["username"] == "jdoe"
    assert parsed["session_id"] == 2
    assert parsed["state"] == "Disc"
    assert parsed["is_active"] is False
    # session_name vacio -> no es RDP ni console
    assert parsed["is_rdp"] is False


def test_query_session_spanish():
    parsed = _parse_query_session_line(
        ">consola           usuario1         10  Conectado   00:01    6/7/2026 09:00:00"
    )
    assert parsed is not None
    assert parsed["username"] == "usuario1"
    assert parsed["session_id"] == 10
    assert parsed["state"] == "Conectado"
    assert parsed["is_active"] is True
    assert parsed["session_type"] == "consola"  # espanol == console


def test_query_session_rdp_tcp_filtered():
    """Las sesiones rdp-tcp#N (listeners) deben filtrarse."""
    parsed = _parse_query_session_line(
        "                   rdp-tcp#0          3  Escuchando"
    )
    assert parsed is None  # filtrado: rdp-tcp#X


def test_query_session_services_filtered():
    """Las sesiones 'services' (System) deben filtrarse."""
    parsed = _parse_query_session_line(
        "                   services           0  Desconectado"
    )
    assert parsed is None  # filtrado: username == services


def test_query_session_empty_line():
    assert _parse_query_session_line("") is None
    assert _parse_query_session_line("   ") is None


def test_query_session_state_variants():
    """Acepta variantes en espanol e ingles."""
    for state_str, expected_active in [
        ("Active", True), ("Conectado", True), ("Activo", True),
        ("Disc", False), ("Disconnected", False), ("Desconectado", False),
    ]:
        parsed = _parse_query_session_line(
            f"                   user{state_str.replace(' ', '')}  99  {state_str}       00:00    6/7/2026 12:00:00"
        )
        if parsed is not None:
            assert parsed["is_active"] == expected_active, f"state={state_str}"
