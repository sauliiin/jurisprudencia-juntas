"""Aplicação FastAPI: API do acervo e, por padrão, o site estático no mesmo processo.

uvicorn api.main:app            # ou: python -m api
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from api import __version__, erros
from api.config import Settings, get_settings
from api.logs import configurar_logs, request_id
from api.routes import acervo, saude

log = logging.getLogger("api")

DESCRICAO = """
Busca no acervo público de decisões das Juntas Integradas de Julgamento Fiscal de Belo Horizonte.

O índice é gerado do acervo já mascarado (`python -m api.indice`): sem o nome dos autuados e
sem caminhos internos. A busca usa SQLite FTS5 com ranking BM25.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configurar_logs(settings.log_level)
    app = FastAPI(
        title="OmniAI · Jurisprudência",
        version=__version__,
        description=DESCRICAO,
        docs_url="/docs",
        redoc_url=None,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    erros.registrar(app)

    # JSON da busca, JSONL do modo estático e o site: texto que comprime bem.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )

    @app.middleware("http")
    async def registrar_requisicao(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id.set(rid)
        inicio = time.perf_counter()
        status = 500
        try:
            resposta = await call_next(request)
            status = resposta.status_code
            resposta.headers["X-Request-ID"] = rid
            if request.url.path.startswith("/api/") and "cache-control" not in resposta.headers:
                resposta.headers["Cache-Control"] = "no-store"
            return resposta
        finally:
            log.info(
                "requisicao",
                extra={
                    "metodo": request.method,
                    "rota": request.url.path,
                    "status": status,
                    "duracao_ms": round((time.perf_counter() - inicio) * 1000, 1),
                },
            )
            request_id.reset(token)

    app.include_router(saude.router)
    app.include_router(acervo.router)

    if settings.servir_site:
        # Mesma estrutura do GitHub Pages: o site acha ../site_data/ como lá.
        if settings.dados_dir.is_dir():
            app.mount("/site_data", StaticFiles(directory=settings.dados_dir), name="site_data")
        if settings.site_dir.is_dir():
            app.mount("/site_publico", StaticFiles(directory=settings.site_dir, html=True), name="site")

            @app.get("/", include_in_schema=False)
            def raiz() -> RedirectResponse:
                return RedirectResponse("/site_publico/")

    return app


app = create_app()
