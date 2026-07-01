# Desplegar el webhook en Koyeb (Siebel se queda en tu PC)

## Arquitectura

```
   Meta (WhatsApp)                      TU PC (siempre encendida)
        │                          ┌─────────────────────────────┐
        │  POST /webhook           │  CHECAR ESTATUS (estatus.py) │
        ▼                          │  API Siebel  → 127.0.0.1:8787│
 ┌──────────────┐                  │        ▲                    │
 │    KOYEB     │                  │        │ cloudflared tunnel  │
 │ whatsapp_    │  GET /estatus/…  │        │                     │
 │ estatus.py   │─────────────────▶│  https://estatus.idocrm.es  │
 │ (webhook)    │   Bearer key     └─────────────────────────────┘
 └──────────────┘
   https://<app>.koyeb.app/webhook
```

- **El API de Siebel NO se puede mover a la nube**: necesita el login por navegador
  (Playwright) y la sesión Siebel viva. Vive en tu PC y se expone por el túnel
  cloudflared que ya tienes (`estatus.idocrm.es`), protegido por el Bearer key.
- **Solo el webhook** corre en Koyeb (modo `ESTATUS_BACKEND=api`).

## Requisito en tu PC: el túnel debe publicar el API de Siebel

Ya está en `cloudflared-wa.yml`. Deja corriendo en tu PC:

```
cloudflared tunnel --config "C:\proyects\WHATSAPP ESTATUS\cloudflared-wa.yml" run estatus-idocrm
```

Comprueba desde fuera que responde:  `https://estatus.idocrm.es/health` → `{"status":"ok"}`.
(Ya NO necesitas `wa.idocrm.es`: el webhook lo sirve Koyeb, no tu PC.)

## Desplegar en Koyeb

1. Koyeb → **Create Service** → **GitHub** → repo `huizarj26-creator/whatsapp-server-huizar`, rama `main`.
2. Builder: **Dockerfile** (lo detecta solo).
3. **Health check**: HTTP, path `/health`.
4. **Environment variables** (ver tabla). Marca como *Secret* las sensibles.
5. Deploy. Koyeb te da una URL `https://<algo>.koyeb.app`.

## Variables de entorno en Koyeb

| Variable | Valor | Notas |
|---|---|---|
| `WA_HOST` | `0.0.0.0` | (opcional; el código ya lo pone solo si hay `$PORT`) |
| `ESTATUS_BACKEND` | `api` | obligatorio en la nube (no puede ser `embedded`) |
| `ESTATUS_API_URL` | `https://estatus.idocrm.es` | el túnel de tu PC |
| `ESTATUS_API_KEY` | *(la key de tu `api.env`)* | **obligatoria**: en la nube no hay `api.env` que leer |
| `WHATSAPP_TOKEN` | *(token permanente System User)* | secret. El temporal de 24h caduca. |
| `WHATSAPP_PHONE_NUMBER_ID` | *(phone_number_id)* | |
| `WHATSAPP_VERIFY_TOKEN` | *(tu verify token)* | mismo que pones en Meta |
| `WHATSAPP_APP_SECRET` | *(App Secret de Meta)* | **ponlo**: webhook público sin firma = cualquiera puede inyectar mensajes |
| `GRAPH_VERSION` | `v25.0` | |
| `DEEPSEEK_API_KEY` | *(tu key)* | secret; opcional (resumen IA) |
| `WA_MAX_CUENTAS` | `50` | |
| `WA_COMMAND` | `/` | |
| `REPROG_LIVE` | `1` | crea casos REALES en izzi |
| `REPROG_FLUJO` | `master` | |
| `REPROG_TIPO` | `CORRECION` | |
| `REPROG_MOTIVO1` | `MOD DE INFORMACION CT MASTER` | |
| `REPROG_MOTIVO2` | `CORRECCION NUM EXT` | |

## Registrar el webhook en Meta

App Dashboard → WhatsApp → Configuration → Webhook:
- **Callback URL**: `https://<algo>.koyeb.app/webhook`
- **Verify token**: el mismo `WHATSAPP_VERIFY_TOKEN`.
- Suscríbete al campo **messages**.

## Avisos importantes

- **`_estado/seen.db` es efímero en Koyeb** (el disco se reinicia en cada deploy). El
  dedupe de Meta se pierde al reiniciar; a lo mucho se reprocesa un mensaje reciente.
  Aceptable. Si molesta, mover el dedupe a un Redis/Postgres externo.
- **Un solo worker / una sola instancia.** El dedupe y el rate-limit son en memoria.
  No escales a >1 instancia sin mover ese estado a un store compartido.
- **Si tu PC se apaga o el túnel se cae**, el webhook responde
  "⚠️ Servicio de Siebel no disponible". El túnel es el punto único de falla.
