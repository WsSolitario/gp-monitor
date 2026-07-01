@echo off
REM install.bat — Instala gp-monitor como servicio de Windows.
REM Requiere Python 3.9+ en PATH y haber ejecutado `pip install -e .` antes.

setlocal

echo === gp-monitor: instalador de servicio Windows ===

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python no esta en PATH. Instala Python 3.9+ primero.
    exit /b 1
)

if not exist config\config.yaml (
    echo [WARN] No se encontro config\config.yaml
    echo Copia config\config.example.yaml a config\config.yaml y editalo.
    echo.
    pause
)

python -m gp_monitor install
if errorlevel 1 (
    echo [ERROR] Fallo la instalacion del servicio.
    exit /b 1
)

echo.
echo Arrancando servicio...
python -m gp_monitor start

echo.
echo === Listo ===
echo El servicio gp-monitor arrancara automaticamente con Windows.
echo Comandos utiles:
echo   gp-monitor status     - ver estado
echo   gp-monitor stop       - detener
echo   gp-monitor uninstall  - desinstalar

endlocal