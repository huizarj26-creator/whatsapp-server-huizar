# -*- coding: utf-8 -*-
"""
whatsapp_estatus.py — BOT de WhatsApp para consultar ESTATUS de cuentas (Siebel).

Igual que la herramienta de CHECAR ESTATUS pero por WhatsApp Business **Cloud API**
(Meta). El cliente escribe por WhatsApp:

    /estatus 52783460 52784100 52785128

y el bot responde con lo más importante de cada cuenta: clasificación de estatus,
estado de la orden y CASOS ABIERTOS.

Arquitectura (ver README.md para detalle):
  - Recibe el webhook de Meta, valida la firma (X-Hub-Signature-256), responde 200
    al instante y procesa en segundo plano (las consultas a Siebel tardan).
  - Para consultar Siebel NO abre su propia sesión por defecto: reutiliza el
    **API de estatus** que ya corre en 127.0.0.1:8787 (CHECAR ESTATUS). Así no se
    pelea la sesión de Siebel (mismo usuario) ni contamina su carpeta.
  - Modo `embedded` (opcional, EXCLUYENTE con el API): importa el motor de
    estatus.py y maneja su propia sesión. Solo si el API NO está corriendo.

Modos de ejecución:
  py whatsapp_estatus.py                 -> levanta el webhook (servidor)
  py whatsapp_estatus.py --probar 52783460 52784100   -> imprime la respuesta SIN WhatsApp
  py whatsapp_estatus.py --enviar 521555... 52783460   -> manda un WhatsApp de prueba

Config: archivo .env en esta carpeta (ver .env de ejemplo).
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request, Response

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ESTADO = HERE / "_estado"          # estado interno del bot (dedupe, etc.)
ESTADO.mkdir(parents=True, exist_ok=True)
load_dotenv(HERE / ".env")

log = logging.getLogger("wa_estatus")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ CONFIG (todo por .env, con valores por defecto razonables)                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝
def _env(k, d=""):
    return (os.getenv(k, d) or "").strip()


# --- WhatsApp / Meta ---
VERIFY_TOKEN = _env("WHATSAPP_VERIFY_TOKEN")           # GET /webhook (handshake)
APP_SECRET = _env("WHATSAPP_APP_SECRET")               # firma X-Hub-Signature-256
ACCESS_TOKEN = _env("WHATSAPP_TOKEN")                  # Bearer para enviar
PHONE_NUMBER_ID = _env("WHATSAPP_PHONE_NUMBER_ID")     # fallback; se prefiere el del webhook
GRAPH_VERSION = _env("GRAPH_VERSION", "v25.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# --- Backend de consulta (Siebel) ---
ESTATUS_BACKEND = _env("ESTATUS_BACKEND", "api").lower()   # "api" | "embedded"
ESTATUS_API_URL = _env("ESTATUS_API_URL", "http://127.0.0.1:8787").rstrip("/")
ESTATUS_DIR = _env("ESTATUS_DIR", r"C:\proyects\CHECAR ESTATUS")
ESTATUS_EMBEDDED_FORCE = _env("ESTATUS_EMBEDDED_FORCE", "0") == "1"

# --- Operación ---
# En la nube (Koyeb/Render/Railway…) la plataforma inyecta $PORT y hay que
# escuchar en 0.0.0.0. En local se mantienen los defaults de siempre.
_IN_CLOUD = bool(_env("PORT"))
PORT = int(_env("PORT") or _env("WA_PORT", "8788"))
HOST = _env("WA_HOST", "0.0.0.0" if _IN_CLOUD else "127.0.0.1")
MAX_CUENTAS = int(_env("WA_MAX_CUENTAS", "50"))
CHUNK = int(_env("WA_CHUNK", "3900"))                  # tope WhatsApp = 4096
API_TIMEOUT = float(_env("WA_API_TIMEOUT", "180"))     # Siebel puede tardar
WA_CONCURRENCIA = int(_env("WA_CONCURRENCIA", "2"))    # consultas en paralelo (= pool de sesiones del API)
WA_BATCH = max(WA_CONCURRENCIA, int(_env("WA_BATCH", "6")))   # cuentas que se despachan por tanda
WA_REINTENTOS = int(_env("WA_REINTENTOS", "3"))               # intentos por cuenta si da error
WA_REINTENTO_ESPERA = float(_env("WA_REINTENTO_ESPERA", "3")) # espera (s) entre reintentos
SEEN_TTL = float(_env("WA_SEEN_TTL", "7200"))          # 2 h: ventana de reintentos
RL_MAX = int(_env("WA_RATE_MAX", "6"))                 # consultas por remitente
RL_WIN = float(_env("WA_RATE_WIN", "600"))             # ...en esta ventana (s)
COMMAND = _env("WA_COMMAND", "/").lower()

# --- /ine: genera credencial INE consumiendo gen-docs-izzi ---
GENDOCS_URL = _env("GENDOCS_URL").rstrip("/")          # URL de gen-docs en Koyeb
GENDOCS_COMMAND = _env("GENDOCS_COMMAND", "/gen").lower()
CURP_COMMAND = _env("CURP_COMMAND", "/curp").lower()     # calcular CURP
GENDOCS_TIMEOUT = float(_env("GENDOCS_TIMEOUT", "180"))  # generar la INE tarda

# --- Resumen final con DeepSeek (opcional) ---
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = _env("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_URL = _env("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")

# --- /reprogramar: crea Caso de Negocio en izzi (reusa cn_master_app.py) ---
REPROG_COMMAND = _env("REPROG_COMMAND", "/reprogramar").lower()
REPROG_LIVE = _env("REPROG_LIVE", "0") == "1"            # 0 = solo PREVIEW; 1 = crea de verdad en izzi
REPROG_FLUJO = _env("REPROG_FLUJO", "master").lower()    # master (/crearCNMaster) | normal (/crearCN)
REPROG_DESTINO = _env("REPROG_DESTINO", "izzi").lower()  # izzi (directo) | wrapper (crearcn.idocrm.es)
REPROG_TOKEN = _env("REPROG_TOKEN")                      # Bearer (solo wrapper)
REPROG_TIPO = _env("REPROG_TIPO", "CORRECION")           # CORRECION | FALLA_ACTIVACION (define los fijos)
REPROG_MOTIVO1 = _env("REPROG_MOTIVO1", "MOD DE INFORMACION CT MASTER")  # spinner 1 -> submotivo
REPROG_MOTIVO2 = _env("REPROG_MOTIVO2", "CORRECCION NUM EXT")            # spinner 2 -> motivoCliente
REPROG_MEDIO = _env("REPROG_MEDIO", "")                   # medioContacto (vacío, como tu captura)


def _resolver_api_key():
    """La key del API de estatus: de env, o del api.env de CHECAR ESTATUS."""
    k = _env("ESTATUS_API_KEY")
    if k:
        return k
    f = Path(ESTATUS_DIR) / "api.env"
    if f.exists():
        for ln in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            ln = ln.strip()
            if ln.lower().startswith("estatus_api_key") and "=" in ln:
                return ln.split("=", 1)[1].strip()
    return ""


ESTATUS_API_KEY = _resolver_api_key()
CUENTA_RE = re.compile(r"(?<!\d)\d{8}(?!\d)")          # 8 dígitos exactos
TERMINALES = {"cerrado", "cerrada", "cancelado", "cancelada", "canceled",
              "cancelled", "resuelto", "resuelta", "closed", "completado"}


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ DEDUPE persistente (SQLite con TTL) — Meta reintenta por días              ║
# ╚══════════════════════════════════════════════════════════════════════════╝
_db = sqlite3.connect(str(ESTADO / "seen.db"), check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, ts REAL)")
_db.commit()
_db_lock = threading.Lock()


def visto_y_marcar(mid: str) -> bool:
    """True si el id es NUEVO (no visto). Marca como visto de forma atómica."""
    if not mid:
        return True
    now = time.time()
    with _db_lock:
        _db.execute("DELETE FROM seen WHERE ts < ?", (now - SEEN_TTL,))
        try:
            _db.execute("INSERT INTO seen(id, ts) VALUES(?, ?)", (mid, now))
            _db.commit()
            return True
        except sqlite3.IntegrityError:
            return False


# Rate-limit y anti-concurrencia por remitente (1 solo worker -> en memoria basta)
_rl = defaultdict(list)
_inflight = set()
_rl_calls = 0

# Guardado en Supabase pendiente de confirmar (/gen -> "¿guardar? sí/no")
# wa_id -> {ts, nombre, json_data, b64_fronta, b64_trasera}
_pend_supabase = {}
PEND_TTL = float(_env("WA_PEND_TTL", "1800"))          # 30 min para confirmar
_SUBIR = {"subir", "sube", "guardar", "guarda", "si", "sí", "s", "ok", "dale", "yes", "y", "1"}
_NO = {"no", "n", "0", "nel", "cancelar", "cancela", "descartar", "descarta"}
_PREGUNTA_PEND = ("💾 ¿Qué hago con esta captura?\n"
                  "• *subir* → guardar en Supabase\n"
                  "• *no* → descartar\n"
                  "• o dime qué *editar* (ej: «cambia el nombre a JUAN», «CP 64000») y la regenero.")


def rate_ok(wa_id: str) -> bool:
    global _rl_calls
    now = time.time()
    _rl_calls += 1
    if _rl_calls % 500 == 0:        # barrido: evita que _rl crezca sin límite
        for k in [k for k, v in list(_rl.items()) if not v or now - v[-1] >= RL_WIN]:
            _rl.pop(k, None)
    h = [t for t in _rl[wa_id] if now - t < RL_WIN]
    _rl[wa_id] = h
    if len(h) >= RL_MAX:
        return False
    h.append(now)
    return True


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ BACKEND de consulta a Siebel                                               ║
# ╚══════════════════════════════════════════════════════════════════════════╝
class MotorEmbedded:
    """Sesión Siebel propia (importa estatus.py). EXCLUYENTE con el API."""
    def __init__(self):
        os.environ.setdefault("ESTATUS_DUMP_DET", "0")   # no ensuciar SALIDA/det_*
        sys.path.insert(0, ESTATUS_DIR)
        import estatus  # noqa: E402
        self.e = estatus
        self._cli = None
        self._tpl = None
        self._lock = asyncio.Lock()
        self._fallo = 0.0

    async def _ensure(self):
        if self._cli is not None:
            return
        if time.monotonic() - self._fallo < 20:
            raise RuntimeError("Siebel en cooldown")
        if self._tpl is None:
            self._tpl = self.e.cargar_templates()
        try:
            ses = await self.e.bootstrap(on_log=lambda *_: None, profile="whatsapp")
        except Exception as ex:
            self._fallo = time.monotonic()
            raise RuntimeError(f"login Siebel falló: {str(ex)[:60]}")
        cli = self.e.SiebelApi(ses, self._tpl)
        cli.start_keepalive(50)
        self._cli = cli

    def _reset(self):
        try:
            if self._cli:
                self._cli.stop_keepalive()
                self._cli.logoff()
        except Exception:
            pass
        self._cli = None

    async def estatus(self, cuenta: str) -> dict:
        async with self._lock:
            await self._ensure()
            try:
                return await asyncio.to_thread(self.e.extraer_cuenta, self._cli, cuenta, lambda *_: None)
            except self.e.SessionExpired:
                self._reset()
                await self._ensure()
                return await asyncio.to_thread(self.e.extraer_cuenta, self._cli, cuenta, lambda *_: None)


MOTOR = None  # se crea en startup solo si backend == embedded


async def consultar_cuenta(cuenta: str, client: httpx.AsyncClient) -> dict:
    """Devuelve el dict de la cuenta (o {cuenta, error})."""
    if ESTATUS_BACKEND == "embedded":
        try:
            return await MOTOR.estatus(cuenta)
        except Exception as ex:
            return {"cuenta": cuenta, "error": str(ex)[:80]}
    # API mode (por defecto)
    try:
        r = await client.get(f"{ESTATUS_API_URL}/estatus/{cuenta}",
                             headers={"Authorization": f"Bearer {ESTATUS_API_KEY}"},
                             timeout=API_TIMEOUT)
        if r.status_code == 503:
            return {"cuenta": cuenta, "error": "siebel no disponible"}
        if r.status_code == 401:
            return {"cuenta": cuenta, "error": "api key inválida"}
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as ex:
        return {"cuenta": cuenta, "error": f"api: {str(ex)[:60]}"}


async def backend_vivo(client: httpx.AsyncClient) -> bool:
    if ESTATUS_BACKEND == "embedded":
        return True
    try:
        r = await client.get(f"{ESTATUS_API_URL}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ FORMATO del mensaje (estatus + casos abiertos)                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝
def _dt(s):
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return datetime.min


def caso_abierto(c: dict) -> bool:
    e = (c.get("estado") or "").strip().lower()
    return bool(e) and e not in TERMINALES


def clasificar(o: dict):
    estado = (o.get("estado") or "").lower()
    sub = (o.get("sub_estado") or "").lower()
    if "completa" in estado:
        return "✅", "Instalada"
    if "cancel" in estado:
        return "⚫", "Cancelada"
    if "not done" in sub:
        return "🔴", "Not Done"
    if "tecnico" in sub or "técnico" in sub:
        return "🔵", "En ruta (técnico)"
    if estado == "abierta":
        return "🟡", "En proceso"
    return "🟡", (o.get("estado") or "—")


def _limpiar(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _recortar(s, n):
    s = _limpiar(s)
    return s if len(s) <= n else s[:n - 1] + "…"


_SEP = "━" * 14


def resumir_cuenta(d: dict) -> str:
    cuenta = d.get("cuenta", "?")
    if d.get("error") is not None and "ordenes" not in d:
        return f"{_SEP}\n⚠️ *{cuenta}*: no disponible ({d.get('error') or 'sin datos'})"

    nombre = (d.get("nombre") or "").strip()
    ordenes = d.get("ordenes") or []
    casos = sorted(d.get("casos_de_negocio") or [], key=lambda c: _dt(c.get("fecha")), reverse=True)

    emoji, etq = clasificar(ordenes[0] if ordenes else {})
    titulo = f"{cuenta} · {nombre}".rstrip(" ·")
    out = [_SEP, f"{emoji} *{titulo}*", f"Estatus: *{etq}*"]

    # --- Órdenes de servicio (OS): estado, vendedor, técnico y comentarios ---
    if not ordenes:
        out.append("")
        out.append("📦 OS: sin orden registrada")
    for o in ordenes:
        out.append("")
        out.append(f"📦 *OS {o.get('orden', '-')}* — {o.get('estado', '-')}/{o.get('sub_estado') or '—'}")
        if o.get("vendedor"):
            out.append(f"   Vendedor: {o.get('vendedor')}")
        out.append(f"   Técnico: {o.get('clave_tecnico') or '—'}")
        fsol = (o.get("fecha_solicitada") or "")[:16]
        if fsol:
            out.append(f"   Solicitada: {fsol}")
        ctec = _recortar(o.get("comentarios_tecnico"), 240)
        if ctec:
            out.append(f"   🔧 Téc: {ctec}")
        cos = _recortar(o.get("comentarios_os"), 160)
        if cos:
            out.append(f"   📝 OS: {cos}")

    # --- Últimos 4 casos: estado, fecha de creación y comentarios ---
    if casos:
        out.append("")
        out.append(f"🗂 *Últimos {min(4, len(casos))} casos:*")
        for i, c in enumerate(casos[:4], 1):
            out.append("")
            est = c.get("estado") or "—"
            f = (c.get("fecha") or "")[:16]
            mot = "/".join(x for x in [c.get("motivos", ""), c.get("submotivo", "")] if x)
            sol = c.get("solucion") or ""
            out.append(f"{i}. [{est}] {f}")
            if mot or sol:
                out.append(f"   {mot} → {sol}")
            com = _recortar(c.get("comentarios"), 200)
            if com:
                out.append(f"   ↳ {com}")
    else:
        out.append("")
        out.append("🗂 Sin casos")
    return "\n".join(out)


def _split_bloque(b: str, limite: int):
    """Parte un bloque gigante (una sola cuenta) por líneas, bajo el límite."""
    partes, cur = [], ""
    for ln in b.split("\n"):
        if len(ln) > limite:
            ln = ln[:limite - 1] + "…"
        if cur and len(cur) + 1 + len(ln) > limite:
            partes.append(cur)
            cur = ln
        else:
            cur = (cur + "\n" + ln) if cur else ln
    if cur:
        partes.append(cur)
    return partes


def construir_chunks(bloques, limite=CHUNK):
    chunks, buf = [], ""
    for b in bloques:
        for p in (_split_bloque(b, limite) if len(b) > limite else [b]):
            if buf and len(buf) + 2 + len(p) > limite:
                chunks.append(buf)
                buf = p
            else:
                buf = (buf + "\n\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def parsear_cuentas(texto: str):
    """Cuentas (8 dígitos) tras el comando, dedup preservando orden, tope MAX."""
    crudas = CUENTA_RE.findall(texto or "")
    vistas, ordenadas = set(), []
    for c in crudas:
        if c not in vistas:
            vistas.add(c)
            ordenadas.append(c)
    truncado = len(ordenadas) > MAX_CUENTAS
    return ordenadas[:MAX_CUENTAS], truncado


AYUDA = ("👋 Mándame  /  seguido de las cuentas (8 dígitos). Ejemplo:\n"
         f"{COMMAND}52783460 52784100\n"
         "y te devuelvo estatus, OS y últimos casos.\n\n"
         f"🪪 Para generar una INE: {GENDOCS_COMMAND} <datos del cliente>\n"
         f"Ej: {GENDOCS_COMMAND} Juan Perez Lopez, CURP PELJ850101HDFRXN09, CDMX, CP 06600\n"
         "(luego te pregunto: subir a Supabase, o dime qué editar y la regenero)\n\n"
         f"🔢 Para calcular una CURP: {CURP_COMMAND} <nombre, sexo, fecha y lugar de nacimiento>\n"
         f"Ej: {CURP_COMMAND} Ana Lopez Soto, mujer, 1999-02-28, Jalisco")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ ENVÍO a WhatsApp                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝
async def wa_send(to: str, body: str, phone_id: str, client: httpx.AsyncClient) -> bool:
    if not (ACCESS_TOKEN and phone_id):
        log.warning("WhatsApp sin token/phone_id; no se envía. to=%s", to)
        return False
    url = f"{GRAPH_BASE}/{phone_id}/messages"
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual",
               "to": to, "type": "text", "text": {"preview_url": False, "body": body}}
    try:
        r = await client.post(url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}",
                                            "Content-Type": "application/json"},
                             json=payload, timeout=30)
    except httpx.HTTPError as ex:
        log.error("envío WhatsApp falló (red): %s", str(ex)[:120])
        return False
    if r.status_code >= 400:
        # 131047 = fuera de la ventana de 24h (requiere plantilla); no reintentar.
        log.error("envío WhatsApp %s: %s", r.status_code, r.text[:300])
        return False
    return True


async def wa_upload_media(data: bytes, mime: str, phone_id: str, client: httpx.AsyncClient) -> str:
    """Sube una imagen a Meta (endpoint /media) y devuelve el media_id (o "" si falla)."""
    if not (ACCESS_TOKEN and phone_id and data):
        return ""
    ext = "png" if mime == "image/png" else "jpg"
    url = f"{GRAPH_BASE}/{phone_id}/media"
    files = {"file": (f"ine.{ext}", data, mime)}
    form = {"messaging_product": "whatsapp", "type": mime}
    try:
        r = await client.post(url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                              data=form, files=files, timeout=60)
    except httpx.HTTPError as ex:
        log.error("subida de media falló (red): %s", str(ex)[:120])
        return ""
    if r.status_code >= 400:
        log.error("subida de media %s: %s", r.status_code, r.text[:300])
        return ""
    return (r.json() or {}).get("id", "")


async def wa_send_image(to: str, media_id: str, phone_id: str, client: httpx.AsyncClient,
                        caption: str = "") -> bool:
    """Manda una imagen ya subida (por media_id) al destinatario."""
    if not (ACCESS_TOKEN and phone_id and media_id):
        return False
    url = f"{GRAPH_BASE}/{phone_id}/messages"
    img = {"id": media_id}
    if caption:
        img["caption"] = caption[:1024]
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual",
               "to": to, "type": "image", "image": img}
    try:
        r = await client.post(url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}",
                                            "Content-Type": "application/json"},
                             json=payload, timeout=30)
    except httpx.HTTPError as ex:
        log.error("envío de imagen falló (red): %s", str(ex)[:120])
        return False
    if r.status_code >= 400:
        log.error("envío de imagen %s: %s", r.status_code, r.text[:300])
        return False
    return True


def _ctx_ia(d) -> str:
    """Contexto COMPLETO de una cuenta para que la IA razone (OS + técnico + casos)."""
    c = d.get("cuenta", "?")
    if d.get("error") is not None and "ordenes" not in d:
        return f"CUENTA {c}: NO DISPONIBLE ({d.get('error')})"
    nombre = (d.get("nombre") or "")[:26]
    ordenes = d.get("ordenes") or []
    _, etq = clasificar(ordenes[0] if ordenes else {})
    out = [f"CUENTA {c} ({nombre}) | estatus: {etq}"]
    for o in ordenes:
        out.append(f"  OS {o.get('orden', '-')} {o.get('estado', '-')}/{o.get('sub_estado') or '-'}"
                   f" | vend {o.get('vendedor') or '-'} | tec {o.get('clave_tecnico') or '-'}")
        ct = _recortar(o.get("comentarios_tecnico"), 180)
        if ct:
            out.append(f"     coment.tec: {ct}")
    casos = sorted(d.get("casos_de_negocio") or [], key=lambda x: _dt(x.get("fecha")), reverse=True)
    for cc in casos[:4]:
        com = _recortar(cc.get("comentarios"), 150)
        out.append(f"  caso [{cc.get('estado', '-')}] {cc.get('submotivo', '')}->{cc.get('solucion', '')}: {com}")
    return "\n".join(out)


async def resumen_ia(datos, client: httpx.AsyncClient):
    """DeepSeek: resumen corto + recomendación por cuenta (por qué y si conviene reprogramar)."""
    if not DEEPSEEK_API_KEY:
        return None
    contexto = "\n".join(_ctx_ia(d) for d in datos)
    sistema = (
        "Eres analista de instalaciones de izzi. Te paso el detalle de varias cuentas: estatus, OS, "
        "comentarios del tecnico y los ultimos casos con sus comentarios. Responde en espanol, texto "
        "plano para WhatsApp (sin markdown), conciso.\n"
        "1) 'Resumen:' 1-2 lineas con el panorama (cuantas instaladas / en ruta / not done / canceladas).\n"
        "2) 'Recomendaciones:' UNA linea por cuenta que requiera accion (not done, cancelada, o instalada "
        "pendiente). Para cada una di POR QUE esta en ese estatus (segun los comentarios) y si CONVIENE "
        "REPROGRAMAR o no, con la razon. Criterios:\n"
        "- Cliente cancela / 'ya no lo requiere' / se fue con otro proveedor -> NO reprogramar (venta perdida).\n"
        "- Imposibilidad o incumplimiento tecnico que CANCELA -> NO reprogramar (o escalar a factibilidad).\n"
        "- Tecnico no acudio / incumplimiento tecnico y el cliente SI quiere -> SI reprogramar.\n"
        "- Cliente no localizado / buzon -> reintentar contacto; reprogramar solo si se logra contactar.\n"
        "- Falta de pago / pago no reflejado -> gestionar el pago antes de reprogramar.\n"
        "- Datos o direccion mal / sin normalizar -> corregir y reprogramar.\n"
        "- Instalada/Completa -> no reprogramar, solo cerrar monitoreo.\n"
        "IMPORTANTE: incluye TODAS las cuentas que te paso, UNA linea por cada una, sin omitir ninguna "
        "(las instaladas o en ruta con una nota breve). Maximo 1-2 lineas por cuenta."
    )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": sistema},
            {"role": "user", "content": contexto},
        ],
        "temperature": 0.3,
        "max_tokens": 3000,
        "stream": False,
    }
    try:
        r = await client.post(DEEPSEEK_URL,
                              headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=120)
        r.raise_for_status()
        content = (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as ex:
        log.warning("DeepSeek falló: %s", str(ex)[:150])
        return None
    # Backstop: garantiza que NINGUNA cuenta se quede fuera (la IA a veces omite)
    presentes = set(re.findall(r"\d{8}", content))
    faltan = []
    for d in datos:
        c = str(d.get("cuenta", ""))
        if not re.fullmatch(r"\d{8}", c) or c in presentes:
            continue
        if d.get("error") is not None and "ordenes" not in d:
            faltan.append(f"- {c}: no disponible")
            continue
        o = (d.get("ordenes") or [{}])[0]
        _, etq = clasificar(o)
        casos = sorted(d.get("casos_de_negocio") or [], key=lambda x: _dt(x.get("fecha")), reverse=True)
        u = casos[0] if casos else {}
        nom = (d.get("nombre") or "")[:20]
        faltan.append(f"- {c} ({nom}): {etq} — {u.get('solucion') or u.get('submotivo') or 'sin casos'}")
    if faltan:
        content += "\n— Otras:\n" + "\n".join(faltan)
    return content


def _resumen_caso_izzi(cuenta, status_code, texto):
    """Mensaje limpio a partir de la respuesta de crearCNMaster (caso creado + cuenta + folio)."""
    try:
        j = json.loads(texto)
    except Exception:
        j = None
    if isinstance(j, dict):
        ok = str(j.get("izziErrorCode", "")) == "000" or str(j.get("izziError", "")).lower() == "success"
        result = ((j.get("response") or {}).get("result") or {})
        folio = ""
        try:
            srs = result["data"]["listOfServicereqio"]["serviceRequest"]
            folio = (srs[0].get("id", "") if srs else "")
        except Exception:
            pass
        if ok:
            return f"✅ Caso creado · cuenta {cuenta}" + (f"\nFolio: {folio}" if folio else "")
        motivo = result.get("message") or j.get("izziError") or f"HTTP {status_code}"
        return f"⚠️ No se creó · cuenta {cuenta}\n{motivo}"
    return (f"✅ Caso creado · cuenta {cuenta}" if status_code < 400
            else f"⚠️ No se creó · cuenta {cuenta} (HTTP {status_code})")


async def manejar_reprogramar(texto, to, phone_id, client):
    """/reprogramar,<cuenta>,<comentario> -> crea Caso de Negocio en izzi usando cn_master_app.py."""
    resto = texto[len(REPROG_COMMAND):]
    m = re.search(r"\d{8}", resto)
    cuenta = m.group(0) if m else ""
    comentario = (resto[m.end():].lstrip(" ,").strip() if m else "")
    if not cuenta or not comentario:
        await wa_send(to, f"Uso: {REPROG_COMMAND},<cuenta 8 dígitos>,<comentario>\n"
                          f"Ej: {REPROG_COMMAND},52782976, tt solicita reprogramar lo antes posible",
                      phone_id, client)
        return
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import cn_master_app as cnm
    except Exception as ex:
        await wa_send(to, f"⚠️ No pude cargar cn_master_app: {str(ex)[:90]}", phone_id, client)
        return

    es_master = REPROG_FLUJO != "normal"
    if es_master:
        payload = cnm.construir_payload_master(REPROG_TIPO, cuenta, comentario,
                                               REPROG_MOTIVO1, REPROG_MOTIVO2, [],
                                               medio_contacto=REPROG_MEDIO)
        url = cnm.EP["master"].get(REPROG_DESTINO, cnm.EP["master"]["izzi"])
    else:
        orden = ""
        d = await consultar_cuenta(cuenta, client)
        if not (d.get("error") is not None and "ordenes" not in d):
            ords = d.get("ordenes") or []
            orden = ords[0].get("orden", "") if ords else ""
        payload = cnm.construir_payload_normal(cuenta, orden, comentario, [])
        url = cnm.EP["normal"].get(REPROG_DESTINO, cnm.EP["normal"]["izzi"])

    if not REPROG_LIVE:
        await wa_send(to, f"👁️ *PREVIEW* (no se envió a izzi)\n{REPROG_FLUJO} → {url}\n```\n"
                          f"{json.dumps(payload, ensure_ascii=False, indent=2)[:1400]}\n```\n"
                          "Si los campos están bien, pon REPROG_LIVE=1 y lo crea de verdad.",
                      phone_id, client)
        return

    # Mismos headers que cn_master_app._do_post (directo a izzi o wrapper con token)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if REPROG_DESTINO == "izzi":
        headers["User-Agent"] = "okhttp/4.12.0"
        if es_master:
            headers["x-access-origin"] = "APPMISVENTAS"
    elif REPROG_TOKEN:
        headers["Authorization"] = f"Bearer {REPROG_TOKEN}"
    try:
        r = await client.post(url, headers=headers, json=payload, timeout=60)
        await wa_send(to, _resumen_caso_izzi(cuenta, r.status_code, r.text), phone_id, client)
    except Exception as ex:
        await wa_send(to, f"❌ No se creó · cuenta {cuenta}: {str(ex)[:120]}", phone_id, client)


async def manejar_ine(texto, to, phone_id, client):
    """/ine <datos libres> -> gen-docs /parsear (texto→JSON) y luego /generar-todo
    (JSON→INE frente+reverso). Manda primero el JSON y después las 2 imágenes."""
    if not GENDOCS_URL:
        await wa_send(to, "⚠️ Falta configurar GENDOCS_URL (URL de gen-docs en Koyeb).", phone_id, client)
        return
    datos_texto = texto[len(GENDOCS_COMMAND):].strip()
    if not datos_texto:
        await wa_send(to, f"Uso: {GENDOCS_COMMAND} <datos del cliente>\n"
                          "Ej: Juan Perez Lopez, CURP PELJ850101HDFRXN09, tel 5512345678, "
                          "Av. Reforma 222 int 4B, col Juarez, Cuauhtemoc, CDMX, CP 06600",
                      phone_id, client)
        return

    await wa_send(to, "🪪 Procesando… te mando el JSON y luego la INE (puede tardar ~1 min).",
                  phone_id, client)

    # 1) Texto libre -> JSON estructurado (DeepSeek en gen-docs)
    try:
        r = await client.post(f"{GENDOCS_URL}/parsear", json={"texto": datos_texto}, timeout=GENDOCS_TIMEOUT)
        if r.status_code >= 400:
            await wa_send(to, f"⚠️ No pude estructurar los datos (HTTP {r.status_code}).\n{r.text[:200]}", phone_id, client)
            return
        datos = (r.json() or {}).get("datos") or {}
    except httpx.HTTPError as ex:
        await wa_send(to, f"⚠️ Error llamando a gen-docs (/parsear): {str(ex)[:120]}", phone_id, client)
        return
    if not datos:
        await wa_send(to, "⚠️ gen-docs no devolvió datos estructurados.", phone_id, client)
        return

    await _generar_y_enviar(datos, to, phone_id, client)


async def _generar_y_enviar(datos, to, phone_id, client, encabezado="📋 *Datos estructurados*"):
    """Manda el JSON, genera y manda la INE (frente+reverso), deja el resultado
    pendiente y PREGUNTA (subir / editar / no). Lo reutilizan /gen y las ediciones."""
    lleno = {k: v for k, v in datos.items() if str(v).strip()}
    js = json.dumps(lleno, ensure_ascii=False, indent=2)
    for ch in construir_chunks([f"{encabezado}\n```\n" + js + "\n```"]):
        await wa_send(to, ch, phone_id, client)
        await asyncio.sleep(0.4)

    # JSON -> imágenes INE (frente + reverso)
    try:
        r = await client.post(f"{GENDOCS_URL}/generar-todo", json=datos, timeout=GENDOCS_TIMEOUT)
        if r.status_code >= 400:
            await wa_send(to, f"⚠️ No pude generar la INE (HTTP {r.status_code}).\n{r.text[:200]}", phone_id, client)
            return
        out = r.json() or {}
    except httpx.HTTPError as ex:
        await wa_send(to, f"⚠️ Error llamando a gen-docs (/generar-todo): {str(ex)[:120]}", phone_id, client)
        return

    b64 = out.get("documentos_b64") or {}
    for nombre in ("frente", "reverso"):
        if nombre not in b64:
            continue
        try:
            data = base64.b64decode(b64[nombre])
        except Exception:
            await wa_send(to, f"⚠️ Imagen '{nombre}' corrupta.", phone_id, client)
            continue
        mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        mid = await wa_upload_media(data, mime, phone_id, client)
        if mid:
            await wa_send_image(to, mid, phone_id, client, caption=f"INE {nombre}")
            await asyncio.sleep(0.4)
        else:
            await wa_send(to, f"⚠️ No pude subir la imagen '{nombre}' a WhatsApp.", phone_id, client)

    notas = []
    if out.get("curp"):
        notas.append(f"CURP usada: {out['curp']}")
    if out.get("advertencias"):
        notas.append("Avisos: " + "; ".join(str(a) for a in out["advertencias"]))
    if out.get("errores"):
        notas.append("Errores: " + json.dumps(out["errores"], ensure_ascii=False)[:300])
    if notas:
        await wa_send(to, "\n".join(notas), phone_id, client)

    # Barre pendientes viejos. Sin imágenes no dejamos nada pendiente.
    now = time.time()
    for k in [k for k, v in list(_pend_supabase.items()) if now - v["ts"] > PEND_TTL]:
        _pend_supabase.pop(k, None)
    if not (b64.get("frente") or b64.get("reverso")):
        await wa_send(to, "⚠️ No se generaron imágenes; no dejo nada pendiente.", phone_id, client)
        return
    nombre = " ".join(
        p for p in (str(datos.get(k, "")).strip()
                    for k in ("NOMBRE_1", "NOMBRE_2", "APELLIDO_1", "APELLIDO_2")) if p)
    _pend_supabase[to] = {"ts": now, "nombre": nombre, "json_data": datos,
                          "b64_fronta": b64.get("frente"), "b64_trasera": b64.get("reverso")}
    await wa_send(to, _PREGUNTA_PEND, phone_id, client)


async def manejar_curp(texto, to, phone_id, client):
    """/curp <datos> -> calcula la CURP (gen-docs /curp, estructura el texto con DeepSeek)."""
    if not GENDOCS_URL:
        await wa_send(to, "⚠️ Falta configurar GENDOCS_URL.", phone_id, client)
        return
    datos_texto = texto[len(CURP_COMMAND):].strip()
    if not datos_texto:
        await wa_send(to, f"Uso: {CURP_COMMAND} <nombre completo, sexo, fecha y lugar de nacimiento>\n"
                          f"Ej: {CURP_COMMAND} Juan Perez Lopez, hombre, 1985-01-01, CDMX",
                      phone_id, client)
        return
    try:
        r = await client.post(f"{GENDOCS_URL}/curp", json={"texto": datos_texto}, timeout=GENDOCS_TIMEOUT)
        try:
            j = r.json()
        except Exception:
            j = {}
    except httpx.HTTPError as ex:
        await wa_send(to, f"⚠️ Error llamando a gen-docs (/curp): {str(ex)[:120]}", phone_id, client)
        return
    if r.status_code >= 400 or not j.get("curp"):
        await wa_send(to, "⚠️ No pude calcular la CURP: "
                          f"{j.get('error') or ('HTTP ' + str(r.status_code))}\n\n"
                          "Para la CURP necesito, además del nombre y apellidos: *sexo*, "
                          "*fecha de nacimiento* (ej. 1985-03-15) y *estado de nacimiento*.\n"
                          f"Ej: {CURP_COMMAND} Juan Perez Lopez, hombre, 1985-03-15, Jalisco",
                      phone_id, client)
        return
    d = j.get("datos") or {}
    nombre = " ".join(x for x in [d.get("NOMBRE_1", ""), d.get("NOMBRE_2", ""),
                                  d.get("APELLIDO_1", ""), d.get("APELLIDO_2", "")] if x).strip()
    msg = f"🪪 *CURP*: {j['curp']}"
    if nombre:
        msg += f"\n{nombre}"
    adv = j.get("advertencias") or []
    if adv:
        msg += "\n\n⚠️ " + "\n⚠️ ".join(str(a) for a in adv)
    await wa_send(to, msg, phone_id, client)


async def manejar_pendiente(texto, low, pend, to, phone_id, client):
    """Respuesta a la pregunta subir/editar/no de una captura pendiente."""
    if low in _SUBIR:
        _pend_supabase.pop(to, None)
        await _guardar_pendiente(pend, to, phone_id, client)
        return
    if low in _NO:
        _pend_supabase.pop(to, None)
        await wa_send(to, "👍 Ok, descarté esta captura (no se guardó).", phone_id, client)
        return
    # Cualquier otra cosa = instrucción de edición para el agente DeepSeek de gen-docs.
    if not GENDOCS_URL:
        await wa_send(to, "⚠️ Falta configurar GENDOCS_URL para editar.", phone_id, client)
        return
    # Invalida YA el pendiente viejo: mientras regenera, un "subir" no debe guardar los
    # datos SIN editar. Se recrea con los datos nuevos si la regeneración funciona.
    _pend_supabase.pop(to, None)
    await wa_send(to, "✏️ Aplicando el cambio y regenerando…", phone_id, client)
    try:
        r = await client.post(f"{GENDOCS_URL}/editar",
                              json={"json_data": pend.get("json_data"), "instruccion": texto},
                              timeout=GENDOCS_TIMEOUT)
        try:
            j = r.json()
        except Exception:
            j = {}
    except httpx.HTTPError as ex:
        _pend_supabase[to] = {**pend, "ts": time.time()}   # restaura: no perder la captura
        await wa_send(to, f"⚠️ Error al editar (tu captura anterior sigue pendiente): {str(ex)[:120]}",
                      phone_id, client)
        return
    if r.status_code >= 400 or not j.get("datos"):
        _pend_supabase[to] = {**pend, "ts": time.time()}
        await wa_send(to, "⚠️ No pude editar (tu captura anterior sigue pendiente): "
                          f"{j.get('error') or ('HTTP ' + str(r.status_code))}", phone_id, client)
        return
    await _generar_y_enviar(j["datos"], to, phone_id, client, encabezado="🔁 *JSON actualizado*")


async def _guardar_pendiente(pend, to, phone_id, client):
    """Sube a Supabase la captura pendiente (gen-docs /guardar), sin regenerar."""
    if not GENDOCS_URL:
        await wa_send(to, "⚠️ Falta configurar GENDOCS_URL para poder guardar.", phone_id, client)
        return
    payload = {"nombre_completp": pend.get("nombre"), "json_data": pend.get("json_data"),
               "b64_fronta": pend.get("b64_fronta"), "b64_trasera": pend.get("b64_trasera")}
    try:
        r = await client.post(f"{GENDOCS_URL}/guardar", json=payload, timeout=GENDOCS_TIMEOUT)
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code < 400 and j.get("ok"):
            await wa_send(to, f"✅ Guardado en Supabase (id {j.get('id')}).", phone_id, client)
        else:
            await wa_send(to, f"⚠️ No se guardó: {j.get('error') or ('HTTP ' + str(r.status_code))}",
                          phone_id, client)
    except httpx.HTTPError as ex:
        await wa_send(to, f"⚠️ Error guardando en Supabase: {str(ex)[:120]}", phone_id, client)


async def procesar(msg: dict, value: dict):
    """Tarea en segundo plano: consulta y responde. Nunca debe lanzar excepción."""
    to = msg.get("from", "")
    phone_id = (value.get("metadata") or {}).get("phone_number_id") or PHONE_NUMBER_ID
    texto = ((msg.get("text") or {}).get("body") or "").strip()
    async with httpx.AsyncClient() as client:
        try:
            low = texto.lower()
            # ¿hay una captura pendiente (tras /gen) esperando subir/editar/no?
            pend = _pend_supabase.get(to)
            if pend and time.time() - pend["ts"] > PEND_TTL:
                _pend_supabase.pop(to, None)          # venció: descártalo y libera los b64
                pend = None
            if pend and not low.startswith("/"):
                # Cualquier texto que NO sea comando = subir / no / instrucción de edición.
                await manejar_pendiente(texto, low, pend, to, phone_id, client)
                return
            if low.startswith(REPROG_COMMAND):
                await manejar_reprogramar(texto, to, phone_id, client)
                return
            if low.startswith(GENDOCS_COMMAND):
                await manejar_ine(texto, to, phone_id, client)
                return
            if low.startswith(CURP_COMMAND):
                await manejar_curp(texto, to, phone_id, client)
                return
            if not low.startswith(COMMAND):
                await wa_send(to, AYUDA, phone_id, client)
                return
            cuentas, truncado = parsear_cuentas(texto[len(COMMAND):])
            if not cuentas:
                await wa_send(to, "No vi cuentas válidas (8 dígitos).\n" + AYUDA, phone_id, client)
                return
            if not await backend_vivo(client):
                await wa_send(to, "⚠️ Servicio de Siebel no disponible ahora. Intenta más tarde.", phone_id, client)
                return

            ack = f"🔎 Consultando {len(cuentas)} cuenta(s)… te las mando por partes."
            if truncado:
                ack += f"\n(solo las primeras {MAX_CUENTAS})"
            await wa_send(to, ack, phone_id, client)

            sem = asyncio.Semaphore(WA_CONCURRENCIA)

            async def _una(c):
                async with sem:
                    d = {"cuenta": c, "error": "sin intentos"}
                    for intento in range(WA_REINTENTOS):
                        try:
                            d = await consultar_cuenta(c, client)
                        except Exception as ex:
                            d = {"cuenta": c, "error": str(ex)[:80]}
                        if not (d.get("error") is not None and "ordenes" not in d):
                            return d                       # éxito
                        if intento < WA_REINTENTOS - 1:
                            await asyncio.sleep(WA_REINTENTO_ESPERA)   # reintenta tras una pausa
                    return d                               # agotó reintentos

            # Streaming: despacha 2 en paralelo y manda UN mensaje por cuenta (en orden).
            acumulado = []
            for i in range(0, len(cuentas), WA_BATCH):
                grupo = cuentas[i:i + WA_BATCH]
                datos = await asyncio.gather(*[_una(c) for c in grupo])   # mantiene el orden del grupo
                acumulado.extend(datos)
                for d in datos:
                    for ch in construir_chunks([resumir_cuenta(d)]):     # 1 cuenta = 1 mensaje (parte si es enorme)
                        await wa_send(to, ch, phone_id, client)
                        await asyncio.sleep(0.4)        # pacing suave entre mensajes

            exitosas = [d for d in acumulado if not (d.get("error") is not None and "ordenes" not in d)]
            if not exitosas:
                await wa_send(to, "⚠️ No pude consultar ninguna cuenta (Siebel no disponible).", phone_id, client)
                return

            # Mensaje final APARTE: resumen + recomendación por cuenta (DeepSeek)
            resumen = await resumen_ia(acumulado, client)
            if resumen:
                for ch in construir_chunks(["🧠 *Resumen y sugerencias*\n\n" + resumen]):
                    await wa_send(to, ch, phone_id, client)
                    await asyncio.sleep(0.4)
        except Exception:
            log.exception("error procesando mensaje de %s", to)
            try:
                await wa_send(to, "⚠️ Ocurrió un error procesando tu consulta.", phone_id, client)
            except Exception:
                pass


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ FASTAPI — webhook                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MOTOR
    log.info("Backend=%s | Graph=%s | puerto=%s | api=%s",
             ESTATUS_BACKEND, GRAPH_VERSION, PORT, ESTATUS_API_URL)
    if not ACCESS_TOKEN:
        log.warning("WHATSAPP_TOKEN vacío: el bot recibe pero NO podrá responder.")
    if not APP_SECRET:
        log.warning("WHATSAPP_APP_SECRET vacío: NO se valida la firma (solo para pruebas).")
    if ESTATUS_BACKEND == "embedded":
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(f"{ESTATUS_API_URL}/health", timeout=4)
                api_arriba = r.status_code == 200
            except Exception:
                api_arriba = False
        if api_arriba and not ESTATUS_EMBEDDED_FORCE:
            log.error("MODO EMBEDDED ABORTADO: el API de estatus está ARRIBA en %s. "
                      "Correr ambos con el MISMO usuario Siebel se pelea la sesión. "
                      "Usa ESTATUS_BACKEND=api, o apaga el API, o fuerza con ESTATUS_EMBEDDED_FORCE=1.",
                      ESTATUS_API_URL)
            raise SystemExit(1)
        MOTOR = MotorEmbedded()
        log.info("Motor embebido listo (perfil 'whatsapp').")
    try:
        yield
    finally:
        if MOTOR is not None:
            MOTOR._reset()          # logoff de la sesión Siebel embebida


app = FastAPI(title="WhatsApp Estatus Bot", version="1.0", lifespan=lifespan)
_TAREAS = set()   # referencia fuerte para que no las recolecte el GC


def firma_valida(raw: bytes, header: str) -> bool:
    if not APP_SECRET:        # sin secreto configurado: no validar (solo dev)
        return True
    if not header:
        return False
    esperado = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, header)


@app.get("/health")
def health():
    return {"status": "ok", "backend": ESTATUS_BACKEND, "graph": GRAPH_VERSION}


@app.get("/webhook")
def verificar(hub_mode: str = Query(None, alias="hub.mode"),
              hub_verify_token: str = Query(None, alias="hub.verify_token"),
              hub_challenge: str = Query(None, alias="hub.challenge")):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return Response(content=hub_challenge or "", media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    if not firma_valida(raw, request.headers.get("x-hub-signature-256")):
        return Response(status_code=403)
    try:
        data = json.loads(raw)
    except Exception:
        return Response(status_code=200)   # ack y ya; no reintentos

    for entry in data.get("entry", []):
        for ch in entry.get("changes", []):
            value = ch.get("value", {}) or {}
            if "messages" not in value:    # statuses/receipts u otros -> ignorar
                continue
            for msg in value.get("messages", []):
                mid = msg.get("id", "")
                if not visto_y_marcar(mid):
                    continue               # duplicado (reintento de Meta)
                wa_id = msg.get("from", "")
                if msg.get("type") != "text":
                    # tipo no soportado: ayuda corta (con rate-limit, sin reservar inflight)
                    if rate_ok(wa_id):
                        t = asyncio.create_task(procesar({"from": wa_id, "text": {"body": ""}}, value))
                        _TAREAS.add(t); t.add_done_callback(_TAREAS.discard)
                    continue
                _resp = ((msg.get("text") or {}).get("body") or "").strip().lower()
                if _resp in (_SUBIR | _NO) and wa_id in _pend_supabase and wa_id not in _inflight:
                    # subir/no de una captura pendiente y SIN nada en curso: barato, sin
                    # rate-limit. Si hay algo corriendo (p.ej. una edición), cae al aviso
                    # normal de "en curso" para no guardar datos a medio regenerar.
                    t = asyncio.create_task(procesar(msg, value))
                elif wa_id in _inflight:
                    t = asyncio.create_task(_avisar(wa_id, value, "⏳ Ya tengo una consulta tuya en curso, espera a que termine."))
                elif not rate_ok(wa_id):
                    t = asyncio.create_task(_avisar(wa_id, value, "🚦 Vas muy rápido. Espera unos minutos e intenta de nuevo."))
                else:
                    _inflight.add(wa_id)
                    t = asyncio.create_task(_correr(msg, value))
                _TAREAS.add(t); t.add_done_callback(_TAREAS.discard)
    return Response(status_code=200)


async def _correr(msg, value):
    try:
        await procesar(msg, value)
    finally:
        _inflight.discard(msg.get("from", ""))


async def _avisar(wa_id, value, texto):
    phone_id = (value.get("metadata") or {}).get("phone_number_id") or PHONE_NUMBER_ID
    async with httpx.AsyncClient() as client:
        await wa_send(wa_id, texto, phone_id, client)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ CLI (pruebas) + arranque                                                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝
async def _probar(cuentas):
    async with httpx.AsyncClient() as client:
        if ESTATUS_BACKEND == "embedded":
            global MOTOR
            MOTOR = MotorEmbedded()
        if not await backend_vivo(client):
            print("⚠️ Backend Siebel no disponible "
                  f"({'API en ' + ESTATUS_API_URL if ESTATUS_BACKEND == 'api' else 'embedded'}).")
            return
        bloques = []
        for c in cuentas:
            d = await consultar_cuenta(c, client)
            bloques.append(resumir_cuenta(d))
        for ch in construir_chunks(bloques):
            print("\n" + "=" * 48 + "\n" + ch)


async def _enviar(to, cuentas):
    async with httpx.AsyncClient() as client:
        if ESTATUS_BACKEND == "embedded":
            global MOTOR
            MOTOR = MotorEmbedded()
        await procesar({"from": to, "text": {"body": f"{COMMAND} {' '.join(cuentas)}"}},
                       {"metadata": {"phone_number_id": PHONE_NUMBER_ID}})


def main():
    ap = argparse.ArgumentParser(description="Bot WhatsApp de estatus Siebel")
    ap.add_argument("--probar", nargs="+", metavar="CUENTA", help="imprime la respuesta sin WhatsApp")
    ap.add_argument("--enviar", nargs="+", metavar="ARG", help="<to> <cuenta...> manda un WhatsApp de prueba")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    if args.probar:
        asyncio.run(_probar([c for c in args.probar if c.isdigit()]))
        return
    if args.enviar:
        if len(args.enviar) < 2:
            print("Uso: --enviar <numero_destino> <cuenta> [cuenta...]")
            return
        to, cuentas = args.enviar[0], [c for c in args.enviar[1:] if c.isdigit()]
        asyncio.run(_enviar(to, cuentas))
        return

    # Servidor (1 solo worker: dedupe/sesión en memoria lo requieren)
    uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
