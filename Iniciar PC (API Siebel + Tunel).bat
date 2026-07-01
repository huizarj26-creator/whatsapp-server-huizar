@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PC izzi - API Siebel + Tunel (el webhook vive en Koyeb)

echo ===================================================
echo    INICIAR PC  -  API Siebel + Tunel Cloudflare
echo    (el bot de WhatsApp corre en Koyeb, aqui NO)
echo ===================================================
echo.

REM Mantener viva la sesion Siebel: logoff solo tras 4 horas de inactividad.
set ESTATUS_IDLE_LOGOFF=14400

REM Localizar cloudflared (PATH; si no, ruta de winget)
set "CF=cloudflared"
where cloudflared >nul 2>nul || set "CF=C:\Users\jvrhz\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"

echo [1/2] API de estatus (Siebel) en 127.0.0.1:8787 ...
start "API Estatus (Siebel)" cmd /k "cd /d "C:\proyects\CHECAR ESTATUS" && set ESTATUS_IDLE_LOGOFF=14400 && py estatus.py --api --host 127.0.0.1 --port 8787"

echo     esperando login Siebel (~12s) ...
timeout /t 12 >nul

echo [2/2] Tunel Cloudflare  estatus.idocrm.es -^> 8787 ...
REM Sin "cmd /k": evita que el quote-stripping de cmd corrompa la ruta con espacios de cloudflared.
start "Tunel estatus.idocrm.es" /d "%~dp0" "%CF%" tunnel --config "%~dp0cloudflared-wa.yml" run estatus-idocrm

echo.
echo ===================================================
echo  LISTO. Se abrieron 2 ventanas: API Siebel y Tunel.
echo.
echo  Koyeb consume Siebel por:  https://estatus.idocrm.es
echo  Comprueba (movil con datos): https://estatus.idocrm.es/health
echo.
echo  El webhook de Meta apunta a Koyeb:
echo    https://sporting-winne-huizar-e6de169e.koyeb.app/webhook
echo.
echo  Detener: cierra las 2 ventanas (API Siebel y Tunel).
echo ===================================================
echo.
pause
