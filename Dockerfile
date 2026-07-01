# Webhook de WhatsApp Estatus para Koyeb (u otro PaaS).
# El API de Siebel NO vive aquí: se consume por HTTPS (ESTATUS_API_URL) desde tu PC.
FROM python:3.12-slim

# tk: cn_master_app.py (usado por /reprogramar) importa tkinter a nivel de módulo.
# Sin esto, importar el módulo revienta y /reprogramar dejaría de funcionar.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-tk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Koyeb inyecta $PORT; el código lo detecta y escucha en 0.0.0.0 automáticamente.
EXPOSE 8000
CMD ["python", "whatsapp_estatus.py"]
