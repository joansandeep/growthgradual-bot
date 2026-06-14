FROM python:3.11-slim

# ── System deps ────────────────────────────────────────────────────────────────
# swig is needed if faiss-cpu falls back to building from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgfortran5 \
    swig \
    && rm -rf /var/lib/apt/lists/*

# ── HF Spaces: must run as non-root user 1000 ──────────────────────────────────
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR $HOME/app

# ── Python dependencies ────────────────────────────────────────────────────────
COPY --chown=user requirements.txt .

# Install torch CPU-only FIRST (small wheel, avoids pulling CUDA)
# then the rest. This keeps the image well under the 10 GB HF free limit.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        torch==2.3.1+cpu \
        --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# ── Pre-download the embedding model so cold-starts are instant ────────────────
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# ── App code ───────────────────────────────────────────────────────────────────
COPY --chown=user . .

# HF Spaces Docker SDK: app must listen on 7860
ENV RAG_PORT=7860
EXPOSE 7860

CMD ["python", "main.py"]
