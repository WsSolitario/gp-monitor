<#
.SYNOPSIS
    Actualiza gp-monitor en un server Windows desde la rama release/v1.0.0.

.DESCRIPTION
    Hace pull, reinstala, reinicia el servicio y verifica la nueva version.

    Caracteristicas:
      - Pre-flight checks (admin, git limpio, venv existe).
      - Backup automatico del estado actual (para rollback).
      - Modo -DryRun para simular sin tocar nada.
      - Modo -Rollback para volver a la version anterior.
      - Loguea todo a C:\ProgramData\gp-monitor\update.log.
      - Verifica el heartbeat post-instalacion (linea "RDP:" en el log).

.PARAMETER TargetVersion
    Branch o tag a bajar. Default: origin/release/v1.0.0

.PARAMETER DryRun
    Solo muestra lo que haria, sin ejecutar cambios.

.PARAMETER Rollback
    Revierte a la version anterior (la que estaba antes del ultimo update).

.PARAMETER SkipPostCheck
    Salta la verificacion post-instalacion (heartbeat + RDP line).

.PARAMETER RestartDelaySeconds
    Segundos a esperar despues de reiniciar el servicio. Default: 65
    (un heartbeat + margen). Subir si tu heartbeat_interval_seconds es mayor.

.EXAMPLE
    .\scripts\update-agent.ps1
    .\scripts\update-agent.ps1 -DryRun
    .\scripts\update-agent.ps1 -Rollback
    .\scripts\update-agent.ps1 -TargetVersion origin/main
#>

[CmdletBinding()]
param(
    [string]$TargetVersion = "origin/release/v1.0.0",
    [switch]$DryRun,
    [switch]$Rollback,
    [switch]$SkipPostCheck,
    [int]$RestartDelaySeconds = 65
)

$ErrorActionPreference = "Stop"

# Wrapper para git: git escribe 'From https://...' a stderr en cada fetch,
# lo que PowerShell intercepta como NativeCommandError y aborta el script
# aunque el comando haya tenido exito. Este helper baja temporalmente
# $ErrorActionPreference a 'Continue' para que no se transforme stderr en
# error fatal, y solo propaga el error si el exit code fue != 0.
function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git @Args 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldEAP
    }
    if ($exitCode -ne 0) {
        throw "git $Args fallo (exit=$exitCode): $($output -join "`n")"
    }
    return $output
}

# Wrapper generico para procesos externos (git, python, etc.) que escriben
# a stderr incluso en exito. Igual que Invoke-Git pero sin asumir git.
# Devuelve el output completo (stdout+stderr mergeados) y propaga throw
# solo si exit code != 0.
function Invoke-Exe {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments=$true)][string[]]$Args
    )
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $FilePath @Args 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldEAP
    }
    if ($exitCode -ne 0) {
        throw "$FilePath $Args fallo (exit=$exitCode): $($output -join "`n")"
    }
    return $output
}

# ─── Paths y constantes ─────────────────────────────────────────────────────
$Script:InstallDir   = "C:\Tools\gp-monitor"
$Script:VenvPython   = Join-Path $InstallDir ".venv\Scripts\python.exe"
$Script:StateDir     = "C:\ProgramData\gp-monitor"
$Script:LogFile      = Join-Path $StateDir "update.log"
$Script:AgentLog     = Join-Path $StateDir "gp-monitor.log"
$Script:BackupFile   = Join-Path $StateDir ".update-backup.json"

# ─── Funciones de logging ───────────────────────────────────────────────────

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
        Add-Content -Path $LogFile -Value $line
    }
}

function Write-Banner {
    param([string]$Text)
    $bar = "=" * 70
    Write-Host ""
    Write-Host $bar
    Write-Host "  $Text"
    Write-Host $bar
}

# ─── Pre-flight checks ─────────────────────────────────────────────────────

Write-Banner "gp-monitor update agent"

if ($Rollback) {
    Write-Log "Modo ROLLBACK activado"
} elseif ($DryRun) {
    Write-Log "Modo DRY-RUN activado (no se haran cambios)"
} else {
    Write-Log "Iniciando update hacia $TargetVersion"
}

