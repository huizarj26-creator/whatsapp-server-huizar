#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
App de escritorio — Crear Casos de Negocio (izzi MisVentas) — TODOS los tipos

Replica EXACTAMENTE la lógica verificada en el bytecode. Hay 3 tipos de caso
(valor de EXTRA_BUSINESS_CASE_TYPE), cada uno con su endpoint y payload:

  SEGUMIENTO        -> POST /crearCN        (BussinessCase: cuenta/orden/comentario/archivos)
  CORRECION         -> POST /crearCNMaster  (NewBCMasterRequest, rama CORRECION)
  FALLA_ACTIVACION  -> POST /crearCNMaster  (NewBCMasterRequest, rama FALLA_ACTIVACION)

Detalles fieles al cliente:
  - Master: campos fijos por rama + cascada de 2 spinners; adjuntos AdjuntoCN
    img(Base64 NO_WRAP)/type/extension(substringAfterLast '.'); header
    x-access-origin: APPMISVENTAS. medioContacto editable (default "App Mis Ventas").
  - Normal (SEGUMIENTO): BussinessCase cuenta(Sale)/orden(Sale)/comentario/archivos;
    adjunto A5.a imagen(Base64 NO_WRAP)/tipo; SIN header x-access-origin.

Solo librería estándar (tkinter, urllib, base64, json). Ejecutar:
    python3 cn_master_app.py
