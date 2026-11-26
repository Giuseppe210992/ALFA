@echo off
rem Windows: doppio clic su questo file per avviare la webapp
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run_webapp.py %*
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python run_webapp.py %*
  ) else (
    echo Python 3 non trovato. Installalo da https://www.python.org/downloads/ ^(spunta "Add python.exe to PATH"^) e riprova.
  )
)
pause
