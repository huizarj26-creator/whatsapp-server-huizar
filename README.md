# WhatsApp Estatus Bot

Bot de **WhatsApp Business Cloud API** (Meta) que consulta el estatus de cuentas en
Siebel. El cliente escribe por WhatsApp:

```
/estatus 52783460 52784100 52785128
```

y el bot responde con lo más importante de cada cuenta: **clasificación de estatus,
estado de la orden y casos abiertos**.

Reutiliza el motor de Siebel de `CHECAR ESTATUS` — por defecto llamando a su **API**
(`http://127.0.0.1:8787`), así que **no abre una segunda sesión de Siebel**.

---

## Ejemplo de respuesta

```
🔎 Consultando 2 cuenta(s)… dame unos segundos.

🔴 52784342 · LUIS MANUEL CARRANZA SANCHEZ
Estatus: Not Done — orden Abierta/Not Done · sol. 06/29/2026
Casos abiertos (1):
  • DISPATCH/NOT_DONE → CLIENTE (06/29/2026)
  motivo: titular comenta que hace 3 semanas llamo a ATC para cancelar...

✅ 52785128 · GABRIELA GUADALUPE GUERRERO LARES
Estatus: Instalada — orden Completa/Pendiente Monitor · sol. 06/29/2026
Casos abiertos: 0
Últ. mov.: MOD AL CONTRATO → CORRECCION DIR NO NORMALIZADA (06/29/2026)
```

Clasificación: ✅ Instalada · 🔵 En ruta (técnico) · 🟡 En proceso · 🔴 Not Done · ⚫ Cancelada.

---

## Arquitectura (resumen)

1. **Webhook** (FastAPI, puerto 8788). `GET /webhook` hace el handshake de Meta;
   `POST /webhook` recibe los mensajes.
2. **Seguridad**: valida la firma `X-Hub-Signature-256` (HMAC-SHA256 del body crudo
   con el *App Secret*), compara en tiempo constante.
3. **Responde 200 al instante** y procesa en segundo plano (las consultas a Siebel
   tardan). Dedupe persistente (SQLite) contra los reintentos de Meta.
4. **Backend de consulta**:
   - `api` (por defecto): llama al API de estatus existente. **No toca la sesión Siebel.**
   - `embedded` (opcional): abre su propia sesión Siebel importando `estatus.py`.
     ⚠️ **Excluyente con el API**: nunca corras los dos a la vez (mismo usuario Siebel
     → se pelean la sesión). El bot lo detecta y aborta si el API está arriba.

---

## Puesta en marcha

### 1) Requisitos
```
py -m pip install -r requirements.txt
```

### 2) Tener el API de estatus corriendo (modo `api`, recomendado)
En `C:\proyects\CHECAR ESTATUS` ejecuta **"Levantar API Estatus.bat"**
(o `py estatus.py --api`). El bot lee la key automáticamente de su `api.env`.

### 3) Configurar `.env` de esta carpeta
Llena las credenciales de Meta:
- `WHATSAPP_TOKEN` — token de acceso (usa uno permanente de *System User* en prod).
- `WHATSAPP_PHONE_NUMBER_ID` — el *phone number id* (de "API Setup"), no el número visible.
- `WHATSAPP_VERIFY_TOKEN` — lo inventas tú; debe coincidir con el del dashboard.
- `WHATSAPP_APP_SECRET` — *App Secret* (Settings > Basic).

### 4) Probar SIN WhatsApp (recomendado antes de exponer)
```
py whatsapp_estatus.py --probar 52783460 52784100
```
Debe imprimir el resumen de cada cuenta. Si falla aquí, el problema es el backend
(API de estatus / Siebel), no WhatsApp.

### 5) Levantar el bot
```
py whatsapp_estatus.py
```
(o **"Levantar WhatsApp Bot.bat"**). Queda en `http://127.0.0.1:8788`.

### 6) Exponer el webhook con una URL pública https
Meta exige https público. Con tu **cloudflared** ya instalado, agrega un hostname en
tu config (ej. `C:\Users\...\.cloudflared\estatus-idocrm.yml`):
```yaml
ingress:
  - hostname: wa.idocrm.es
    service: http://127.0.0.1:8788
  - hostname: estatus.idocrm.es
    service: http://127.0.0.1:8787
  - service: http_status:404
```
y crea el registro DNS (`cloudflared tunnel route dns estatus-idocrm wa.idocrm.es`).
Para una prueba rápida y temporal: `cloudflared tunnel --url http://127.0.0.1:8788`.

### 7) Registrar el webhook en Meta
En el **App Dashboard > WhatsApp > Configuration > Webhook**:
- **Callback URL**: `https://wa.idocrm.es/webhook`
- **Verify token**: el mismo `WHATSAPP_VERIFY_TOKEN` del `.env`.
- Suscríbete al campo **messages**.

Meta hará un `GET /webhook` para verificar; debe quedar en verde.

### 8) Mandar `/estatus 52783460` desde tu WhatsApp al número del negocio.

> **Ventana de 24h**: el bot responde texto libre porque contestas dentro de las 24h
> del mensaje del cliente (mensaje de servicio, sin plantilla ni costo extra). Iniciar
> conversación en frío requeriría una plantilla aprobada.

---

## Variables de `.env` (todas)

| Variable | Default | Qué hace |
|---|---|---|
| `WHATSAPP_TOKEN` | — | Bearer para enviar mensajes |
| `WHATSAPP_PHONE_NUMBER_ID` | — | phone_number_id (fallback; se prefiere el del webhook) |
| `WHATSAPP_VERIFY_TOKEN` | — | handshake del webhook (GET) |
| `WHATSAPP_APP_SECRET` | — | valida la firma del webhook (POST) |
| `GRAPH_VERSION` | `v25.0` | versión de Graph API |
| `ESTATUS_BACKEND` | `api` | `api` o `embedded` |
| `ESTATUS_API_URL` | `http://127.0.0.1:8787` | API de estatus |
| `ESTATUS_DIR` | `C:\proyects\CHECAR ESTATUS` | carpeta de CHECAR ESTATUS |
| `ESTATUS_API_KEY` | (de `api.env`) | key del API |
| `WA_PORT` / `WA_HOST` | `8788` / `127.0.0.1` | dónde escucha el bot |
| `WA_MAX_CUENTAS` | `10` | tope de cuentas por mensaje |
| `WA_COMMAND` | `/estatus` | comando activador |
| `WA_API_TIMEOUT` | `180` | timeout por cuenta (s) |
| `WA_RATE_MAX` / `WA_RATE_WIN` | `6` / `600` | rate-limit por remitente |

---

## Notas de operación
- **Un solo worker** (`workers=1`): el dedupe y, en embedded, la sesión Siebel, son
  en proceso. No escales horizontalmente sin mover el dedupe a Redis y usar `api`.
- **Logs** en la consola del bot: cada envío y cada error de Meta/Siebel.
- **Pruebas**: `--probar` (sin WhatsApp) y `--enviar <numero> <cuentas>` (manda un WA real).
- El estado interno (dedupe) vive en `_estado/seen.db` (ignorado por git).
