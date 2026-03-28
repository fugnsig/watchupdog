@echo off
setlocal enabledelayedexpansion

:: ============================================================
::  watchupdog — Windows launcher
::  Drop this bat anywhere. It will find Python and ComfyUI
::  automatically, or let you override below.
:: ============================================================

:: ── Optional overrides (leave blank for auto-detect) ────────
set PYTHON_OVERRIDE=
set COMFYUI_OVERRIDE=
:: ────────────────────────────────────────────────────────────

set MONITOR_DIR=%~dp0
set PYTHON=
set COMFYUI_PATH=

:: Tell Python to use UTF-8 for all I/O (emoji in node/model names won't crash)
:: Do NOT use chcp 65001 — it breaks for/f loops in cmd.exe
set PYTHONUTF8=1

:: ── 1. Find Python ───────────────────────────────────────────
if not "%PYTHON_OVERRIDE%"=="" (
    if exist "%PYTHON_OVERRIDE%" (
        set PYTHON=%PYTHON_OVERRIDE%
        goto python_found
    )
)

:: Check active virtual environment / conda environment first.
:: Users who run the bat from an already-activated env (conda activate comfyui,
:: source .venv/Scripts/activate, etc.) should get that Python immediately.
if not "%VIRTUAL_ENV%"=="" (
    if exist "%VIRTUAL_ENV%\Scripts\python.exe" (
        set PYTHON=%VIRTUAL_ENV%\Scripts\python.exe
        goto python_found
    )
)
if not "%CONDA_PREFIX%"=="" (
    if exist "%CONDA_PREFIX%\python.exe" (
        set PYTHON=%CONDA_PREFIX%\python.exe
        goto python_found
    )
)
:: CONDA_EXE is set by conda itself regardless of which env is active.
:: Derive the base prefix from it (strip \Scripts\conda.exe or \condabin\conda.exe).
if not "%CONDA_EXE%"=="" (
    for %%E in ("%CONDA_EXE%") do set _CONDA_BASE=%%~dpE..
    if exist "!_CONDA_BASE!\python.exe" ( set PYTHON=!_CONDA_BASE!\python.exe & goto python_found )
)

:: Use a temp file to capture python.exe path — avoids for/f quoting issues
set _TMPPYEXE=%TEMP%\comfyui_pyexe.txt

:: Try py launcher (standard Windows Python installer)
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; print(sys.executable)" > "%_TMPPYEXE%" 2>nul
    set /p PYTHON= < "%_TMPPYEXE%"
    del "%_TMPPYEXE%" >nul 2>&1
    if not "!PYTHON!"=="" goto python_found
)

:: Try python in PATH
where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; print(sys.executable)" > "%_TMPPYEXE%" 2>nul
    set /p PYTHON= < "%_TMPPYEXE%"
    del "%_TMPPYEXE%" >nul 2>&1
    if not "!PYTHON!"=="" goto python_found
)

:: Try python3 in PATH
where python3 >nul 2>&1
if not errorlevel 1 (
    python3 -c "import sys; print(sys.executable)" > "%_TMPPYEXE%" 2>nul
    set /p PYTHON= < "%_TMPPYEXE%"
    del "%_TMPPYEXE%" >nul 2>&1
    if not "!PYTHON!"=="" goto python_found
)

:: Scan standard install locations — 64-bit then 32-bit, newest first
:: Python installer puts 64-bit under Python3XX, 32-bit under Python3XX-32
for %%V in (316 315 314 313 312 311 310 309) do (
    :: 64-bit user install
    set _TRY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe
    if exist "!_TRY!" ( set PYTHON=!_TRY! & goto python_found )
    :: 64-bit system install
    set _TRY=C:\Program Files\Python%%V\python.exe
    if exist "!_TRY!" ( set PYTHON=!_TRY! & goto python_found )
    :: Legacy C:\Python3XX location
    set _TRY=C:\Python%%V\python.exe
    if exist "!_TRY!" ( set PYTHON=!_TRY! & goto python_found )
    :: 32-bit user install (%%V-32 suffix)
    set _TRY=%LOCALAPPDATA%\Programs\Python\Python%%V-32\python.exe
    if exist "!_TRY!" ( set PYTHON=!_TRY! & goto python_found )
    :: 32-bit system install
    set _TRY=C:\Program Files (x86)\Python%%V-32\python.exe
    if exist "!_TRY!" ( set PYTHON=!_TRY! & goto python_found )
    :: ARM64 install (Windows on ARM)
    set _TRY=%LOCALAPPDATA%\Programs\Python\Python%%V-arm64\python.exe
    if exist "!_TRY!" ( set PYTHON=!_TRY! & goto python_found )
)

:: Try Conda / Miniconda / Mamba / Miniforge environments
for %%B in (
    "%USERPROFILE%\miniconda3"
    "%USERPROFILE%\anaconda3"
    "%USERPROFILE%\mambaforge"
    "%USERPROFILE%\miniforge3"
    "C:\ProgramData\miniconda3"
    "C:\ProgramData\anaconda3"
    "C:\ProgramData\miniforge3"
) do (
    if exist "%%~B\python.exe"                      ( set PYTHON=%%~B\python.exe                      & goto python_found )
    if exist "%%~B\envs\comfyui\python.exe"         ( set PYTHON=%%~B\envs\comfyui\python.exe         & goto python_found )
    if exist "%%~B\envs\comfyui3\python.exe"        ( set PYTHON=%%~B\envs\comfyui3\python.exe        & goto python_found )
)

pause & exit /b 1

