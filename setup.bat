@echo off
title THE MACHINE - Setup
color 0A
echo.
echo  ============================================
echo   THE MACHINE - Installazione iniziale
echo  ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  [ERRORE] Python non trovato.
    echo.
    echo  Scarica Python da: https://www.python.org/downloads/
    echo  Spunta "Add Python to PATH" durante l'installazione.
    echo.
    pause
    exit /b 1
)
echo  [OK] Python trovato.
echo.

echo  Installazione dipendenze in corso (1-2 minuti)...
echo.
pip install -r requirements.txt
if errorlevel 1 (
    color 0C
    echo.
    echo  [ERRORE] Installazione fallita.
    pause
    exit /b 1
)
echo.
echo  [OK] Dipendenze installate.
echo.

if not exist ".env" (
    copy ".env.example" ".env" >nul
    color 0E
    echo  [OK] File .env creato.
    echo.
    echo  ============================================
    echo   CONFIGURA IL FILE .env CHE SI APRE ORA
    echo  ============================================
    echo.
    echo  Compila queste due righe:
    echo    TELEGRAM_BOT_TOKEN=...  (da @BotFather)
    echo    TELEGRAM_CHAT_ID=...    (il tuo chat ID)
    echo.
    echo  Salva e chiudi Notepad, poi usa avvia.bat
    echo.
    notepad .env
) else (
    echo  [OK] File .env gia' presente.
)

echo.
color 0A
echo  ============================================
echo   Setup completato! Doppio click su avvia.bat
echo  ============================================
echo.
pause
