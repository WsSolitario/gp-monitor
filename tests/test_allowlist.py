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
    # Por diseño, "Get-Process*" (prefijo) acepta tambien "Get-ProcessSpooler"
    # porque el .* del regex come cualquier continuación. Para matching estricto
    # (sin pegados), usar pattern sin * (ver test_pattern_without_asterisk_is_strict).
    assert al.is_allowed("Get-ProcessSpooler") is True

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
pattern = "Bad-Pattern with [special] chars"
description = "Con caracteres especiales (no rompe)"
""", encoding="utf-8")

    al = CommandAllowlist.load(explicit_path=p)
    # Los 3 son validos: re.escape() escapa [ y ], asi que no hay regex invalido.
    assert len(al) == 3

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
    # Solo relevante cuando NO estamos en Windows. En Windows, _wtsapi32
    # esta cacheado por otros tests y el mock no tiene sentido.
    import platform
    if platform.system() == 'Windows':
        # En Windows verificamos que _wtsapi32 fue cacheado (sistema OK)
        from gp_monitor.collectors import _wtsapi32
        assert _wtsapi32 is not None
        return
    with patch('gp_monitor.collectors.os.name', 'posix'), \
         patch('gp_monitor.collectors._wtsapi32', None):
        from gp_monitor.collectors import _get_sessions_via_wtsapi
        result = _get_sessions_via_wtsapi()
        assert result == []


def test_wtsapi_signature_is_correct():
    """El signature de WTSEnumerateSessionsW debe estar bien declarado.

    Verifica que el arg 2 (Reserved) sea DWORD (ctypes.c_uint32) y no
    puntero, que era el bug que crasheaba el agente con
    'argument 2: TypeError: expected LP_c_ulong instance instead of int'.
    """
    import platform
    if platform.system() != 'Windows':
        return
    import ctypes
    from gp_monitor.collectors import _init_wtsapi, _WTS_SESSION_INFOW
    lib = _init_wtsapi()
    assert lib is not None
    fn = lib.WTSEnumerateSessionsW
    # arg 1: c_void_p (HANDLE)
    assert fn.argtypes[0] is ctypes.c_void_p
    # arg 2: c_uint32 (Reserved, value, no puntero) -- bug fix critico
    # Antes era c_void_p / puntero, lo que causaba TypeError en runtime.
    assert fn.argtypes[1] is ctypes.c_uint32 or fn.argtypes[1] is ctypes.c_ulong
    # arg 3: c_uint32 (Version, value)
    assert fn.argtypes[2] is ctypes.c_uint32 or fn.argtypes[2] is ctypes.c_ulong
    # arg 4: puntero a puntero de WTS_SESSION_INFOW
    assert fn.argtypes[3] == ctypes.POINTER(ctypes.POINTER(_WTS_SESSION_INFOW))
    # arg 5: puntero a DWORD (count) -- no un c_uint32 solo!
    assert fn.argtypes[4] == ctypes.POINTER(ctypes.c_uint32)

    # Verifica que llamar NO crashee con 'expected LP_c_ulong instance instead of int'
    pp_session = ctypes.POINTER(_WTS_SESSION_INFOW)()
    p_count = ctypes.c_uint32(0)
    result = fn(
        ctypes.c_void_p(0),    # hServer = current
        ctypes.c_uint32(0),    # Reserved = 0 (value, no puntero)
        ctypes.c_uint32(1),    # Version = 1
        ctypes.byref(pp_session),
        ctypes.byref(p_count),
    )
    # result puede ser 0 (fallo) o != 0 (exito) -- lo importante es que
    # no haya crasheado al armar la call.
    assert isinstance(result, int)
    # Liberar la memoria si se asigno
    if pp_session:
        lib.WTSFreeMemory(pp_session)


# ─── Tests del fix: sesiones activas con username vacio (Session 0) ──────────

def test_active_session_with_empty_username_is_included():
    """Sesion activa (sduarte via RDP, sid=41) NO debe skipearse solo porque
    WTSQuerySessionInformationW devuelve '' desde Session 0. Esto era el bug
    que ocultaba los usuarios activos en el dashboard.

    Caso bug: usuario conectado por RDP, agente corre como servicio en Session 0,
    WTSAPI devuelve username vacio para la sesion activa -> skip -> usuario no aparece.
    """
    import platform
    if platform.system() != 'Windows':
        return  # skip en linux/mac
    import ctypes
    from unittest.mock import patch

    from gp_monitor import collectors
    from gp_monitor.collectors import _get_sessions_via_wtsapi, _WTS_SESSION_INFOW

    # Sesion activa (sduarte via RDP): sid=41, RDP-Tcp#5, WTSActive(0)
    fake_session = _WTS_SESSION_INFOW(SessionId=41, pWinStationName='RDP-Tcp#5', State=0)

    class FakeLib:
        def WTSFreeMemory(self, _):
            pass

    def fake_wtsenum(_self, _handle, _res, _ver, pp, pc):
        # El parametro `pp` es un CArgObject (address-of-pointer).
        # Escribimos en esa direccion la direccion de nuestro array fake.
        ptr_addr = ctypes.cast(pp, ctypes.POINTER(ctypes.c_void_p))
        ptr_addr[0] = ctypes.cast(ctypes.pointer(fake_session), ctypes.c_void_p).value
        pc_addr = ctypes.cast(pc, ctypes.POINTER(ctypes.c_uint32))
        pc_addr[0] = 1
        return 1

    FakeLib.WTSEnumerateSessionsW = fake_wtsenum

    with patch.object(collectors, '_init_wtsapi', return_value=FakeLib()), \
         patch.object(collectors, '_wts_query_string', return_value=''), \
         patch.object(collectors, '_wts_query_client_address', return_value='192.168.1.100'), \
         patch.object(collectors, '_wts_query_logon_time', return_value=None), \
         patch.object(collectors, '_resolve_username_from_psutil', return_value='sduarte'):
        sessions = _get_sessions_via_wtsapi()
        # Antes del fix: la sesion se skipeaba por `if not username: continue`
        # Ahora: debe aparecer con username 'sduarte' (via fallback psutil)
        assert len(sessions) == 1
        s = sessions[0]
        assert s['session_id'] == 41
        assert s['username'] == 'sduarte'
        assert s['is_active'] is True
        assert s['is_rdp'] is True
        assert s['state'] == 'Active'


def test_listener_session_is_still_filtered():
    """El listener RDP-Tcp (sin usuario real) SI debe filtrarse por win_station."""
    import platform
    if platform.system() != 'Windows':
        return
    import ctypes
    from unittest.mock import patch

    from gp_monitor import collectors
    from gp_monitor.collectors import _get_sessions_via_wtsapi, _WTS_SESSION_INFOW

    fake_session = _WTS_SESSION_INFOW(SessionId=65536, pWinStationName='RDP-Tcp', State=6)

    class FakeLib:
        def WTSFreeMemory(self, _):
            pass

    def fake_wtsenum(_self, _handle, _res, _ver, pp, pc):
        ptr_addr = ctypes.cast(pp, ctypes.POINTER(ctypes.c_void_p))
        ptr_addr[0] = ctypes.cast(ctypes.pointer(fake_session), ctypes.c_void_p).value
        pc_addr = ctypes.cast(pc, ctypes.POINTER(ctypes.c_uint32))
        pc_addr[0] = 1
        return 1

    FakeLib.WTSEnumerateSessionsW = fake_wtsenum

    with patch.object(collectors, '_init_wtsapi', return_value=FakeLib()), \
         patch.object(collectors, '_wts_query_string', return_value=''), \
         patch.object(collectors, '_wts_query_client_address', return_value=''), \
         patch.object(collectors, '_wts_query_logon_time', return_value=None), \
         patch.object(collectors, '_resolve_username_from_psutil', return_value=''):
        sessions = _get_sessions_via_wtsapi()
        # El listener debe filtrarse por win_station.startswith('rdp-tcp')
        assert len(sessions) == 0


def test_services_session_is_still_filtered():
    """Sesion 'Services' (Session 0, servicios Windows) debe filtrarse por win_station."""
    import platform
    if platform.system() != 'Windows':
        return
    import ctypes
    from unittest.mock import patch

    from gp_monitor import collectors
    from gp_monitor.collectors import _get_sessions_via_wtsapi, _WTS_SESSION_INFOW

    fake_session = _WTS_SESSION_INFOW(SessionId=0, pWinStationName='Services', State=0)

    class FakeLib:
        def WTSFreeMemory(self, _):
            pass

    def fake_wtsenum(_self, _handle, _res, _ver, pp, pc):
        ptr_addr = ctypes.cast(pp, ctypes.POINTER(ctypes.c_void_p))
        ptr_addr[0] = ctypes.cast(ctypes.pointer(fake_session), ctypes.c_void_p).value
        pc_addr = ctypes.cast(pc, ctypes.POINTER(ctypes.c_uint32))
        pc_addr[0] = 1
        return 1

    FakeLib.WTSEnumerateSessionsW = fake_wtsenum

    with patch.object(collectors, '_init_wtsapi', return_value=FakeLib()), \
         patch.object(collectors, '_wts_query_string', return_value='SYSTEM'), \
         patch.object(collectors, '_wts_query_client_address', return_value=''), \
         patch.object(collectors, '_wts_query_logon_time', return_value=None):
        sessions = _get_sessions_via_wtsapi()
        # Session 0 'Services' debe filtrarse
        assert len(sessions) == 0


# ─── Tests del fix: UnicodeEncodeError en consola Windows cp1252 ───────────

def test_safe_print_handles_unicode():
    """_safe_print debe tolerar UnicodeEncodeError sin crashear.

    Bug: en Windows con codificacion cp1252 (la default), print('✓ ...')
    lanzaba UnicodeEncodeError y abortaba el script (caso 'start' del agente).
    """
    import sys
    from gp_monitor.windows_service import _safe_print

    # Caso 1: stream que NO soporta unicode (simulado)
    class FakeCp1252Stream:
        def __init__(self):
            self.encoding = 'cp1252'
        def write(self, s):
            # cp1252 no puede codificar U+2713 (✓)
            s.encode('cp1252')
            return len(s)

    fake = FakeCp1252Stream()
    orig = sys.stdout
    sys.stdout = fake
    try:
        # Debe hacer fallback a ASCII sin crashear
        _safe_print("\u2713 Servicio 'gp-monitor' arrancado.")
        _safe_print("\u2717 Error cargando configuracion: foo")
    finally:
        sys.stdout = orig
    # Si llego aqui sin excepcion, el test pasa


def test_configure_utf8_io_runs():
    """_configure_utf8_io debe reconfigurar stdout/stderr a UTF-8 sin crashear."""
    import sys
    from gp_monitor.__main__ import _configure_utf8_io

    # Llamar no debe lanzar excepciones
    _configure_utf8_io()
    _configure_utf8_io()  # idempotente

    # El stdout/stderr deben seguir funcionando normalmente
    print("test print after _configure_utf8_io")
    print("\u2713 unicode print after _configure_utf8_io")
