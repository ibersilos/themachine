@echo off
title THE MACHINE - Avvio
color 0A
echo.
echo  ============================================
echo   THE MACHINE - Avvio sistema
echo  ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  [ERRORE] Python non trovato.
    echo  Esegui prima setup.bat
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    color 0E
    echo  [ATTENZIONE] File .env non trovato.
    echo  Esegui prima setup.bat
    echo.
    pause
    exit /b 1
)

python -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    color 0E
    echo  Dipendenze mancanti, installo...
    echo.
    pip install -r requirements.txt
    echo.
)

echo  [OK] Tutto pronto.
echo.
echo  Avvio backend in una nuova finestra...
echo  (NON chiudere la finestra "Backend")
echo.

start "THE MACHINE Backend" cmd /k "color 0A && echo. && echo  THE MACHINE - Backend attivo && echo  API: http://127.0.0.1:8080/api/docs && echo. && python main.py"

echo  Attendo avvio server (3 secondi)...
timeout /t 3 /nobreak >nul

echo  Apertura dashboard nel browser...
start "" "dashboard.html"

echo.
echo  ============================================
echo   Sistema avviato!
echo.
echo   - Dashboard aperto nel browser
echo   - Backend attivo nell'altra finestra
echo   - NON chiudere la finestra Backend
echo  ============================================
echo.
pause
