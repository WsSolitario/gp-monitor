@echo off
REM uninstall.bat — Detiene y desinstala el servicio gp-monitor de Windows.

setlocal

echo === gp-monitor: desinstalador de servicio Windows ===

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python no esta en PATH.
    exit /b 1
)

python -m gp_monitor uninstall
if errorlevel 1 (
    echo [ERROR] Fallo la desinstalacion.
    exit /b 1
)

echo.
echo === Listo ===
echo Para borrar configuracion persistente, elimina manualmente:
echo   C:\ProgramData\gp-monitor\

endlocal