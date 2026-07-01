@echo off
cd /d "%~dp0"
title WhatsApp Estatus Bot (+ Cloudflare Tunnel)

echo ===============================================
echo    Bot de WhatsApp  -  Estatus Siebel
echo ===============================================
echo.

REM Dependencias (instala SOLO si faltan)
py -c "import fastapi, uvicorn, httpx, dotenv" 2>nul || (
  echo Instalando dependencias...
  py -m pip install -r requirements.txt
)

REM Aviso: en modo "api" debe estar corriendo el API de estatus (8787).
echo NOTA: con ESTATUS_BACKEND=api necesitas el API de estatus arriba
echo       ("Levantar API Estatus.bat" en la carpeta CHECAR ESTATUS).
echo.

REM 1) Bot local (ventana propia, con logs)
echo Levantando bot en http://127.0.0.1:8788 ...
start "WA Estatus Bot" cmd /k py "whatsapp_estatus.py"

REM 2) Tunel Cloudflare para EXPONER el webhook (Meta necesita una URL publica https)
timeout /t 4 >nul
where cloudflared >nul 2>nul
if %errorlevel%==0 (
  echo.
  echo Para exponer el webhook, agrega en tu config de cloudflared un hostname
  echo   - hostname: wa.idocrm.es
  echo     service: http://127.0.0.1:8788
  echo y corre tu tunel. O para una prueba rapida (URL temporal):
  echo   cloudflared tunnel --url http://127.0.0.1:8788
) else (
  echo [AVISO] cloudflared NO instalado: el webhook solo es accesible localmente.
)

echo.
echo Listo. La URL del webhook en Meta sera:  https://TU-DOMINIO/webhook
echo   Local:  http://127.0.0.1:8788/health
echo.
pause
