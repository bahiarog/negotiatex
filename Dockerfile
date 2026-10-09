FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
    libfontconfig1 libgdk-pixbuf-xlib-2.0-0 libcairo2 poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Embedding-Modell fuer die semantische Belegsuche fest ins Image: zur Laufzeit
# keine Downloads, Dokumenttexte verlassen den Server nie.
ENV MDC_EMBEDDING_CACHE=/opt/models
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('jinaai/jina-embeddings-v2-base-de', cache_dir='/opt/models')"
ENV HF_HUB_OFFLINE=1
COPY app/ .
RUN mkdir -p /app/uploads/offers /app/uploads/exports /app/uploads/mdc
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
