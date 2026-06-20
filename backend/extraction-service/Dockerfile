FROM node:20-slim

# Tesseract OCR (only needed for extraction-service, harmless in others)
RUN apt-get update && apt-get install -y \
    tesseract-ocr tesseract-ocr-eng \
    python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production
COPY . .

RUN mkdir -p /app/uploads /tmp/extraction

EXPOSE 4003
CMD ["node", "server.js"]
