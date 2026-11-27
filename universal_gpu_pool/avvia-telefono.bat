@echo off
rem Windows: avvia la webapp raggiungibile anche dal telefono (stessa Wi-Fi)
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run_webapp.py --lan %*
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python run_webapp.py --lan %*
  ) else (
    echo Python 3 non trovato. Installalo da https://www.python.org/downloads/
  )
)
pause
