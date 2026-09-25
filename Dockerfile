# IVZ Carbon en un contenedor, con Tesseract para leer facturas y manifiestos escaneados o en foto.
# La misma imagen sirve para Azure (Container Apps / App Service) y SAP BTP (Cloud Foundry / Kyma).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CARBON_ENV=production

# Tesseract con el idioma español; sin él, backend/invoice_parser.py responde 'ocr-unavailable'.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY static ./static
COPY seed.json .

RUN useradd --create-home carbon
USER carbon

# Cloud Foundry (BTP) y App Service indican el puerto en PORT; el resto usa 8001.
EXPOSE 8001
CMD ["sh", "-c", "exec uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8001} --proxy-headers --forwarded-allow-ips '*'"]