# Check 1: Admin
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Log "Este script requiere PowerShell como Administrator." "ERROR"
    exit 1
}
Write-Log "OK: corriendo como Administrator"

# Check 2: Install dir existe
if (-not (Test-Path $InstallDir)) {
    Write-Log "No se encuentra $InstallDir. Ajusta `$InstallDir o instala primero el agente." "ERROR"
    exit 1
}
Write-Log "OK: install dir existe ($InstallDir)"

# Check 3: Venv existe
if (-not (Test-Path $VenvPython)) {
    Write-Log "No se encuentra $VenvPython. Crea el venv primero." "ERROR"
    exit 1
}
Write-Log "OK: venv existe"

# Check 4: git
try {
    $gitVersion = Invoke-Exe -FilePath "git" -Args "--version"
    Write-Log "OK: git ($($gitVersion -join ' '))"
} catch {
    Write-Log "git no esta instalado o no esta en PATH." "ERROR"
    exit 1
}

# Check 5: gp-monitor esta corriendo como servicio?
$runningProcs = Get-Process python* -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*pythonservice*" }
Write-Log "Procesos pythonservice activos: $($runningProcs.Count)"

# ─── ROLLBACK path ──────────────────────────────────────────────────────────

if ($Rollback) {
    if (-not (Test-Path $BackupFile)) {
        Write-Log "No hay backup en $BackupFile. Nada que revertir." "ERROR"
        exit 1
    }
    $backup = Get-Content $BackupFile -Raw | ConvertFrom-Json
    Write-Log "Revirtiendo a $($backup.commit) (version $($backup.version))"

    Push-Location $InstallDir
    try {
        Invoke-Git reset --hard $backup.commit | Out-Null
        Write-Log "OK: git reset a $($backup.commit)"
    } finally {
        Pop-Location
    }

    Invoke-Exe -FilePath $VenvPython -Args @("-m", "pip", "install", "-e", ".") | Out-Null
    Write-Log "OK: pip install -e ."

    $runningProcs | Stop-Process -Force
    Start-Sleep -Seconds 5
    try {
        $rbOutput = Invoke-Exe -FilePath $VenvPython -Args @("-m", "gp_monitor", "start")
    } catch {
        Write-Log "Rollback: fallo al arrancar agente: $_" "ERROR"
        exit 1
    }
    Write-Log "OK: agente reiniciado en version $($backup.version)"
    Write-Banner "Rollback completo a $($backup.version). Verificar log: $AgentLog"
    exit 0
}

# ─── UPDATE path ────────────────────────────────────────────────────────────

