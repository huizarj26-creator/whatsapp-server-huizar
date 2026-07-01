@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Servicio WhatsApp Estatus (API + Bot + Tunel)

echo ===================================================
echo    INICIAR SERVICIO  -  WhatsApp Estatus izzi
echo ===================================================
echo.

REM Mantener viva la sesion Siebel: logoff solo tras 4 horas de inactividad.
set ESTATUS_IDLE_LOGOFF=14400

REM Dependencias del bot (instala si faltan)
py -c "import fastapi, uvicorn, httpx, dotenv" 2>nul || py -m pip install -r requirements.txt

REM Localizar cloudflared (PATH; si no, ruta de winget)
set "CF=cloudflared"
where cloudflared >nul 2>nul || set "CF=C:\Users\jvrhz\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"

echo [1/3] API de estatus (Siebel) en 127.0.0.1:8787 ...
start "API Estatus (Siebel)" cmd /k "cd /d "C:\proyects\CHECAR ESTATUS" && set ESTATUS_IDLE_LOGOFF=14400 && py estatus.py --api --host 127.0.0.1 --port 8787"

echo     esperando login Siebel (~12s) ...
timeout /t 12 >nul

echo [2/3] Bot de WhatsApp en 127.0.0.1:8788 ...
start "WhatsApp Bot" cmd /k "py whatsapp_estatus.py"

timeout /t 3 >nul

echo [3/3] Tunel Cloudflare  wa.idocrm.es -^> 8788 ...
REM Sin "cmd /k": evita que el quote-stripping de cmd corrompa la ruta con espacios de cloudflared.
start "Tunel wa.idocrm.es" /d "%~dp0" "%CF%" tunnel --config "%~dp0cloudflared-wa.yml" run estatus-idocrm

echo.
echo ===================================================
echo  LISTO. Se abrieron 3 ventanas: API, Bot y Tunel.
echo.
echo  Webhook (Meta):  https://wa.idocrm.es/webhook
echo  Verify token:    izzi-wa-Q-iflZaw1PwBUhPDiMV-NKPToc4FCixI
echo.
echo  Probar: manda  /estatus 52783460  al WhatsApp del negocio.
echo  Detener: cierra las 3 ventanas (API, Bot, Tunel).
echo ===================================================
echo.
pause
