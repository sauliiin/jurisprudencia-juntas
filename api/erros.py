"""Erros em JSON padronizado: {"erro": {"codigo", "mensagem", "detalhes", "request_id"}}."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.db import IndiceIndisponivel
from api.logs import request_id
from api.services.busca import ConsultaInvalida

log = logging.getLogger("api")

_CODIGOS = {
    400: "requisicao_invalida",
    403: "proibido",
    404: "nao_encontrado",
    405: "metodo_nao_permitido",
    422: "parametros_invalidos",
    503: "indisponivel",
}


def resposta_erro(status: int, mensagem: str, detalhes: list[dict] | None = None) -> JSONResponse:
    corpo = {
        "erro": {
            "codigo": _CODIGOS.get(status, "erro_interno"),
            "mensagem": mensagem,
            "detalhes": detalhes,
            "request_id": request_id.get(),
        }
    }
    return JSONResponse(corpo, status_code=status, headers={"Cache-Control": "no-store"})


def registrar(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        mensagem = exc.detail if isinstance(exc.detail, str) else "Erro na requisição"
        return resposta_erro(exc.status_code, mensagem)

    @app.exception_handler(RequestValidationError)
    async def _validacao(_: Request, exc: RequestValidationError) -> JSONResponse:
        detalhes = [
            {"campo": ".".join(str(p) for p in erro["loc"][1:]) or None, "problema": erro["msg"]}
            for erro in exc.errors()
        ]
        return resposta_erro(422, "Parâmetros inválidos", detalhes)

    @app.exception_handler(ConsultaInvalida)
    async def _consulta(_: Request, exc: ConsultaInvalida) -> JSONResponse:
        return resposta_erro(400, "Consulta inválida para a busca textual")

    @app.exception_handler(IndiceIndisponivel)
    async def _indice(_: Request, exc: IndiceIndisponivel) -> JSONResponse:
        log.error("indice_indisponivel", extra={"detalhe": str(exc)})
        return resposta_erro(503, "Índice de busca indisponível")

    @app.exception_handler(Exception)
    async def _inesperado(_: Request, exc: Exception) -> JSONResponse:
        log.exception("erro_inesperado")
        return resposta_erro(500, "Erro interno")