:python_found
:: ── Version check — must be 3.9+ ────────────────────────────────────────────
set _TMPVER=%TEMP%\comfyui_pyver.txt
"%PYTHON%" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" > "%_TMPVER%" 2>nul
set /p _PYVER= < "%_TMPVER%"
del "%_TMPVER%" >nul 2>&1

"%PYTHON%" -c "import sys; exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
if errorlevel 1 (
    pause & exit /b 1
)

:: Warn if 32-bit Python (torch/ComfyUI require 64-bit)
set _TMPARCHI=%TEMP%\comfyui_archi.txt
"%PYTHON%" -c "import struct; print(struct.calcsize('P') * 8)" > "%_TMPARCHI%" 2>nul
set /p _PYBITS= < "%_TMPARCHI%"
del "%_TMPARCHI%" >nul 2>&1
if "!_PYBITS!"=="32" (
    pause
)

:: ── 2. pip available? ────────────────────────────────────────
"%PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 (
    "%PYTHON%" -m ensurepip --upgrade >nul 2>&1
    if errorlevel 1 ( pause & exit /b 1 )
)

:: Strip trailing backslash from MONITOR_DIR early so all pip install -e calls
:: use a clean path. "path\" makes Windows C-runtime treat \" as an escaped
:: quote, corrupting the argument passed to pip.
set "_MDIR=%MONITOR_DIR%"
if "!_MDIR:~-1!"=="\" set "_MDIR=!_MDIR:~0,-1!"

:: ── 3. Core deps for the tool itself ─────────────────────────
"%PYTHON%" -c "import rich, click, httpx, pydantic" >nul 2>&1
if errorlevel 1 (
    "%PYTHON%" -m pip install rich click "httpx>=0.27" "pydantic>=2.0" psutil tomli --quiet 2>nul
    "%PYTHON%" -c "import rich, click, httpx, pydantic" >nul 2>&1
    if errorlevel 1 (
        "%PYTHON%" -m pip install rich click "httpx>=0.27" "pydantic>=2.0" psutil tomli --user --quiet 2>nul
    )
    "%PYTHON%" -c "import rich, click, httpx, pydantic" >nul 2>&1
    if errorlevel 1 ( pause & exit /b 1 )
)

:: ── 4. watchupdog installed? ─────────────────────────────
"%PYTHON%" -c "import watchupdog" >nul 2>&1
if errorlevel 1 (
    "%PYTHON%" -m pip install -e "!_MDIR!" --quiet 2>nul
    "%PYTHON%" -c "import watchupdog" >nul 2>&1
    if errorlevel 1 (
        "%PYTHON%" -m pip install -e "!_MDIR!" --user --quiet 2>nul
    )
    "%PYTHON%" -c "import watchupdog" >nul 2>&1
    if errorlevel 1 ( pause & exit /b 1 )
)

:: ── 5. Find ComfyUI ───────────────────────────────────────────
if not "%COMFYUI_OVERRIDE%"=="" (
    if exist "%COMFYUI_OVERRIDE%\main.py" (
        set COMFYUI_PATH=%COMFYUI_OVERRIDE%
        goto comfyui_found
    )
)

set _TMPOUT=%TEMP%\comfyui_found.txt
"%PYTHON%" "%MONITOR_DIR%find_comfyui.py" > "%_TMPOUT%" 2>nul
set /p COMFYUI_PATH= < "%_TMPOUT%"
del "%_TMPOUT%" >nul 2>&1

if not "!COMFYUI_PATH!"=="" goto comfyui_skip

set /p COMFYUI_PATH=Enter full path to your ComfyUI folder (or Enter to skip):
set /p _PORTINPUT=Enter ComfyUI port if different from 8188 (or Enter to keep):
if not "!_PORTINPUT!"=="" set COMFYUI_URL=http://127.0.0.1:!_PORTINPUT!

:comfyui_skip

:: ── Launch interactive menu ───────────────────────────────────
:comfyui_found

:: Read URL from watchupdog.toml if present — leave empty if not configured
:: (Python will probe all common ports and detect the live instance automatically)
set COMFYUI_URL=
set _TMPCFG=%TEMP%\comfyui_url.txt
"%PYTHON%" -c "import pathlib;p=pathlib.Path(r'%MONITOR_DIR%watchupdog.toml');print(next((l.split('=',1)[1].strip().strip(chr(34)).strip(chr(39)) for l in (p.read_text().splitlines() if p.exists() else []) if l.strip().startswith('url') and '=' in l),''))" > "%_TMPCFG%" 2>nul
set /p _CFGURL= < "%_TMPCFG%"
del "%_TMPCFG%" >nul 2>&1
if not "!_CFGURL!"=="" set COMFYUI_URL=!_CFGURL!

:: Strip trailing backslash from COMFYUI_PATH — same reason as _MDIR above.
:: "path\" makes the C-runtime treat \" as an escaped quote, corrupting the
:: --comfyui-path argument passed to Python on the :menu line.
if not "!COMFYUI_PATH!"=="" (
    if "!COMFYUI_PATH:~-1!"=="\" set "COMFYUI_PATH=!COMFYUI_PATH:~0,-1!"
)

:menu
"%PYTHON%" -m watchupdog.interactive_menu --url "!COMFYUI_URL!" --monitor-dir "!_MDIR!" --comfyui-path "!COMFYUI_PATH!" --launcher "%~f0"

:quit
choice /c RX /n /m "  R = Relaunch   X = Exit: "
if errorlevel 2 goto :done
goto menu
:done
endlocal