Push-Location $InstallDir
try {
    # Check 6: git working tree limpio (sin cambios sin commitear)
    $gitStatus = Invoke-Git status --porcelain
    if ($gitStatus) {
        Write-Log "Hay cambios sin commitear en el repo:" "WARN"
        $gitStatus | ForEach-Object { Write-Log "  $_" "WARN" }
        if (-not $DryRun) {
            $answer = Read-Host "Continuar de todas formas? (s/N)"
            if ($answer -ne "s" -and $answer -ne "S") {
                Write-Log "Abortado por el usuario"
                exit 1
            }
        }
    } else {
        Write-Log "OK: git working tree limpio"
    }

    # Check 7: branch actual y version antes
    $currentBranch = Invoke-Git rev-parse --abbrev-ref HEAD
    $currentCommit = Invoke-Git rev-parse --short HEAD
    $currentVersion = Select-String -Path "pyproject.toml" -Pattern '^version' | ForEach-Object { ($_ -split '"')[1] }
    Write-Log "Estado actual: branch=$currentBranch commit=$currentCommit version=$currentVersion"

    # Backup state (para rollback)
    if (-not $DryRun) {
        $backupObj = @{
            commit   = $currentCommit
            version  = $currentVersion
            branch   = $currentBranch
            backedAt = (Get-Date).ToString("o")
        } | ConvertTo-Json
        Set-Content -Path $BackupFile -Value $backupObj -Encoding UTF8
        Write-Log "Backup guardado en $BackupFile"
    }

    # git fetch + reset al target
    Write-Log "git fetch origin..."
    if (-not $DryRun) {
        Invoke-Git fetch origin | Out-Null
    }

    Write-Log "git reset --hard $TargetVersion..."
    if (-not $DryRun) {
        Invoke-Git reset --hard $TargetVersion | Out-Null
    }

    # nueva version
    $newVersion = Select-String -Path "pyproject.toml" -Pattern '^version' | ForEach-Object { ($_ -split '"')[1] }
    $newCommit = Invoke-Git rev-parse --short HEAD
    Write-Log "Estado nuevo: commit=$newCommit version=$newVersion"

    if ($DryRun) {
        Write-Banner "DRY-RUN: no se aplicaron cambios. Commit=$newCommit Version=$newVersion"
        exit 0
    }

    # pip install -e .
    Write-Log "pip install -e . ..."
    try {
        Invoke-Exe -FilePath $VenvPython -Args @("-m", "pip", "install", "-e", ".") | Out-Null
    } catch {
        Write-Log "pip install fallo: $_" "ERROR"
        Write-Log "Iniciando rollback automatico..." "WARN"
        & $PSCommandPath -Rollback
        exit 1
    }
    Write-Log "OK: pip install"

    # Detener servicio (matar pythonservice)
    Write-Log "Deteniendo servicio (kill pythonservice)..."
    $procsBefore = Get-Process python* -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*pythonservice*" }
    $procsBefore | Stop-Process -Force
    Start-Sleep -Seconds 5
    $procsAfter = Get-Process python* -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*pythonservice*" }
    if ($procsAfter) {
        Write-Log "Aun quedan procesos pythonservice. Forzando kill..." "WARN"
        $procsAfter | Stop-Process -Force
        Start-Sleep -Seconds 3
    }
    Write-Log "OK: procesos eliminados"

    # Iniciar servicio
    Write-Log "Iniciando agente..."
    try {
        $startOutput = Invoke-Exe -FilePath $VenvPython -Args @("-m", "gp_monitor", "start")
        # El output incluye '✓ Servicio arrancado.' si fue exitoso
        Write-Log "OK: agente arrancado ($($startOutput -join ' '))"
    } catch {
        Write-Log "Fallo al arrancar el agente: $_" "ERROR"
        Write-Log "Para diagnostico manual:" "ERROR"
        Write-Log "  $VenvPython -m gp_monitor start" "ERROR"
        exit 1
    }

    # Post-check
    if (-not $SkipPostCheck) {
        Write-Banner "Esperando $RestartDelaySeconds segundos para el primer heartbeat..."
        Start-Sleep -Seconds $RestartDelaySeconds

        if (-not (Test-Path $AgentLog)) {
            Write-Log "No se encontro $AgentLog. Algo salio mal." "ERROR"
            exit 1
        }

        $lastLines = Get-Content $AgentLog -Tail 20
        Write-Log "Ultimas 20 lineas del log del agente:"
        $lastLines | ForEach-Object { Write-Log "  | $_" }

        # Verificar version
        if ($lastLines -match "v$newVersion") {
            Write-Log "OK: log confirma version $newVersion"
        } else {
            Write-Log "No se encontro 'v$newVersion' en el log reciente." "WARN"
        }

        # Verificar RDP line (con el fix nuevo: "X users (Y active, Z RDP)" donde Y > 0)
        $rdpLine = $lastLines | Select-String -Pattern "RDP: \d+ users \(\d+ active"
        if ($rdpLine) {
            Write-Log "OK: linea RDP encontrada: $($rdpLine.Line.Trim())"
        } else {
            Write-Log "No se encontro linea 'RDP: ...' en el log reciente." "WARN"
        }

        # Verificar heartbeat OK
        $hbLine = $lastLines | Select-String -Pattern "Heartbeat OK"
        if ($hbLine) {
            Write-Log "OK: heartbeat aceptado"
        } else {
            Write-Log "No se encontro 'Heartbeat OK'. Puede haber fallo de red." "WARN"
        }
    }

    Write-Banner "Update completo: $currentVersion -> $newVersion. Si algo falla: .\scripts\update-agent.ps1 -Rollback"
} finally {
    Pop-Location
}
