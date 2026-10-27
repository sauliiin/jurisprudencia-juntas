"""Health check para o Docker, o load balancer e o monitoramento."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api import __version__
from api.config import Settings, get_settings
from api.db import IndiceIndisponivel, conectar
from api.schemas import Health

router = APIRouter(tags=["saúde"])


@router.get("/health", response_model=Health, summary="Status da API e do índice")
def health(settings: Annotated[Settings, Depends(get_settings)]) -> JSONResponse:
    try:
        con = conectar(settings.indice)
        try:
            votos = con.execute("SELECT count(*) FROM votos").fetchone()[0]
        finally:
            con.close()
    except (IndiceIndisponivel, sqlite3.Error):
        corpo = Health(status="degradado", indice=False, versao=__version__)
        return JSONResponse(corpo.model_dump(), status_code=503, headers={"Cache-Control": "no-store"})
    corpo = Health(status="ok", indice=True, votos=votos, versao=__version__)
    return JSONResponse(corpo.model_dump(), headers={"Cache-Control": "no-store"})
