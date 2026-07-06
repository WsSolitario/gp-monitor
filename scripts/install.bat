@echo off
REM install.bat — Instala gp-monitor como servicio de Windows.
REM Pre-requisito: haber corrido `pip install -e .` dentro del venv.

setlocal

echo === gp-monitor: instalador de servicio Windows ===

REM Preferir SIEMPRE el Python del venv (donde esta instalado gp_monitor).
REM Si no existe venv, caer a python del PATH (instalacion global).
set PYTHON_EXE=.\.venv\Scripts\python.exe
if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=python
    echo [INFO] venv no encontrado, usando Python del PATH.
)

REM Verificar que gp_monitor este disponible en el Python elegido.
"%PYTHON_EXE%" -c "import gp_monitor" 2>nul
if errorlevel 1 (
    echo [ERROR] gp_monitor no esta instalado en %PYTHON_EXE%.
    echo.
    echo Si acabas de clonar el repo, primero:
    echo     python -m venv .venv
    echo     .\.venv\Scripts\pip install -e .
    echo.
    echo Si instalaste Python via Chocolatey en otra ruta, asegurate de que
    echo el venv use el mismo Python donde hiciste pip install.
    exit /b 1
)

if not exist config\config.yaml (
    echo [WARN] No se encontro config\config.yaml
    echo Copia config\config.example.yaml a config\config.yaml y editalo.
    echo.
    pause
)

"%PYTHON_EXE%" -m gp_monitor install
if errorlevel 1 (
    echo [ERROR] Fallo la instalacion del servicio.
    exit /b 1
)

echo.
echo Arrancando servicio...
"%PYTHON_EXE%" -m gp_monitor start

echo.
echo === Listo ===
echo El servicio gp-monitor arrancara automaticamente con Windows.
echo Comandos utiles:
echo   gp-monitor status     - ver estado
echo   gp-monitor stop       - detener
echo   gp-monitor uninstall  - desinstalar
echo.
echo NOTA: gp-monitor es un wrapper de .venv\Scripts\gp-monitor.exe
echo Si tu PATH no incluye el venv, agregalo o usa la ruta completa.

endlocal