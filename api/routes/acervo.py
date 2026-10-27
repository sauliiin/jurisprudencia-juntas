"""Rotas que o site usa: /api/stats, /api/search, /api/voto/{file_id} e /api/file/{file_id}."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from api.config import Settings, get_settings
from api.db import Db
from api.schemas import Erro, Instancia, Mes, ModoBusca, ResultadoBusca, Stats, Voto
from api.services import busca

router = APIRouter(prefix="/api", tags=["acervo"])

ERROS = {404: {"model": Erro}, 422: {"model": Erro}, 503: {"model": Erro}}


@router.get("/stats", response_model=Stats, summary="Números do acervo")
def stats(db: Db) -> dict:
    return busca.stats(db)


@router.get("/search", response_model=ResultadoBusca, summary="Busca nas decisões", responses=ERROS)
def search(
    db: Db,
    settings: Annotated[Settings, Depends(get_settings)],
    q: Annotated[str, Query(max_length=500, description="Texto da busca.")] = "",
    instancia: Annotated[Instancia, Query(description="Instância da decisão.")] = "todas",
    modo: Annotated[ModoBusca, Query(description="phrase: expressão exata; words: todas as palavras.")] = "phrase",
    mes: Annotated[Mes, Query(description="Mês da decisão (exige ano).")] = "",
    ano: Annotated[str, Query(pattern=r"^(\d{4})?$", description="Ano da decisão.")] = "",
    limit: Annotated[int, Query(ge=1, description="Resultados por página (acima do máximo, usa o máximo).")] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> dict:
    parametros = busca.ParametrosBusca(
        q=q, instancia=instancia, modo=modo, mes=mes, ano=ano, limit=min(limit, settings.max_limit), offset=offset
    )
    return busca.buscar(db, parametros)


@router.get("/voto/{file_id}", response_model=Voto, summary="Uma decisão completa", responses=ERROS)
def voto(file_id: str, db: Db) -> dict:
    payload = busca.voto(db, file_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Voto não encontrado")
    return payload


@router.get(
    "/file/{file_id}",
    summary="Arquivo original da decisão",
    response_class=FileResponse,
    responses={**ERROS, 200: {"content": {"application/pdf": {}, "application/octet-stream": {}}}},
)
def arquivo(file_id: str, db: Db, settings: Annotated[Settings, Depends(get_settings)]) -> FileResponse:
    if settings.arquivos_dir is None:
        raise HTTPException(status_code=404, detail="Arquivos originais não disponíveis neste ambiente")
    achado = busca.caminho_do_arquivo(db, file_id, settings.arquivos_dir)
    if achado is None:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    caminho, nome = achado
    disposicao = "inline" if caminho.suffix.lower() in {".pdf", ".txt"} else "attachment"
    return FileResponse(
        caminho,
        headers={"Content-Disposition": f"{disposicao}; filename*=UTF-8''{quote(nome)}"},
    )
