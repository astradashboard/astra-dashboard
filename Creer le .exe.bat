@echo off
chcp 65001 >nul
title Creation de ASTRA Dashboard.exe
cd /d "%~dp0"

set "PY="
for %%V in (3.14 3.13 3.12 3.11 3.10) do (
  if not defined PY py -%%V-64 -c "import sys" >nul 2>nul && set "PY=py -%%V-64"
)
if not defined PY python -c "import sys,platform;sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,14) and platform.architecture()[0]=='64bit' else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Aucune version compatible de Python : installe Python 3.14 64 bits puis relance.
  start "" https://www.python.org/ftp/python/3.14.0/python-3.14.0-amd64.exe
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || goto :error

set ICON=
if exist "icone.ico" set ICON=--icon icone.ico

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "ASTRA Dashboard" %ICON% ^
  --add-data "static;static" ^
  --add-data "icone.ico;." ^
  app.py || goto :error

echo.
echo Termine ! Le fichier est ici : dist\ASTRA Dashboard.exe
echo C'est ce fichier que tu peux envoyer aux membres de ton accompagnement.
explorer dist
pause
exit /b 0

:error
echo La creation a echoue. Copie le message ci-dessus et envoie-le moi.
pause
exit /b 1
