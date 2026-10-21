@echo off
chcp 65001 >nul
title ASTRA Dashboard
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" goto :run

rem --- Trouver un Python compatible avec MetaTrader5 (3.10 a 3.14, 64 bits) ---
set "PY="
for %%V in (3.14 3.13 3.12 3.11 3.10) do (
  if not defined PY py -%%V-64 -c "import sys" >nul 2>nul && set "PY=py -%%V-64"
)
if not defined PY python -c "import sys,platform;sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,14) and platform.architecture()[0]=='64bit' else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Aucune version de Python compatible n'a ete trouvee.
  echo MetaTrader 5 fonctionne uniquement avec Python 3.10 a 3.14 en 64 bits.
  echo.
  echo 1. Telecharge Python 3.14 pour Windows 64 bits avec le lien qui va s'ouvrir.
  echo 2. Pendant l'installation, coche "Add python.exe to PATH" puis "Install Now".
  echo 3. Relance ensuite ce fichier.
  start "" https://www.python.org/ftp/python/3.14.0/python-3.14.0-amd64.exe
  pause
  exit /b 1
)

echo Premiere installation, patiente une a deux minutes...
if exist ".venv" rmdir /s /q ".venv"
%PY% -m venv .venv || goto :error
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error

:run
rem --- Raccourci "ASTRA Dashboard" sur le bureau, avec l'icone de l'application ---
set "ASTRA_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$dir=$env:ASTRA_DIR; $lnk=Join-Path ([Environment]::GetFolderPath('Desktop')) 'ASTRA Dashboard.lnk'; if (-not (Test-Path $lnk)) { $s=(New-Object -ComObject WScript.Shell).CreateShortcut($lnk); $s.TargetPath=(Join-Path $dir '.venv\Scripts\pythonw.exe'); $s.Arguments='app.py'; $s.WorkingDirectory=$dir; $s.IconLocation=(Join-Path $dir 'icone.ico'); $s.Description='ASTRA CAPITAL - Dashboard des comptes'; $s.Save() }" >nul 2>nul

start "" ".venv\Scripts\pythonw.exe" app.py
exit /b 0

:error
if exist ".venv" rmdir /s /q ".venv"
echo.
echo L'installation a echoue. Fais une capture de ce message et envoie-la moi.
pause
exit /b 1
