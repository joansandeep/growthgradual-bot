FROM python:3.12-slim

# Chromium + its driver for the fast/primary PDF render path (routes/pdf.py,
# GG_PDF_RENDER_ENGINE=chromium). WeasyPrint (53+) needs no system libraries
# of its own — it's pure Python — so nothing else has to be installed for the
# WeasyPrint fallback path to keep working.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render injects $PORT at runtime; default kept for local `docker run` testing.
ENV PORT=10000
EXPOSE 10000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
