@echo off
title THE MACHINE — Setup
color 0A
echo.
echo  ============================================
echo   THE MACHINE — Installazione iniziale
echo  ============================================
echo.

:: Verifica Python
python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  [ERRORE] Python non trovato.
    echo.
    echo  Scarica Python da: https://www.python.org/downloads/
    echo  Assicurati di spuntare "Add Python to PATH" durante l'installazione.
    echo.
    pause
    exit /b 1
)

echo  [OK] Python trovato.
echo.
echo  Installazione dipendenze in corso...
echo  (questa operazione richiede circa 1-2 minuti)
echo.
pip install -r requirements.txt --quiet
if errorlevel 1 (
    color 0C
    echo  [ERRORE] Installazione dipendenze fallita.
    pause
    exit /b 1
)
echo  [OK] Dipendenze installate.
echo.

:: Crea .env da .env.example se non esiste
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo  [OK] File .env creato da .env.example
    echo.
    color 0E
    echo  ============================================
    echo   ATTENZIONE: configura il file .env
    echo  ============================================
    echo.
    echo  Apri il file .env con Notepad e compila:
    echo    - TELEGRAM_BOT_TOKEN   (da @BotFather su Telegram)
    echo    - TELEGRAM_CHAT_ID     (il tuo Chat ID)
    echo.
    echo  Poi riesegui avvia.bat
    echo.
    notepad .env
) else (
    echo  [OK] File .env gia' presente.
)

echo.
color 0A
echo  ============================================
echo   Setup completato! Usa avvia.bat per partire.
echo  ============================================
echo.
pause