"""

import base64
import json
import os
import queue
import ssl
import threading
import time
import urllib.request
import urllib.error

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ───────────────────────── Lógica de negocio (del bytecode) ─────────────────────────

CATEGORIA = "VALIDACION"          # fijo en Master (ambas ramas)
MEDIO_CONTACTO = "App Mis Ventas"  # default editable en Master

# Tipos de caso (EXTRA_BUSINESS_CASE_TYPE) y su naturaleza
TIPOS_CASO = {
    "SEGUMIENTO":       "normal",   # -> /crearCN
    "CORRECION":        "master",   # -> /crearCNMaster
    "FALLA_ACTIVACION": "master",   # -> /crearCNMaster
}

# Spinner 1 (spinner_motivo) en Master: opciones según la rama
MOTIVO_1 = {
    "CORRECION": [
        "MOD DE INFORMACION CT MASTER",
        "MOD DE INFORMACION OS MASTER",
    ],
    "FALLA_ACTIVACION": [
        "ANALISIS DE FALLA",
    ],
}

# Spinner 2 (spinner_motivo_cliente): cascada según lo elegido en spinner 1
CASCADA_2 = {
    "MOD DE INFORMACION CT MASTER": [  # modificacion_datos_options
        "CORRECCION NUM EXT", "CORRECCION NUM INT", "CORRECCION COLONIA",
        "CORRECCION CALLE", "CORRECCION NOMBRE CTE", "CORRECCION CP",
        "CORRECCION CORREO", "CORRECCION TELEFONO", "CAMBIO TIPO DE CUENTA",
    ],
    "MOD DE INFORMACION OS MASTER": [  # modificacion_os_options
        "ALTA DE PRODUCTOS", "BAJA DE PRODUCTOS",
        "IDNULO/CAM DOMICILIO ERRONEO", "MODIFICACION DE PAQUETE",
    ],
    "ANALISIS DE FALLA": [  # analisis_falla_options
        "ORDEN IN PROGRESS", "CORRECCION DE TECNOLOGIA", "CORREGIR COMPONENTES",
        "SOLICITUD ABIERTA O PENDIENTE", "ORDENES CON ERROR DE ENVIO",
        "ERROR ORDEN DE REEMPLAZO", "ERROR CON MENSAJES EN PANTALLA",
        "CUENTA SIN SEÑAL SALDO A FAVOR", "EMPATAR RECARGA", "ERROR EN ACTIVACION",
        "EQUIPO DUPLICADO", "EQUIPO NO VISIBLE", "ERROR ORDEN DE REEMPLAZO HOTEL",
        "ERROR OS EQUIPO ADICIONAL", "ERROR EN JERARQUIA DE EQUIPOS",
    ],
}
DEFAULT_2 = ["NO OPTION"]  # default_options

# Tipos de documento de adjuntos (distintos por flujo)
TIPOS_DOC_NORMAL = [  # document_types
    "Carta asig. num. no geografico", "Comprobante de Numero Tel",
    "Comprobante de pago", "Contrato", "Factura",
    "Formulario de Solicitud", "Identificacion oficial", "Poder Legal",
]
TIPOS_DOC_MASTER = [  # cnmaster_document_types_options
    "Identificacion oficial", "Comprobante de domicilio", "Certificado de domicilio",
]

# Endpoints
EP = {
    "normal": {
        "izzi":    "https://www.izzi.mx/WSVeL/webservices/hojaruta/crearCN",
        "wrapper": "https://crearcn.idocrm.es/crear-cn",
    },
    "master": {
        "izzi":    "https://www.izzi.mx/WSVeL/webservices/hojaruta/crearCNMaster",
        "wrapper": "https://crearcn.idocrm.es/crear-cn-master",
    },
}


def substring_after_last(name: str, delim: str = ".") -> str:
    """Replica kotlin substringAfterLast('.', "") — solo la extensión real."""
    idx = name.rfind(delim)
    return name[idx + 1:] if idx != -1 else ""


def b64_nowrap(path: str) -> str:
    """Base64 NO_WRAP del archivo; '' si no existe (igual que la app)."""
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return ""


def adjunto_normal(path, tipo):
    """A5.a del flujo normal: imagen / tipo."""
    return {"imagen": b64_nowrap(path), "tipo": tipo}


def adjunto_master(path, tipo):
    """AdjuntoCN del flujo master: img / type / extension."""
    return {
        "img": b64_nowrap(path),
        "type": tipo,
        "extension": substring_after_last(os.path.basename(path)),
    }


def construir_payload_normal(cuenta, orden, comentario, adjuntos_paths):
    """BussinessCase del /crearCN. archivos = None si no hay adjuntos."""
    archivos = [adjunto_normal(p, t) for p, t in adjuntos_paths]
    return {
        "cuenta": cuenta,
        "orden": orden,
        "comentario": comentario,
        "archivos": archivos if archivos else None,
    }


def construir_payload_master(tipo_caso, cuenta, comentarios, motivo1, motivo2,
                             adjuntos_paths, medio_contacto=MEDIO_CONTACTO):
    """
    NewBCMasterRequest EXACTO según la rama.
      motivo1 = spinner_motivo (spinner 1)
      motivo2 = spinner_motivo_cliente (spinner 2) -> SIEMPRE motivoCliente
      medio_contacto = editable (default "App Mis Ventas"); puede ir vacío ""
    """
    if tipo_caso == "CORRECION":
        motivo = "MODIFICACION DE DATOS CUENTA"   # fijo
        submotivo = motivo1                        # spinner 1
        solucion = "SE CANALIZA A VALIDACION"      # fijo
    elif tipo_caso == "FALLA_ACTIVACION":
        motivo = motivo1                           # spinner 1
        submotivo = "VENTA MASTER"                 # fijo
        solucion = "ESCALAMIENTO"                  # fijo
    else:
        raise ValueError(f"tipo no Master: {tipo_caso}")

    file_ = [adjunto_master(p, t) for p, t in adjuntos_paths]
    return {
        "categoria": CATEGORIA,
        "comentarios": comentarios,
        "cuenta": cuenta,
        "medioContacto": medio_contacto,
        "motivo": motivo,
        "motivoCliente": motivo2,
        "solucion": solucion,
        "submotivo": submotivo,
        "file": file_ if file_ else None,
    }


# ───────────────────────────── Interfaz ─────────────────────────────

class CNApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Crear Casos de Negocio — izzi MisVentas (todos los tipos)")
        self.geometry("780x920")
        self.minsize(740, 820)

        self.adjuntos = []   # lista de (path, tipo)
        self._q = queue.Queue()

        self._build_ui()
        self._on_type_change()
        self.after(100, self._poll_queue)

    # ---- construcción de UI ----
    def _build_ui(self):
        pad = dict(padx=8, pady=4)
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # Tipo de caso
        box_t = ttk.LabelFrame(root, text="Tipo de caso de negocio", padding=8)
        box_t.pack(fill="x", **pad)
        self.var_type = tk.StringVar(value="SEGUMIENTO")
        for i, t in enumerate(TIPOS_CASO):
            sub = "/crearCN" if TIPOS_CASO[t] == "normal" else "/crearCNMaster"
            ttk.Radiobutton(box_t, text=f"{t}   ({sub})", variable=self.var_type,
                            value=t, command=self._on_type_change).grid(
                                row=i, column=0, sticky="w")

        # Destino
        box_ep = ttk.LabelFrame(root, text="Destino", padding=8)
        box_ep.pack(fill="x", **pad)
        self.var_dest = tk.StringVar(value="izzi")
        ttk.Radiobutton(box_ep, text="Directo a izzi.mx (como la app)",
                        variable=self.var_dest, value="izzi",
                        command=self._sync_endpoint).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(box_ep, text="Vía wrapper (crearcn.idocrm.es)",
                        variable=self.var_dest, value="wrapper",
                        command=self._sync_endpoint).grid(row=0, column=1, sticky="w")
        ttk.Label(box_ep, text="URL:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.var_url = tk.StringVar()
        ttk.Entry(box_ep, textvariable=self.var_url, width=72).grid(
            row=1, column=1, sticky="we", pady=(6, 0))
        ttk.Label(box_ep, text="Bearer token (solo wrapper):").grid(row=2, column=0, sticky="w")
        self.var_token = tk.StringVar()
        self.ent_token = ttk.Entry(box_ep, textvariable=self.var_token, width=72, show="•")
        self.ent_token.grid(row=2, column=1, sticky="we")
        box_ep.columnconfigure(1, weight=1)

        # Datos
        box = ttk.LabelFrame(root, text="Datos del caso", padding=8)
        box.pack(fill="x", **pad)
        ttk.Label(box, text="Cuenta *").grid(row=0, column=0, sticky="w")
        self.var_cuenta = tk.StringVar()
        ttk.Entry(box, textvariable=self.var_cuenta, width=40).grid(row=0, column=1, sticky="w")

        # Orden (solo normal)
        self.lbl_orden = ttk.Label(box, text="Orden")
        self.var_orden = tk.StringVar()
        self.ent_orden = ttk.Entry(box, textvariable=self.var_orden, width=40)

        # medioContacto (solo master)
        self.lbl_medio = ttk.Label(box, text="Medio de contacto")
        self.var_medio = tk.StringVar(value=MEDIO_CONTACTO)
        self.fr_medio = ttk.Frame(box)
        ttk.Entry(self.fr_medio, textvariable=self.var_medio, width=30).pack(side="left")
        ttk.Button(self.fr_medio, text="Vaciar",
                   command=lambda: self.var_medio.set("")).pack(side="left", padx=(6, 0))
        ttk.Button(self.fr_medio, text="Default",
                   command=lambda: self.var_medio.set(MEDIO_CONTACTO)).pack(side="left", padx=(4, 0))

        # Spinners (solo master)
        self.lbl_m1 = ttk.Label(box, text="Motivo (spinner 1) *")
        self.var_m1 = tk.StringVar()
        self.cmb_m1 = ttk.Combobox(box, textvariable=self.var_m1, state="readonly", width=38)
        self.cmb_m1.bind("<<ComboboxSelected>>", lambda e: self._on_m1_change())

        self.lbl_m2 = ttk.Label(box, text="Detalle / motivoCliente (spinner 2) *")
        self.var_m2 = tk.StringVar()
        self.cmb_m2 = ttk.Combobox(box, textvariable=self.var_m2, state="readonly", width=38)

        box.columnconfigure(1, weight=1)
        self._box = box

        # Info de campos fijos
        self.lbl_fijos = ttk.Label(root, text="", foreground="#555", justify="left")
        self.lbl_fijos.pack(fill="x", padx=12)

        # Comentarios
        self.box_c = ttk.LabelFrame(root, text="Comentario", padding=8)
        self.box_c.pack(fill="both", **pad)
        self.txt_com = scrolledtext.ScrolledText(self.box_c, height=4, wrap="word")
        self.txt_com.pack(fill="both", expand=True)

        # Adjuntos
        box_a = ttk.LabelFrame(root, text="Adjuntos (opcional, máx 10)", padding=8)
        box_a.pack(fill="both", **pad)
        fr_btns = ttk.Frame(box_a)
        fr_btns.pack(fill="x")
        ttk.Button(fr_btns, text="+ Agregar archivo", command=self._add_file).pack(side="left")
        ttk.Button(fr_btns, text="− Quitar selección", command=self._del_file).pack(side="left", padx=6)
        self.lst_adj = tk.Listbox(box_a, height=4)
        self.lst_adj.pack(fill="both", expand=True, pady=(6, 0))

        # Acciones
        fr_act = ttk.Frame(root)
        fr_act.pack(fill="x", **pad)
        ttk.Button(fr_act, text="Ver JSON", command=self._preview).pack(side="left")
        ttk.Button(fr_act, text="Guardar JSON…", command=self._save_json).pack(side="left", padx=6)
        self.btn_send = ttk.Button(fr_act, text="Enviar  ▶", command=self._send)
        self.btn_send.pack(side="right")

        # Log
        box_l = ttk.LabelFrame(root, text="Logs (request / response)", padding=8)
        box_l.pack(fill="both", expand=True, **pad)
        fr_lbtn = ttk.Frame(box_l)
        fr_lbtn.pack(fill="x")
        self.var_verbose = tk.BooleanVar(value=True)
        ttk.Checkbutton(fr_lbtn, text="Mostrar request (headers+body)",
                        variable=self.var_verbose).pack(side="left")
        ttk.Button(fr_lbtn, text="Copiar", command=self._copy_log).pack(side="right")
        ttk.Button(fr_lbtn, text="Limpiar", command=self._clear_log).pack(side="right", padx=6)
        self.txt_log = scrolledtext.ScrolledText(box_l, height=12, wrap="word", state="disabled")
        self.txt_log.pack(fill="both", expand=True, pady=(6, 0))
        # colores por severidad
        self.txt_log.tag_config("ok", foreground="#1a7f37")
        self.txt_log.tag_config("warn", foreground="#9a6700")
        self.txt_log.tag_config("err", foreground="#cf222e")
        self.txt_log.tag_config("dim", foreground="#57606a")

    # ---- helpers de estado ----
    def _kind(self):
        return TIPOS_CASO[self.var_type.get()]

    def _tipos_doc(self):
        # Verificado en BusinessCaseAddFileSetupDialogFragment.smali: el picker se
        # inicializa SIEMPRE a document_types (8) y SOLO se sobrescribe a los 3
        # cnmaster_document_types_options cuando el tipo == "CORRECION" exacto.
        # => FALLA_ACTIVACION y SEGUMIENTO usan los 8; solo CORRECION usa los 3.
        return TIPOS_DOC_MASTER if self.var_type.get() == "CORRECION" else TIPOS_DOC_NORMAL

    # ---- handlers ----
    def _on_type_change(self):
        kind = self._kind()
        # Mostrar/ocultar campos según el tipo
        self.lbl_orden.grid_forget(); self.ent_orden.grid_forget()
        self.lbl_medio.grid_forget(); self.fr_medio.grid_forget()
        self.lbl_m1.grid_forget(); self.cmb_m1.grid_forget()
        self.lbl_m2.grid_forget(); self.cmb_m2.grid_forget()

        if kind == "normal":
            self.lbl_orden.grid(row=1, column=0, sticky="w", pady=(6, 0))
            self.ent_orden.grid(row=1, column=1, sticky="w", pady=(6, 0))
            self.box_c.configure(text="Comentario")
        else:
            self.lbl_medio.grid(row=1, column=0, sticky="w", pady=(6, 0))
            self.fr_medio.grid(row=1, column=1, sticky="we", pady=(6, 0))
            self.lbl_m1.grid(row=2, column=0, sticky="w", pady=(6, 0))
            self.cmb_m1.grid(row=2, column=1, sticky="w", pady=(6, 0))
            self.lbl_m2.grid(row=3, column=0, sticky="w", pady=(6, 0))
            self.cmb_m2.grid(row=3, column=1, sticky="w", pady=(6, 0))
            self.box_c.configure(text="Comentarios")
            opciones = MOTIVO_1[self.var_type.get()]
            self.cmb_m1["values"] = opciones
            self.var_m1.set(opciones[0])
            self._on_m1_change()

        # Las opciones de tipo de adjunto cambian; limpiar lista para evitar mezclar tipos
        if self.adjuntos:
            self.adjuntos.clear()
            self.lst_adj.delete(0, "end")
        self._refresh_fijos()
        self._sync_endpoint()

    def _on_m1_change(self):
        opciones = CASCADA_2.get(self.var_m1.get(), DEFAULT_2)
        self.cmb_m2["values"] = opciones
        self.var_m2.set(opciones[0] if opciones else "")
        self._refresh_fijos()

    def _sync_endpoint(self):
        kind = self._kind()
        dest = self.var_dest.get()
        self.var_url.set(EP[kind][dest])
        self.ent_token.configure(state=("normal" if dest == "wrapper" else "disabled"))

    def _refresh_fijos(self):
        t = self.var_type.get()
        if t == "SEGUMIENTO":
            txt = ("Flujo NORMAL /crearCN → payload: cuenta · orden · comentario · archivos[imagen,tipo]. "
                   "Sin header x-access-origin. 8 tipos de documento.")
        elif t == "CORRECION":
            txt = ("Flujo MASTER /crearCNMaster → fijos: categoria=VALIDACION · "
                   "motivo=MODIFICACION DE DATOS CUENTA · solucion=SE CANALIZA A VALIDACION\n"
                   "submotivo = spinner 1 · motivoCliente = spinner 2 · medioContacto editable")
        else:  # FALLA_ACTIVACION
            txt = ("Flujo MASTER /crearCNMaster → fijos: categoria=VALIDACION · "
                   "submotivo=VENTA MASTER · solucion=ESCALAMIENTO\n"
                   "motivo = spinner 1 · motivoCliente = spinner 2 · medioContacto editable")
        self.lbl_fijos.config(text=txt)

    def _add_file(self):
        if len(self.adjuntos) >= 10:
            messagebox.showwarning("Límite", "Máximo 10 adjuntos.")
            return
        path = filedialog.askopenfilename(title="Selecciona archivo")
        if not path:
            return
        tipo = self._ask_tipo()
        if not tipo:
            return
        self.adjuntos.append((path, tipo))
        self.lst_adj.insert("end", f"{os.path.basename(path)}   [{tipo}]")

    def _ask_tipo(self):
        dlg = tk.Toplevel(self)
        dlg.title("Tipo de documento")
        dlg.transient(self); dlg.grab_set()
        ttk.Label(dlg, text="Tipo de documento:").pack(padx=12, pady=(12, 4))
        tipos = self._tipos_doc()
        var = tk.StringVar(value=tipos[0])
        ttk.Combobox(dlg, textvariable=var, values=tipos,
                     state="readonly", width=34).pack(padx=12, pady=4)
        res = {"v": None}
        ttk.Button(dlg, text="Aceptar",
                   command=lambda: (res.update(v=var.get()), dlg.destroy())).pack(pady=10)
        self.wait_window(dlg)
        return res["v"]

    def _del_file(self):
        sel = self.lst_adj.curselection()
        if not sel:
            return
        i = sel[0]
        self.lst_adj.delete(i)
        del self.adjuntos[i]

    # ---- payload ----
    def _build_payload(self):
        cuenta = self.var_cuenta.get().strip()
        if not cuenta:
            messagebox.showerror("Falta dato", "La cuenta es obligatoria.")
            return None
        if self._kind() == "normal":
            comentario = self.txt_com.get("1.0", "end").strip()
            return construir_payload_normal(
                cuenta, self.var_orden.get().strip(), comentario, self.adjuntos)
        else:
            comentarios = self.txt_com.get("1.0", "end").strip()
            return construir_payload_master(
                self.var_type.get(), cuenta, comentarios,
                self.var_m1.get(), self.var_m2.get(), self.adjuntos,
                medio_contacto=self.var_medio.get())

    def _payload_legible(self, payload):
        p = json.loads(json.dumps(payload))
        for key in ("file", "archivos"):
            if p.get(key):
                for a in p[key]:
                    for k in ("img", "imagen"):
                        if a.get(k) and len(a[k]) > 40:
                            a[k] = a[k][:40] + f"...(+{len(a[k])-40} chars)"
        return p

    def _preview(self):
        payload = self._build_payload()
        if payload is None:
            return
        self._log(json.dumps(self._payload_legible(payload), indent=2, ensure_ascii=False), clear=True)

    def _save_json(self):
        payload = self._build_payload()
        if payload is None:
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        self._log(f"Guardado en {path}", clear=True)

    def _send(self):
        payload = self._build_payload()
        if payload is None:
            return
        url = self.var_url.get().strip()
        dest = self.var_dest.get()
        kind = self._kind()
        token = self.var_token.get().strip()
        if dest == "wrapper" and not token:
            if not messagebox.askyesno("Sin token",
                                       "El wrapper normalmente requiere Bearer token. ¿Enviar igual?"):
                return
        if not messagebox.askyesno("Confirmar", f"Tipo: {self.var_type.get()}\nEnviar a:\n{url} ?"):
            return
        self.btn_send.configure(state="disabled")
        self._log(f"━━━ [{self.var_type.get()}] POST {url}", clear=True, tag="dim")
        threading.Thread(target=self._do_post,
                         args=(url, dest, kind, token, payload, self.var_verbose.get()),
                         daemon=True).start()

    def _do_post(self, url, dest, kind, token, payload, verbose):
        t0 = time.monotonic()
        try:
            body = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if dest == "izzi":
                headers["User-Agent"] = "okhttp/4.12.0"
                if kind == "master":  # solo Master lleva x-access-origin
                    headers["x-access-origin"] = "APPMISVENTAS"
            else:  # wrapper
                if token:
                    headers["Authorization"] = f"Bearer {token}"

            if verbose:
                hdr_show = dict(headers)
                if "Authorization" in hdr_show:
                    hdr_show["Authorization"] = "Bearer ***"  # no filtrar token al log
                hlines = "\n".join(f"    {k}: {v}" for k, v in hdr_show.items())
                pretty = json.dumps(self._payload_legible(payload), indent=2, ensure_ascii=False)
                self._q.put(("log", None,
                             f"REQUEST HEADERS:\n{hlines}\nREQUEST BODY ({len(body)} bytes):\n{pretty}"))

            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                dt = time.monotonic() - t0
                self._q.put(("ok", resp.status, self._fmt_resp(resp.read(), dt)))
        except urllib.error.HTTPError as e:
            dt = time.monotonic() - t0
            self._q.put(("http", e.code, self._fmt_resp(e.read(), dt)))
        except Exception as e:  # noqa
            dt = time.monotonic() - t0
            self._q.put(("err", 0, f"{type(e).__name__}: {e}  ({dt:.2f}s)"))

    @staticmethod
    def _fmt_resp(raw, dt):
        txt = raw.decode("utf-8", "replace")
        try:  # intenta formatear JSON
            txt = json.dumps(json.loads(txt), indent=2, ensure_ascii=False)
        except ValueError:
            pass
        return f"({dt:.2f}s)\n{txt}"

    def _poll_queue(self):
        try:
            while True:
                kind, code, txt = self._q.get_nowait()
                if kind == "log":
                    self._log(txt, tag="dim")
                    continue
                tag = {"ok": "ok", "http": "warn", "err": "err"}[kind]
                icon = {"ok": "✅", "http": "⚠️", "err": "❌"}[kind]
                head = (f"{icon} HTTP {code}" if kind != "err"
                        else f"{icon} Error de red")
                self._log(f"{head} {txt}", tag=tag)
                self.btn_send.configure(state="normal")
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)

    def _copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.txt_log.get("1.0", "end").strip())

    def _clear_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def _log(self, msg, clear=False, tag=None):
        self.txt_log.configure(state="normal")
        if clear:
            self.txt_log.delete("1.0", "end")
        ts = time.strftime("%H:%M:%S")
        self.txt_log.insert("end", f"[{ts}] ", "dim")
        self.txt_log.insert("end", msg + "\n", tag or ())
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")


if __name__ == "__main__":
    CNApp().mainloop()
