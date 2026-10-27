# syntax=docker/dockerfile:1

# ---- Estágio 1: dependências num venv isolado ----
FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements-api.txt /tmp/requirements-api.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-compile -r /tmp/requirements-api.txt

# ---- Estágio 2: imagem final, sem pip cache nem ferramentas de build ----
FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="OmniAI · Jurisprudência" \
      org.opencontainers.image.description="API FastAPI e site de busca de decisões administrativas" \
      org.opencontainers.image.source="https://github.com/sauliiin/jurisprudencia-juntas" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JURIS_HOST=0.0.0.0 \
    JURIS_PORT=8000 \
    JURIS_INDICE=/indice/indice_publico.db \
    JURIS_SITE_DIR=/app/site_publico \
    JURIS_DADOS_DIR=/dados/site_data

# Usuário sem privilégios; /indice já pertence a ele para o volume nomeado herdar o dono.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin juris \
    && mkdir -p /indice /dados \
    && chown juris:juris /indice

WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY api ./api
COPY site_publico ./site_publico

USER juris
EXPOSE 8000

# O índice e o acervo (site_data/) entram por volume; veja docker-compose.yml.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status != 200)"]

CMD ["python", "-m", "api"]
