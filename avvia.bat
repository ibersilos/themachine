@echo off
title THE MACHINE — Avvio
color 0A
echo.
echo  ============================================
echo   THE MACHINE — Avvio sistema
echo  ============================================
echo.

:: Verifica Python
python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  [ERRORE] Python non trovato.
    echo  Esegui prima setup.bat
    pause
    exit /b 1
)

:: Verifica .env
if not exist ".env" (
    color 0E
    echo  [ATTENZIONE] File .env non trovato.
    echo  Esegui prima setup.bat
    pause
    exit /b 1
)

:: Verifica dipendenze (fastapi come indicatore)
python -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    color 0E
    echo  [ATTENZIONE] Dipendenze mancanti. Installo...
    pip install -r requirements.txt --quiet
)

echo  [OK] Controlli superati.
echo.
echo  Avvio THE MACHINE in background...
echo  (la finestra del server rimane aperta — non chiuderla)
echo.

:: Avvia il backend in una finestra separata
start "THE MACHINE - Backend" cmd /k "color 0A && echo. && echo  THE MACHINE - Backend attivo && echo  API: http://127.0.0.1:8080/api/docs && echo  Premi CTRL+C per fermare && echo. && python main.py"

:: Attendi 3 secondi che il server si avvii
echo  Attendo avvio server...
timeout /t 3 /nobreak >nul

:: Apri il dashboard nel browser predefinito
echo  Apertura dashboard...
start "" "dashboard.html"

echo.
echo  ============================================
echo   Sistema avviato!
echo.
echo   Dashboard: aperto nel browser
echo   API docs:  http://127.0.0.1:8080/api/docs
echo   Logs:      vedi finestra "Backend"
echo  ============================================
echo.
echo  Premi un tasto per chiudere questa finestra.
echo  (il backend continua a girare nella sua finestra)
pause >nul
